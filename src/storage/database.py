"""SQLite database with WAL mode — stores all market data, trades, and calibration records.

Uses raw sqlite3 with helper methods (no heavy ORM for simplicity).
WAL mode allows concurrent reads during writes.

# NOTE: Database is unencrypted. Enable FileVault on macOS or use SQLCipher for at-rest encryption.
#
# H-6: Prices and monetary values are stored as REAL (float). Aggregation queries
# use ROUND(..., 4) to mitigate cumulative float drift. A full migration to
# INTEGER cents would eliminate this but requires changes across all callers.
# Current approach is safe for typical trading volumes (<10K trades).
#
# H-3 MIGRATION PLAN (execute when trade volume exceeds 10K or multi-user):
#   1. Create schema v16 with INTEGER cent columns alongside existing REAL columns
#   2. Backfill: UPDATE trades SET price_cents = ROUND(price * 100) etc.
#   3. Update all callers to use dollars_to_cents() / cents_to_dollars() helpers
#   4. Drop old REAL columns in schema v17 after validation
#   Affected tables/columns:
#     - markets: volume_24h, volume_total, liquidity, spread
#     - market_snapshots: yes_price, no_price, volume_1h, liquidity
#     - signals: edge, probability_estimate, market_price, confidence
#     - orders: price, size, cost, fill_price
#     - trades: price, size, fee, realized_pnl
#     - calibration_records: predicted_probability, market_price_at_prediction
#
# M-10: Domain-specific methods are split into mixin modules for maintainability:
#   - db_markets.py   — market/snapshot CRUD, cleanup
#   - db_trades.py    — trade/order/signal/position operations
#   - db_calibration.py — calibration/prediction tracking
#   - db_risk.py      — circuit breaker, cooldowns, settings
#   - db_whales.py    — whale trades, cross-platform pairs
#   - db_stats.py     — P&L reporting, strategy stats
# The Database class inherits from all mixins so existing imports are unchanged.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional


# M-10: Import domain mixins
from src.storage.db_calibration import CalibrationMixin
from src.storage.db_markets import MarketsMixin
from src.storage.db_risk import RiskMixin
from src.storage.db_stats import StatsMixin
from src.storage.db_trades import TradesMixin
from src.storage.db_whales import WhalesMixin

logger = logging.getLogger(__name__)


def prices_equal(a: float, b: float, epsilon: float = 1e-6) -> bool:
    """Compare prices with epsilon tolerance to handle float storage (H-20/L-1)."""
    return abs(a - b) < epsilon


SCHEMA_VERSION = 6

SCHEMA_SQL = """
-- NOTE: Prices and monetary values are stored as REAL (float). Ideally these
-- would be INTEGER cents to avoid floating-point rounding, but migrating the
-- schema is deferred to avoid risk. All comparison logic should use epsilon
-- tolerances (e.g., abs(a - b) < 1e-9) rather than exact equality.

