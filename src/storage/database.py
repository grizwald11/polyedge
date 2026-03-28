"""SQLite database with WAL mode — stores all market data, trades, and calibration records.

Uses raw sqlite3 with helper methods (no heavy ORM for simplicity).
WAL mode allows concurrent reads during writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.models import Market, MarketSnapshot, Signal, Order, Trade, CalibrationRecord

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 6

SCHEMA_SQL = """
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
    last_updated TEXT NOT NULL
);

-- Cooldown timers for risk engine
CREATE TABLE IF NOT EXISTS cooldowns (
    market_id TEXT PRIMARY KEY,
    exit_time TEXT NOT NULL
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

-- Cross-platform market pairs (Kalshi ↔ Polymarket)
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

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


class Database:
    """SQLite database manager with WAL mode and write serialization."""

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
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.wal_mode:
            conn.execute("PRAGMA journal_mode=WAL")
        # FK enforcement disabled: v6 migration changed markets to composite PK
        # (ticker, platform) but child tables still reference single-column ticker.
        # Enabling FKs would break DELETE/UPDATE on signals/orders/trades because
        # SQLite requires the parent to have a UNIQUE constraint on the referenced
        # column(s), and composite PK (ticker, platform) doesn't satisfy FK refs
        # to markets(ticker) alone. App logic enforces referential integrity.
        # TECH DEBT: Migrate child tables to composite FK (market_id, platform)
        # to re-enable database-level referential integrity. Until then, orphaned
        # records are possible if markets are deleted without cascading.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        return conn

    def close(self):
        """Close the persistent database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                logger.debug(f"Error closing database connection: {e}")
            self._conn = None

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        # Set schema version
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,)
        )
        conn.commit()
        self._run_migrations(conn)
        self._prune_stale_data(conn)
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
                    exit_time TEXT NOT NULL
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

        conn.commit()

    # ──────────────────────────────────────
    # Market Operations
    # ──────────────────────────────────────

    def upsert_market(self, market: Market):
        """Insert or update a market."""
        now = datetime.now(timezone.utc).isoformat()
        platform = market.platform.value if hasattr(market.platform, 'value') else str(market.platform)
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO markets (
                ticker, platform, question, description, category, tags, tokens,
                end_date, volume_24h, volume_total, liquidity, spread,
                active, closed, resolution_source, slug, subtitle, event_ticker,
                result, first_seen, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, platform) DO UPDATE SET
                question=excluded.question,
                description=excluded.description,
                category=excluded.category,
                tags=excluded.tags,
                tokens=excluded.tokens,
                end_date=excluded.end_date,
                volume_24h=excluded.volume_24h,
                volume_total=excluded.volume_total,
                liquidity=excluded.liquidity,
                spread=excluded.spread,
                active=excluded.active,
                closed=excluded.closed,
                resolution_source=excluded.resolution_source,
                result=excluded.result,
                last_updated=excluded.last_updated
        """, (
            market.ticker,
            platform,
            market.question,
            market.description,
            market.category.value,
            json.dumps(market.tags),
            json.dumps([t.model_dump() for t in market.tokens]),
            market.end_date.isoformat() if market.end_date else None,
            market.volume_24h,
            market.volume_total,
            market.liquidity,
            market.spread,
            int(market.active),
            int(market.closed),
            market.resolution_source,
            market.slug,
            market.subtitle,
            market.event_ticker,
            market.result,
            now,
            now,
        ))
        conn.commit()

    def upsert_markets(self, markets: list[Market]):
        """Bulk upsert markets in a single transaction (much faster than N separate calls)."""
        if not markets:
            return
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        for market in markets:
            platform = market.platform.value if hasattr(market.platform, 'value') else str(market.platform)
            conn.execute("""
                INSERT INTO markets (
                    ticker, platform, question, description, category, tags, tokens,
                    end_date, volume_24h, volume_total, liquidity, spread,
                    active, closed, resolution_source, slug, subtitle, event_ticker,
                    result, first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, platform) DO UPDATE SET
                    question=excluded.question,
                    description=excluded.description,
                    category=excluded.category,
                    tags=excluded.tags,
                    tokens=excluded.tokens,
                    end_date=excluded.end_date,
                    volume_24h=excluded.volume_24h,
                    volume_total=excluded.volume_total,
                    liquidity=excluded.liquidity,
                    spread=excluded.spread,
                    active=excluded.active,
                    closed=excluded.closed,
                    resolution_source=excluded.resolution_source,
                    result=excluded.result,
                    last_updated=excluded.last_updated
            """, (
                market.ticker,
                platform,
                market.question,
                market.description,
                market.category.value,
                json.dumps(market.tags),
                json.dumps([t.model_dump() for t in market.tokens]),
                market.end_date.isoformat() if market.end_date else None,
                market.volume_24h,
                market.volume_total,
                market.liquidity,
                market.spread,
                int(market.active),
                int(market.closed),
                market.resolution_source,
                market.slug,
                market.subtitle,
                market.event_ticker,
                market.result,
                now,
                now,
            ))
        conn.commit()

    def get_active_markets(self) -> list[dict]:
        """Get all active markets from database."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM markets WHERE active=1 AND closed=0 ORDER BY volume_24h DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_market(self, ticker: str, platform: str = None) -> Optional[dict]:
        """Get a single market by ticker, optionally filtered by platform.

        When platform is None, returns the first match (backward-compatible).
        When platform is specified, returns only the exact match for that
        ticker+platform combination (correct for multi-platform usage).
        """
        conn = self._get_conn()
        if platform:
            row = conn.execute(
                "SELECT * FROM markets WHERE ticker=? AND platform=?", (ticker, platform)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM markets WHERE ticker=?", (ticker,)
            ).fetchone()
        return dict(row) if row else None

    def get_market_count(self) -> int:
        """Get total active market count."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE active=1").fetchone()
        return row["cnt"] if row else 0

    # ──────────────────────────────────────
    # Snapshot Operations
    # ──────────────────────────────────────

    def log_snapshot(self, snapshot: MarketSnapshot):
        """Log a market price snapshot. Replaces if same market+timestamp exists."""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO market_snapshots (market_id, timestamp, yes_price, no_price, spread, volume_1h, liquidity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.market_id,
            snapshot.timestamp.isoformat(),
            snapshot.yes_price,
            snapshot.no_price,
            snapshot.spread,
            snapshot.volume_1h,
            snapshot.liquidity,
        ))
        conn.commit()

    # ──────────────────────────────────────
    # Signal Operations
    # ──────────────────────────────────────

    def log_signal(self, signal: Signal) -> int:
        """Log a trading signal, return the row ID."""
        conn = self._get_conn()
        platform = signal.platform.value if hasattr(signal.platform, 'value') else str(signal.platform)
        cursor = conn.execute("""
            INSERT INTO signals (
                strategy, market_id, platform, market_question, direction,
                edge, probability_estimate, market_price, confidence,
                reasoning, timestamp, acted_on, order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.strategy.value,
            signal.market_id,
            platform,
            signal.market_question,
            signal.direction.value,
            signal.edge,
            signal.probability_estimate,
            signal.market_price,
            signal.confidence,
            signal.reasoning,
            signal.timestamp.isoformat(),
            int(signal.acted_on),
            signal.order_id,
        ))
        conn.commit()
        return cursor.lastrowid

    def update_signal_acted_on(self, signal_id: int, order_id: str):
        """Mark a signal as acted on after successful trade execution."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE signals SET acted_on=1, order_id=? WHERE id=?",
            (order_id, signal_id),
        )
        conn.commit()

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Get recent signals."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ──────────────────────────────────────
    # Trade Operations
    # ──────────────────────────────────────

    def log_trade(self, trade: Trade) -> int:
        """Log a completed trade. Ignores duplicates (same order_id + side)."""
        conn = self._get_conn()
        platform = trade.platform.value if hasattr(trade.platform, 'value') else str(trade.platform)
        with self._write_lock:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO trades (order_id, market_id, platform, token_id, side, price, size, fee, realized_pnl, strategy, paper, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.order_id,
                trade.market_id,
                platform,
                trade.token_id,
                trade.side.value,
                trade.price,
                trade.size,
                trade.fee,
                trade.realized_pnl,
                trade.strategy.value,
                int(trade.paper),
                trade.timestamp.isoformat(),
            ))
            conn.commit()
            return cursor.lastrowid

    def log_exit_reason(
        self,
        market_id: str,
        exit_reason: str,
        exit_price: float = 0.0,
        position_size: float = 0.0,
        realized_pnl: float = 0.0,
        strategy: str = "",
        platform: str = "kalshi",
    ):
        """Log the reason a position was exited."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn.execute("""
                INSERT INTO position_exits
                    (market_id, platform, strategy, exit_reason, exit_price,
                     position_size, realized_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id, platform, strategy, exit_reason,
                exit_price, position_size, realized_pnl, now,
            ))
            conn.commit()

    def has_recent_trade(self, market_id: str, seconds: int = 300) -> bool:
        """Check if a BUY trade was placed on this market within the last N seconds.

        Used to prevent duplicate trades when concurrent processes (e.g. pm2
        restart overlap) try to trade the same market simultaneously.

        Args:
            market_id: Market ticker
            seconds: Lookback window (default 5 minutes)

        Returns:
            True if a recent BUY trade exists
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades "
            "WHERE market_id=? AND side='BUY' AND timestamp > ?",
            (market_id, cutoff),
        ).fetchone()
        return row["cnt"] > 0 if row else False

    def has_recent_exit(self, market_id: str, seconds: int = 300) -> bool:
        """Check if a SELL trade was placed on this market within the last N seconds.

        Used to prevent duplicate exit orders when scan cycles overlap
        (e.g. pm2 restart or rapid consecutive cycles).

        Args:
            market_id: Market ticker
            seconds: Lookback window (default 5 minutes)

        Returns:
            True if a recent SELL trade exists
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades "
            "WHERE market_id=? AND side='SELL' AND timestamp > ?",
            (market_id, cutoff),
        ).fetchone()
        return row["cnt"] > 0 if row else False

    def get_trades_today(self) -> list[dict]:
        """Get all trades from today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC",
            (today,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_trades_for_date(self, date_str: str | None = None) -> list[dict]:
        """Get all trades for a specific date.

        Args:
            date_str: Date in YYYY-MM-DD format. Defaults to today (UTC).
        """
        from datetime import date as date_type, timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC",
            (date_str, next_date_str),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_strategy_pnl(self, date_str: str | None = None) -> dict[str, dict]:
        """Get P&L breakdown by strategy for a date.

        Returns dict of strategy_name -> {"count": int, "pnl": float}
        """
        from datetime import date as date_type, timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT strategy, COUNT(*) as cnt, COALESCE(SUM(realized_pnl), 0) as total "
            "FROM trades WHERE timestamp >= ? AND timestamp < ? GROUP BY strategy",
            (date_str, next_date_str),
        ).fetchall()
        return {
            row["strategy"]: {"count": row["cnt"], "pnl": row["total"]}
            for row in rows
        }

    def get_portfolio_summary(self) -> dict:
        """Get portfolio-level summary stats."""
        conn = self._get_conn()
        trades = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(realized_pnl), 0) as total FROM trades"
        ).fetchone()
        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE realized_pnl > 0"
        ).fetchone()
        return {
            "total_trades": trades["cnt"] if trades else 0,
            "total_pnl": trades["total"] if trades else 0.0,
            "winning_trades": wins["cnt"] if wins else 0,
        }

    def get_positions_with_pnl(self) -> list[dict]:
        """Compute open positions with unrealized P&L from trades + latest snapshots.

        Aggregates BUY/SELL trades per market to find net positions, then joins
        with the latest market snapshot to compute current price and unrealized P&L.
        Works without a running PositionManager.
        """
        conn = self._get_conn()
        rows = conn.execute("""
            WITH net_positions AS (
                SELECT
                    market_id,
                    SUM(CASE WHEN side='BUY' THEN size ELSE -size END) as net_size,
                    SUM(CASE WHEN side='BUY' THEN price * size ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN side='BUY' THEN size ELSE 0 END), 0) as avg_entry,
                    SUM(CASE WHEN side='BUY' THEN price * size ELSE 0 END) as total_cost,
                    SUM(CASE WHEN side='BUY' THEN fee ELSE 0 END) as total_fees,
                    MAX(strategy) as strategy,
                    -- Determine direction from the first BUY trade's token_id
                    MAX(CASE WHEN side='BUY' THEN token_id ELSE NULL END) as token_id
                FROM trades
                GROUP BY market_id
                HAVING net_size > 0
            ),
            latest_snap AS (
                SELECT market_id, yes_price, no_price,
                       ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY timestamp DESC) as rn
                FROM market_snapshots
            )
            SELECT
                np.market_id,
                np.net_size as size,
                np.avg_entry,
                np.total_cost,
                np.total_fees,
                np.strategy,
                np.token_id,
                COALESCE(ls.yes_price, 0) as yes_price,
                COALESCE(ls.no_price, 0) as no_price,
                m.question as market_question
            FROM net_positions np
            LEFT JOIN latest_snap ls ON np.market_id = ls.market_id AND ls.rn = 1
            LEFT JOIN markets m ON np.market_id = m.ticker
            ORDER BY np.total_cost DESC
        """).fetchall()

        positions = []
        for row in rows:
            r = dict(row)
            # Determine if YES or NO position from token_id
            token_id = r.get("token_id") or ""
            is_no = "no" in token_id.lower()
            current_price = r["no_price"] if is_no else r["yes_price"]
            direction = "BUY_NO" if is_no else "BUY_YES"

            # Unrealized P&L: (current_price - avg_entry) * size for YES
            # For NO positions: same formula since avg_entry is the NO price paid
            unrealized_pnl = (current_price - r["avg_entry"]) * r["size"] if current_price > 0 else 0.0

            # Return on investment percentage
            cost_basis = r["total_cost"] + r["total_fees"]
            roi_pct = (unrealized_pnl / cost_basis * 100) if cost_basis > 0 else 0.0

            positions.append({
                "market_id": r["market_id"],
                "market_question": r.get("market_question") or r["market_id"],
                "direction": direction,
                "size": int(r["size"]),
                "avg_entry": round(r["avg_entry"], 4) if r["avg_entry"] else 0.0,
                "current_price": round(current_price, 4),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "cost_basis": round(cost_basis, 2),
                "total_fees": round(r["total_fees"], 2),
                "roi_pct": round(roi_pct, 1),
                "strategy": r["strategy"],
            })

        return positions

    def get_daily_pnl(self, date_str: str | None = None) -> float:
        """Get total realized P&L for a given day.

        Args:
            date_str: Date in YYYY-MM-DD format. Defaults to today (UTC).
        """
        from datetime import date as date_type, timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM trades "
            "WHERE timestamp >= ? AND timestamp < ?",
            (date_str, next_date_str),
        ).fetchone()
        return row["total"] if row else 0.0

    # ──────────────────────────────────────
    # Calibration Operations
    # ──────────────────────────────────────

    def log_calibration(self, record: CalibrationRecord) -> int:
        """Log a calibration prediction."""
        conn = self._get_conn()
        cursor = conn.execute("""
            INSERT INTO calibration_records (
                market_id, market_question, strategy,
                predicted_probability, market_price_at_prediction,
                actual_outcome, predicted_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.market_id,
            record.market_question,
            record.strategy.value,
            record.predicted_probability,
            record.market_price_at_prediction,
            record.actual_outcome,
            record.predicted_at.isoformat(),
            record.resolved_at.isoformat() if record.resolved_at else None,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_unresolved_predictions(self) -> list[dict]:
        """Get predictions that haven't been resolved yet."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM calibration_records WHERE actual_outcome IS NULL"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_calibration_records(self) -> list[dict]:
        """Get all calibration records for analysis."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM calibration_records ORDER BY predicted_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def store_prediction(
        self,
        market_ticker: str,
        predicted_probability: float,
        predicted_side: str,
        market_price: float,
        strategy: str = "ai_probability",
        confidence_low: float = 0.0,
        confidence_high: float = 1.0,
        market_question: str = "",
    ) -> int:
        """Store a prediction when Claude makes an assessment.

        Args:
            market_ticker: Market ticker.
            predicted_probability: Our estimated probability (0-1).
            predicted_side: "YES" or "NO" — the side we'd bet.
            market_price: Market price at time of prediction.
            strategy: Strategy name.
            confidence_low: Lower bound of confidence interval.
            confidence_high: Upper bound of confidence interval.
            market_question: Human-readable question.

        Returns:
            Database row ID.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        cursor = conn.execute("""
            INSERT INTO calibration_records (
                market_id, market_question, strategy,
                predicted_probability, market_price_at_prediction,
                predicted_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            market_ticker,
            market_question,
            strategy,
            predicted_probability,
            market_price,
            now,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_latest_prediction(self, market_ticker: str) -> Optional[dict]:
        """Get the most recent prediction for a market.

        Returns dict with predicted_probability, market_price_at_prediction,
        predicted_at, or None if no prediction exists.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT predicted_probability, market_price_at_prediction, predicted_at "
            "FROM calibration_records WHERE market_id=? "
            "ORDER BY predicted_at DESC LIMIT 1",
            (market_ticker,),
        ).fetchone()
        return dict(row) if row else None

    def get_resolved_predictions(
        self,
        strategy: Optional[str] = None,
        days: Optional[int] = None,
    ) -> list[dict]:
        """Get all resolved predictions for analysis.

        Args:
            strategy: Filter by strategy name (None = all).
            days: Only include predictions from last N days.

        Returns:
            List of resolved calibration records.
        """
        conn = self._get_conn()
        query = "SELECT * FROM calibration_records WHERE actual_outcome IS NOT NULL"
        params: list = []

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if days:
            query += " AND predicted_at >= datetime('now', ?)"
            params.append(f"-{days} days")

        query += " ORDER BY resolved_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_resolution(
        self,
        market_id: str,
        actual_outcome: int,
        brier_score: Optional[float] = None,
        profit_loss: Optional[float] = None,
    ) -> int:
        """Update calibration records with resolution data.

        Args:
            market_id: Market ticker.
            actual_outcome: 1 for YES, 0 for NO.
            brier_score: (predicted - actual)^2.
            profit_loss: Realized P&L for this prediction.

        Returns:
            Number of records updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE calibration_records
               SET actual_outcome = ?, resolved_at = ?,
                   brier_score = ?, profit_loss = ?
               WHERE market_id = ? AND actual_outcome IS NULL""",
            (actual_outcome, now, brier_score, profit_loss, market_id),
        )
        conn.commit()
        return cursor.rowcount

    # ──────────────────────────────────────
    # Circuit Breaker State
    # ──────────────────────────────────────

    def save_circuit_breaker_state(
        self,
        consecutive_losing_days: int,
        reduced_sizing: bool,
        halted: bool,
        halt_reason: Optional[str] = None,
        halt_time: Optional[str] = None,
    ):
        """Persist circuit breaker state (singleton row, id=1)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO circuit_breaker_state
                (id, consecutive_losing_days, reduced_sizing, halted,
                 halt_reason, halt_time, last_updated)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                consecutive_losing_days=excluded.consecutive_losing_days,
                reduced_sizing=excluded.reduced_sizing,
                halted=excluded.halted,
                halt_reason=excluded.halt_reason,
                halt_time=excluded.halt_time,
                last_updated=excluded.last_updated
        """, (
            consecutive_losing_days,
            int(reduced_sizing),
            int(halted),
            halt_reason,
            halt_time,
            now,
        ))
        conn.commit()

    def load_circuit_breaker_state(self) -> Optional[dict]:
        """Load persisted circuit breaker state. Returns None if no state saved."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id=1"
        ).fetchone()
        return dict(row) if row else None

    # ──────────────────────────────────────
    # Cooldowns
    # ──────────────────────────────────────

    def save_cooldown(self, market_id: str, exit_time: datetime):
        """Persist a cooldown entry."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO cooldowns (market_id, exit_time) VALUES (?, ?)",
            (market_id, exit_time.isoformat()),
        )
        conn.commit()

    def load_cooldowns(self, max_age_seconds: int = 3600) -> dict[str, datetime]:
        """Load valid cooldowns, deleting expired ones.

        Args:
            max_age_seconds: Maximum cooldown age in seconds.

        Returns:
            Dict of market_id -> exit_time for still-active cooldowns.
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT market_id, exit_time FROM cooldowns").fetchall()
        now = datetime.now(timezone.utc)
        active: dict[str, datetime] = {}
        expired: list[str] = []

        for row in rows:
            try:
                exit_time = datetime.fromisoformat(row["exit_time"])
            except (ValueError, TypeError):
                logger.warning(f"Invalid cooldown datetime for {row['market_id']}: {row['exit_time']!r} — removing")
                expired.append(row["market_id"])
                continue
            if exit_time.tzinfo is None:
                exit_time = exit_time.replace(tzinfo=timezone.utc)
            if (now - exit_time).total_seconds() < max_age_seconds:
                active[row["market_id"]] = exit_time
            else:
                expired.append(row["market_id"])

        # Clean up expired
        if expired:
            conn.executemany(
                "DELETE FROM cooldowns WHERE market_id=?",
                [(m,) for m in expired],
            )
            conn.commit()

        return active

    def delete_cooldown(self, market_id: str):
        """Remove a cooldown entry."""
        conn = self._get_conn()
        conn.execute("DELETE FROM cooldowns WHERE market_id=?", (market_id,))
        conn.commit()

    # ──────────────────────────────────────
    # Stats
    # ──────────────────────────────────────

    def get_snapshots_for_market(
        self,
        market_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """Get price snapshots for a market within a time range.

        Args:
            market_id: Market ticker.
            start: ISO start timestamp (inclusive). None = no lower bound.
            end: ISO end timestamp (inclusive). None = no upper bound.

        Returns:
            List of snapshot dicts ordered by timestamp.
        """
        conn = self._get_conn()
        query = "SELECT * FROM market_snapshots WHERE market_id = ?"
        params: list = [market_id]
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        query += " ORDER BY timestamp ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_pnl_timeseries(self, days: int = 30) -> list[dict]:
        """Get daily P&L aggregates for charting.

        Returns list of {date, pnl, cumulative_pnl, trade_count} dicts.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT DATE(timestamp) as date,
                   COALESCE(SUM(realized_pnl), 0) as pnl,
                   COUNT(*) as trade_count
            FROM trades
            WHERE timestamp >= ?
            GROUP BY DATE(timestamp)
            ORDER BY date ASC
        """, (cutoff,)).fetchall()

        result = []
        cumulative = 0.0
        for row in rows:
            cumulative += row["pnl"]
            result.append({
                "date": row["date"],
                "pnl": row["pnl"],
                "cumulative_pnl": cumulative,
                "trade_count": row["trade_count"],
            })
        return result

    def get_strategy_stats(self) -> list[dict]:
        """Get per-strategy lifetime totals.

        Returns list of {strategy, trade_count, total_pnl, winning, losing, win_rate}.
        """
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT strategy,
                   COUNT(*) as trade_count,
                   COALESCE(SUM(realized_pnl), 0) as total_pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losing
            FROM trades
            GROUP BY strategy
            ORDER BY total_pnl DESC
        """).fetchall()

        result = []
        for row in rows:
            count = row["trade_count"]
            winning = row["winning"] or 0
            result.append({
                "strategy": row["strategy"],
                "trade_count": count,
                "total_pnl": row["total_pnl"],
                "winning": winning,
                "losing": row["losing"] or 0,
                "win_rate": winning / count if count > 0 else 0.0,
            })
        return result

    def get_whale_activity(self, limit: int = 50) -> list[dict]:
        """Get recent whale trades."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM whale_trades ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get database stats summary."""
        conn = self._get_conn()
        markets = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE active=1").fetchone()
        signals = conn.execute("SELECT COUNT(*) as cnt FROM signals").fetchone()
        trades = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
        pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) as total FROM trades").fetchone()

        return {
            "active_markets": markets["cnt"] if markets else 0,
            "total_signals": signals["cnt"] if signals else 0,
            "total_trades": trades["cnt"] if trades else 0,
            "total_pnl": pnl["total"] if pnl else 0.0,
        }

    def cleanup_old_snapshots(self, max_age_days: int = 30) -> int:
        """Delete market snapshots older than max_age_days.

        Prevents unbounded growth of the snapshots table in long-running
        deployments. Call this periodically (e.g., daily).

        Returns number of rows deleted.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM market_snapshots WHERE timestamp < ?", (cutoff,)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} snapshots older than {max_age_days} days")
        return deleted

    # ──────────────────────────────────────
    # Cross-Platform Pairs
    # ──────────────────────────────────────

    def upsert_cross_platform_pair(
        self,
        kalshi_ticker: str,
        poly_condition_id: str,
        kalshi_question: str = "",
        poly_question: str = "",
        similarity: float = 0.0,
        validated: bool = False,
    ):
        """Insert or update a cross-platform market pair."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO cross_platform_pairs
                (kalshi_ticker, poly_condition_id, kalshi_question, poly_question,
                 similarity, validated, validated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kalshi_ticker, poly_condition_id) DO UPDATE SET
                similarity=excluded.similarity,
                validated=excluded.validated,
                validated_at=excluded.validated_at
        """, (
            kalshi_ticker,
            poly_condition_id,
            kalshi_question,
            poly_question,
            similarity,
            int(validated),
            datetime.now(timezone.utc).isoformat() if validated else None,
        ))
        conn.commit()

    def get_cross_platform_pairs(self, validated_only: bool = False) -> list[dict]:
        """Get all cross-platform market pairs."""
        conn = self._get_conn()
        query = "SELECT * FROM cross_platform_pairs"
        if validated_only:
            query += " WHERE validated = 1"
        query += " ORDER BY similarity DESC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]