-- Markets (ticker + platform composite key for multi-platform support)
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT NOT NULL,
    platform TEXT DEFAULT 'kalshi',
    question TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'Other',
    tags TEXT DEFAULT '[]',
    tokens TEXT DEFAULT '[]',
    end_date TEXT,
    volume_24h REAL DEFAULT 0,
    volume_total REAL DEFAULT 0,
    liquidity REAL DEFAULT 0,
    spread REAL DEFAULT 0,
    active INTEGER DEFAULT 1,
    closed INTEGER DEFAULT 0,
    resolution_source TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    subtitle TEXT DEFAULT '',
    event_ticker TEXT DEFAULT '',
    result TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (ticker, platform)
);
-- M-2: Unique index on ticker so child table FK refs to markets(ticker) are valid
-- with PRAGMA foreign_keys=ON (composite PK alone doesn't satisfy single-column FK).
CREATE UNIQUE INDEX IF NOT EXISTS idx_markets_ticker_unique ON markets(ticker);

-- Market price snapshots
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    yes_price REAL NOT NULL,
    no_price REAL NOT NULL,
    spread REAL DEFAULT 0,
    volume_1h REAL DEFAULT 0,
    liquidity REAL DEFAULT 0,
    FOREIGN KEY (market_id) REFERENCES markets(ticker)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_market_time ON market_snapshots(market_id, timestamp);

-- Trading signals
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    market_id TEXT NOT NULL,
    platform TEXT DEFAULT 'kalshi',
    market_question TEXT DEFAULT '',
    direction TEXT NOT NULL,
    edge REAL NOT NULL,
    probability_estimate REAL NOT NULL,
    market_price REAL NOT NULL,
    confidence REAL DEFAULT 0.5,
    reasoning TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    acted_on INTEGER DEFAULT 0,
    order_id TEXT,
    risk_passed INTEGER,          -- H-1/H-5: 1=passed, 0=rejected, NULL=not checked
    risk_failed_checks TEXT DEFAULT '',  -- H-5: comma-separated failed gate names
    risk_warnings TEXT DEFAULT '',       -- H-5: comma-separated warning messages
    status TEXT DEFAULT 'generated',     -- H-1: generated|risk_gated|executed|skipped
    FOREIGN KEY (market_id) REFERENCES markets(ticker)
);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    platform TEXT DEFAULT 'kalshi',
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    cost REAL DEFAULT 0,
    order_type TEXT DEFAULT 'GTC',
    fee_rate_bps INTEGER DEFAULT 0,
    status TEXT DEFAULT 'PENDING',
    strategy TEXT DEFAULT 'ai_probability',
    signal_id TEXT,
    paper INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    filled_at TEXT,
    fill_price REAL,
    cancelled_at TEXT,
    rejection_reason TEXT,
    exchange_order_id TEXT,  -- Kalshi/Polymarket order ID for cancel/lookup
    FOREIGN KEY (market_id) REFERENCES markets(ticker)
);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Trades (filled orders)
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    platform TEXT DEFAULT 'kalshi',
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    strategy TEXT DEFAULT 'ai_probability',
    paper INTEGER DEFAULT 1,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

-- Calibration records
CREATE TABLE IF NOT EXISTS calibration_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    platform TEXT DEFAULT 'kalshi',
    market_question TEXT DEFAULT '',
    strategy TEXT DEFAULT 'ai_probability',
    predicted_probability REAL NOT NULL,
    market_price_at_prediction REAL NOT NULL,
    actual_outcome INTEGER,  -- NULL = unresolved, 1 = YES, 0 = NO
    predicted_at TEXT NOT NULL,
    resolved_at TEXT,
    brier_score REAL,        -- (predicted - actual)^2
    profit_loss REAL,        -- realized P&L for this prediction
    prompt_variant TEXT DEFAULT '',  -- A/B test variant used for this prediction
    FOREIGN KEY (market_id) REFERENCES markets(ticker)
);
CREATE INDEX IF NOT EXISTS idx_calibration_market ON calibration_records(market_id);
CREATE INDEX IF NOT EXISTS idx_calibration_resolved ON calibration_records(actual_outcome);
CREATE INDEX IF NOT EXISTS idx_calibration_resolved_at ON calibration_records(resolved_at);

-- Whale wallets (Phase 6)
CREATE TABLE IF NOT EXISTS whale_wallets (
    address TEXT PRIMARY KEY,
    alias TEXT DEFAULT '',
    win_rate REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    last_active TEXT,
    categories TEXT DEFAULT '[]',
    trusted INTEGER DEFAULT 1,
    added_at TEXT NOT NULL
);

-- Circuit breaker persistent state (singleton row)
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_losing_days INTEGER DEFAULT 0,
    reduced_sizing INTEGER DEFAULT 0,
    halted INTEGER DEFAULT 0,
    halt_reason TEXT,
    halt_time TEXT,
    last_recorded_day TEXT,
    last_updated TEXT NOT NULL
);

-- Cooldown timers for risk engine
CREATE TABLE IF NOT EXISTS cooldowns (
    market_id TEXT PRIMARY KEY,
    exit_time TEXT NOT NULL,
    duration_seconds INTEGER
);

-- Arbitrage relationships (cached Claude validations)
CREATE TABLE IF NOT EXISTS arb_relationships (
    market_a TEXT NOT NULL,
    market_b TEXT NOT NULL,
    relationship_data TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- Whale trades (Phase 6)
CREATE TABLE IF NOT EXISTS whale_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL DEFAULT 0,
    price REAL DEFAULT 0,
    detected_at TEXT NOT NULL,
    FOREIGN KEY (wallet_address) REFERENCES whale_wallets(address)
);
CREATE INDEX IF NOT EXISTS idx_whale_trades_market ON whale_trades(market_id);

-- Cross-platform market pairs (Kalshi <-> Polymarket)
CREATE TABLE IF NOT EXISTS cross_platform_pairs (
    kalshi_ticker TEXT NOT NULL,
    poly_condition_id TEXT NOT NULL,
    kalshi_question TEXT DEFAULT '',
    poly_question TEXT DEFAULT '',
    similarity REAL DEFAULT 0,
    validated INTEGER DEFAULT 0,
    validated_at TEXT,
    PRIMARY KEY (kalshi_ticker, poly_condition_id)
);

-- Pending orders (resting limit orders not yet filled)
-- Persisted so state survives restarts (H-1).
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id TEXT PRIMARY KEY,
    cost REAL NOT NULL
);

-- Divergence records -- tracks Claude-vs-market disagreements and who was right
CREATE TABLE IF NOT EXISTS divergence_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    category TEXT DEFAULT '',
    claude_estimate REAL NOT NULL,
    market_price REAL NOT NULL,
    divergence REAL NOT NULL,
    abs_divergence REAL NOT NULL,
    predicted_at TEXT NOT NULL,
    actual_outcome INTEGER,
    claude_was_right INTEGER,
    resolved_at TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(ticker)
);
CREATE INDEX IF NOT EXISTS idx_divergence_market ON divergence_records(market_id);
CREATE INDEX IF NOT EXISTS idx_divergence_category ON divergence_records(category);

-- Prompt variant Thompson sampling state (Feature 7)
CREATE TABLE IF NOT EXISTS prompt_variant_stats (
    category TEXT NOT NULL,
    variant_name TEXT NOT NULL,
    alpha REAL DEFAULT 1.0,
    beta REAL DEFAULT 1.0,
    total_predictions INTEGER DEFAULT 0,
    sum_brier REAL DEFAULT 0.0,
    updated_at TEXT,
    PRIMARY KEY (category, variant_name)
);

-- Schema version tracking (M-7: with applied_at timestamp)
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT
);
"""


class Database(
    MarketsMixin,
    TradesMixin,
    CalibrationMixin,
    RiskMixin,
    WhalesMixin,
    StatsMixin,
):
    """SQLite database manager with WAL mode and write serialization.

    Domain-specific methods are provided by mixin classes (M-10):
      - MarketsMixin:     market/snapshot CRUD, cleanup
      - TradesMixin:      trade/order/signal/position operations
      - CalibrationMixin: calibration/prediction tracking
      - RiskMixin:        circuit breaker, cooldowns, settings
      - WhalesMixin:      whale trades, cross-platform pairs
      - StatsMixin:       P&L reporting, strategy stats
    """

    def __init__(self, db_path: str = "data/markets.db", wal_mode: bool = True):
        self.db_path = db_path
        self.wal_mode = wal_mode
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()
        self._ensure_directory()
        self._init_db()

    def _ensure_directory(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        # H-16: Ensure database file has restrictive permissions (owner-only)
        db_file = Path(self.db_path)
        if db_file.exists():
            current_mode = db_file.stat().st_mode & 0o777
            if current_mode != 0o600:
                try:
                    db_file.chmod(0o600)
                    logger.info(f"Fixed DB file permissions: {oct(current_mode)} -> 0o600")
                except OSError as e:
                    logger.warning(f"Could not fix DB permissions: {e}")
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.wal_mode:
            conn.execute("PRAGMA journal_mode=WAL")
        # M-2: FK enforcement enabled. Child tables reference markets(ticker)
        # which has a UNIQUE index (added in migration v12) to satisfy SQLite's
        # FK requirement that the parent column(s) have a UNIQUE constraint.
        conn.execute("PRAGMA foreign_keys=ON")
        logger.debug("FK enforcement enabled (unique index on markets.ticker satisfies FK refs)")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        return conn

    def close(self):
        """Close the persistent database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except (sqlite3.Error, OSError) as e:
                logger.debug(f"Error closing database connection: {e}")
            self._conn = None

    def wal_checkpoint(self) -> int:
        """Run WAL checkpoint to merge WAL frames back into the main database.

        Returns the number of WAL frames checkpointed, or -1 on error.
        Should be called periodically (e.g. every hour) to prevent unbounded
        WAL growth on long-running processes.
        """
        conn = self._get_conn()
        try:
            result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            # result: (busy, log_frames, checkpointed_frames)
            checkpointed = result[2] if result else 0
            if checkpointed > 0:
                logger.info(f"WAL checkpoint: {checkpointed} frames merged")
            return checkpointed
        except sqlite3.Error as e:
            logger.warning(f"WAL checkpoint failed: {e}")
            return -1

    def backup(self, dest_path: str | None = None) -> str:
        """Create a safe backup using SQLite's online backup API.

        Args:
            dest_path: Destination file path. Defaults to {db_path}.bak-{timestamp}.

        Returns:
            The path of the created backup file.
        """
        if self.db_path == ":memory:":
            raise ValueError("Cannot backup in-memory database")

        if dest_path is None:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            dest_path = f"{self.db_path}.bak-{ts}"

        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

        source_conn = self._get_conn()
        dest_conn = sqlite3.connect(dest_path)
        try:
            source_conn.backup(dest_conn)
            logger.info(f"Database backed up to {dest_path}")
        finally:
            dest_conn.close()

        # Restrict backup file permissions
        try:
            import os
            os.chmod(dest_path, 0o600)
        except OSError:
            pass

        return dest_path

    def _init_db(self):
        """Create tables if they don't exist."""
        from datetime import datetime, timezone

        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)

        # M-7: Schema version tracking with mismatch detection
        existing = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        db_version = existing[0] if existing and existing[0] is not None else None
        if db_version is not None and db_version != SCHEMA_VERSION:
            logger.warning(
                f"Schema version mismatch: database has v{db_version}, "
                f"code expects v{SCHEMA_VERSION}. Migrations will run."
            )
        if db_version is None or db_version < SCHEMA_VERSION:
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
        self._run_migrations(conn)
        self._prune_stale_data(conn)
        # L-6: Restrict database file permissions to owner-only after creation
        if self.db_path != ":memory:":
            import os
            try:
                os.chmod(self.db_path, 0o600)
            except OSError as e:
                logger.warning(f"Could not set DB file permissions: {e}")
        logger.info(f"Database initialized at {self.db_path}")

    def _prune_stale_data(self, conn: sqlite3.Connection):
        """Prune old unacted signals and stale snapshots to control DB growth."""
        # Keep acted signals forever, prune unacted older than 24h
        deleted_signals = conn.execute(
            "DELETE FROM signals WHERE acted_on = 0 AND timestamp < datetime('now', '-24 hours')"
        ).rowcount
        # Prune snapshots older than 7 days
        deleted_snaps = conn.execute(
            "DELETE FROM market_snapshots WHERE timestamp < datetime('now', '-7 days')"
        ).rowcount
        conn.commit()
        if deleted_signals or deleted_snaps:
            logger.info(
                f"DB pruned: {deleted_signals} stale signals, {deleted_snaps} old snapshots"
            )

    def _run_migrations(self, conn: sqlite3.Connection):
        """Run schema migrations for existing databases."""
        # H-8: Forward-compatible INTEGER cents migration started in v17 below.
        # Next steps: add _cents columns to orders table, then update read paths
        # to prefer _cents columns when available.
        # Migration v2 -> v3: add brier_score and profit_loss to calibration_records
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(calibration_records)").fetchall()
        }
        if "brier_score" not in existing_cols:
            conn.execute("ALTER TABLE calibration_records ADD COLUMN brier_score REAL")
            logger.info("Migration: added brier_score column to calibration_records")
        if "profit_loss" not in existing_cols:
            conn.execute("ALTER TABLE calibration_records ADD COLUMN profit_loss REAL")
            logger.info("Migration: added profit_loss column to calibration_records")

        # Migration v3 -> v4: circuit_breaker_state and cooldowns tables
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "circuit_breaker_state" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    consecutive_losing_days INTEGER DEFAULT 0,
                    reduced_sizing INTEGER DEFAULT 0,
                    halted INTEGER DEFAULT 0,
                    halt_reason TEXT,
                    halt_time TEXT,
                    last_updated TEXT NOT NULL
                )
            """)
            logger.info("Migration: created circuit_breaker_state table")
        if "cooldowns" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cooldowns (
                    market_id TEXT PRIMARY KEY,
                    exit_time TEXT NOT NULL,
                    duration_seconds INTEGER
                )
            """)
            logger.info("Migration: created cooldowns table")

        # Migration v4 -> v5: arb_relationships and whale_trades tables
        if "arb_relationships" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS arb_relationships (
                    market_a TEXT NOT NULL,
                    market_b TEXT NOT NULL,
                    relationship_data TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    PRIMARY KEY (market_a, market_b)
                )
            """)
            logger.info("Migration: created arb_relationships table")
        if "whale_trades" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS whale_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    size REAL DEFAULT 0,
                    price REAL DEFAULT 0,
                    detected_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_whale_trades_market ON whale_trades(market_id)"
            )
            logger.info("Migration: created whale_trades table")

        # Migration: add result column to markets (for backtest engine)
        market_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(markets)").fetchall()
        }
        if "result" not in market_cols:
            conn.execute("ALTER TABLE markets ADD COLUMN result TEXT DEFAULT ''")
            logger.info("Migration: added result column to markets")

        # Migration v5 -> v6: add missing performance indices
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calibration_resolved_at ON calibration_records(resolved_at)")

        # Migration v6: add platform columns for multi-platform support
        # Add platform column to tables that don't have it yet
        for table_name in ("signals", "orders", "trades", "calibration_records"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
            if "platform" not in cols:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN platform TEXT DEFAULT 'kalshi'")
                logger.info(f"Migration v6: added platform column to {table_name}")

        # Migrate markets table to composite PK (ticker, platform)
        market_cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)").fetchall()}
        if "platform" not in market_cols:
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                BEGIN;
                DROP TABLE IF EXISTS markets_new;
                CREATE TABLE markets_new (
                    ticker TEXT NOT NULL,
                    platform TEXT DEFAULT 'kalshi',
                    question TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    category TEXT DEFAULT 'Other',
                    tags TEXT DEFAULT '[]',
                    tokens TEXT DEFAULT '[]',
                    end_date TEXT,
                    volume_24h REAL DEFAULT 0,
                    volume_total REAL DEFAULT 0,
                    liquidity REAL DEFAULT 0,
                    spread REAL DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    closed INTEGER DEFAULT 0,
                    resolution_source TEXT DEFAULT '',
                    slug TEXT DEFAULT '',
                    subtitle TEXT DEFAULT '',
                    event_ticker TEXT DEFAULT '',
                    result TEXT DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    PRIMARY KEY (ticker, platform)
                );
                INSERT INTO markets_new SELECT
                    ticker, 'kalshi', question, description, category, tags, tokens,
                    end_date, volume_24h, volume_total, liquidity, spread,
                    active, closed, resolution_source, slug, subtitle, event_ticker,
                    result, first_seen, last_updated
                FROM markets;
                DROP TABLE markets;
                ALTER TABLE markets_new RENAME TO markets;
                COMMIT;
                PRAGMA foreign_keys=ON;
            """)
            logger.info("Migration v6: migrated markets to composite PK (ticker, platform)")

        # Create cross_platform_pairs table if missing
        if "cross_platform_pairs" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cross_platform_pairs (
                    kalshi_ticker TEXT NOT NULL,
                    poly_condition_id TEXT NOT NULL,
                    kalshi_question TEXT DEFAULT '',
                    poly_question TEXT DEFAULT '',
                    similarity REAL DEFAULT 0,
                    validated INTEGER DEFAULT 0,
                    validated_at TEXT,
                    PRIMARY KEY (kalshi_ticker, poly_condition_id)
                )
            """)
            logger.info("Migration v6: created cross_platform_pairs table")

        # Migration v7: add unique constraint on trades to prevent duplicate recording
        existing_indices = {
            row[1]
            for row in conn.execute("PRAGMA index_list(trades)").fetchall()
        }
        if "idx_trades_unique_order" not in existing_indices:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_order "
                "ON trades(order_id, side)"
            )
            logger.info("Migration v7: added unique constraint on trades(order_id, side)")

        # Migration: add exchange_order_id to orders for proper cancel operations
        order_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "exchange_order_id" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN exchange_order_id TEXT")
            logger.info("Migration: added exchange_order_id column to orders")

        # Migration v7: add unique constraint on market_snapshots
        if "idx_snapshots_unique" not in {
            row[1] for row in conn.execute("PRAGMA index_list(market_snapshots)").fetchall()
        }:
            # Remove exact duplicates first before creating unique index
            conn.execute("""
                DELETE FROM market_snapshots WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM market_snapshots
                    GROUP BY market_id, timestamp
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_unique "
                "ON market_snapshots(market_id, timestamp)"
            )
            logger.info("Migration v7: added unique constraint on market_snapshots")

        # Migration v8: position_exits table for tracking exit reasons
        if "position_exits" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_exits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    platform TEXT DEFAULT 'kalshi',
                    strategy TEXT DEFAULT '',
                    exit_reason TEXT NOT NULL,
                    exit_price REAL DEFAULT 0,
                    position_size REAL DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (market_id) REFERENCES markets(ticker)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_position_exits_market "
                "ON position_exits(market_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_position_exits_reason "
                "ON position_exits(exit_reason)"
            )
            logger.info("Migration v8: created position_exits table")

        # Migration v9: pending_orders table for crash-recovery of resting orders (H-1)
        if "pending_orders" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_orders (
                    order_id TEXT PRIMARY KEY,
                    cost REAL NOT NULL
                )
            """)
            logger.info("Migration v9: created pending_orders table")

        # Migration v10: add duration_seconds to cooldowns (H-15)
        cooldown_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(cooldowns)").fetchall()
        }
        if "duration_seconds" not in cooldown_cols:
            conn.execute("ALTER TABLE cooldowns ADD COLUMN duration_seconds INTEGER")
            logger.info("Migration v10: added duration_seconds column to cooldowns")

        # Migration v11: add risk gate columns to signals (H-1/H-5)
        signal_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        if "risk_passed" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN risk_passed INTEGER")
            conn.execute("ALTER TABLE signals ADD COLUMN risk_failed_checks TEXT DEFAULT ''")
            conn.execute("ALTER TABLE signals ADD COLUMN risk_warnings TEXT DEFAULT ''")
            conn.execute("ALTER TABLE signals ADD COLUMN status TEXT DEFAULT 'generated'")
            logger.info("Migration v11: added risk gate columns to signals (H-1/H-5)")

        # Migration v12: unique index on markets(ticker) for FK enforcement (M-2).
        # Also added to SCHEMA_SQL for fresh databases; this handles existing DBs.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_markets_ticker_unique "
            "ON markets(ticker)"
        )

        # Migration v13: add last_recorded_day to circuit_breaker_state (C-1)
        cb_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(circuit_breaker_state)").fetchall()
        }
        if "last_recorded_day" not in cb_cols:
            conn.execute("ALTER TABLE circuit_breaker_state ADD COLUMN last_recorded_day TEXT")
            logger.info("Migration v13: added last_recorded_day to circuit_breaker_state")

        # Migration v14: prompt_variant column on calibration_records + prompt_variant_stats table
        cal_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(calibration_records)").fetchall()
        }
        if "prompt_variant" not in cal_cols:
            conn.execute("ALTER TABLE calibration_records ADD COLUMN prompt_variant TEXT DEFAULT ''")
            logger.info("Migration v14: added prompt_variant column to calibration_records")
        if "prompt_variant_stats" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_variant_stats (
                    category TEXT NOT NULL,
                    variant_name TEXT NOT NULL,
                    alpha REAL DEFAULT 1.0,
                    beta REAL DEFAULT 1.0,
                    total_predictions INTEGER DEFAULT 0,
                    sum_brier REAL DEFAULT 0.0,
                    updated_at TEXT,
                    PRIMARY KEY (category, variant_name)
                )
            """)
            logger.info("Migration v14: created prompt_variant_stats table")

        # Migration v15: high_water_mark on circuit_breaker_state (H-5)
        if "high_water_mark" not in cb_cols:
            conn.execute("ALTER TABLE circuit_breaker_state ADD COLUMN high_water_mark REAL")
            logger.info("Migration v15: added high_water_mark to circuit_breaker_state")

        # Migration v16: pending_exits table for crash-recovery of exit orders (C-4)
        if "pending_exits" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_exits (
                    market_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
            """)
            logger.info("Migration v16: created pending_exits table for exit order persistence")

        # Migration v17: H-8 — add INTEGER cents columns to trades table.
        # Forward-compatible: existing REAL columns remain untouched, new code
        # can read from _cents columns when available. Backfills from existing data.
        trades_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "price_cents" not in trades_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN price_cents INTEGER")
            conn.execute("ALTER TABLE trades ADD COLUMN fee_cents INTEGER")
            conn.execute(
                "UPDATE trades SET "
                "price_cents = CAST(ROUND(price * 100) AS INTEGER), "
                "fee_cents = CAST(ROUND(fee * 100) AS INTEGER) "
                "WHERE price_cents IS NULL"
            )
            logger.info("Migration v17: added price_cents/fee_cents INTEGER columns to trades (H-8)")

        conn.commit()

    def archive_old_data(self, days: int = 90) -> int:
        """Move records older than `days` from main tables to archive tables (H-11).

        Creates archive tables if they don't exist, then moves old records from
        trades, orders, signals, and market_snapshots into their respective
        archive tables. This controls unbounded database growth while preserving
        historical data for analysis.

        Args:
            days: Records older than this many days are archived. Default 90.

        Returns:
            Total number of records archived across all tables.
        """
        conn = self._get_conn()
        cutoff = f"-{days} days"
        total_archived = 0

        # Table configs: (source_table, timestamp_column, column_list)
        archive_configs = [
            (
                "trades",
                "timestamp",
                "id, order_id, market_id, platform, token_id, side, price, size, "
                "fee, realized_pnl, strategy, paper, timestamp",
            ),
            (
                "orders",
                "created_at",
                "id, market_id, platform, token_id, side, price, size, cost, "
                "order_type, fee_rate_bps, status, strategy, signal_id, paper, "
                "created_at, filled_at, fill_price, cancelled_at, rejection_reason, "
                "exchange_order_id",
            ),
            (
                "signals",
                "timestamp",
                "id, strategy, market_id, platform, market_question, direction, "
                "edge, probability_estimate, market_price, confidence, reasoning, "
                "timestamp, acted_on, order_id, risk_passed, risk_failed_checks, "
                "risk_warnings, status",
            ),
            (
                "market_snapshots",
                "timestamp",
                "id, market_id, timestamp, yes_price, no_price, spread, "
                "volume_1h, liquidity",
            ),
        ]

        with self._write_lock:
            try:
                for source_table, ts_col, columns in archive_configs:
                    archive_table = f"{source_table}_archive"

                    # Create archive table with same schema (if not exists)
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {archive_table} "
                        f"AS SELECT {columns} FROM {source_table} WHERE 0"
                    )

                    # Copy old records to archive
                    inserted = conn.execute(
                        f"INSERT INTO {archive_table} SELECT {columns} "
                        f"FROM {source_table} "
                        f"WHERE {ts_col} < datetime('now', ?)",
                        (cutoff,),
                    ).rowcount

                    # Delete archived records from source
                    if inserted > 0:
                        conn.execute(
                            f"DELETE FROM {source_table} "
                            f"WHERE {ts_col} < datetime('now', ?)",
                            (cutoff,),
                        )

                    total_archived += inserted
                    if inserted > 0:
                        logger.info(
                            f"Archived {inserted} rows from {source_table} "
                            f"(older than {days} days)"
                        )

                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("Failed to archive old data")
                raise

        if total_archived > 0:
            logger.info(f"Archive complete: {total_archived} total records moved")
        else:
            logger.info("Archive: no records older than %d days to archive", days)

        return total_archived
