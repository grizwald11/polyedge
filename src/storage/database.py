"""SQLite database with WAL mode — stores all market data, trades, and calibration records.

Uses raw sqlite3 with helper methods (no heavy ORM for simplicity).
WAL mode allows concurrent reads during writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.models import Market, MarketSnapshot, Signal, Order, Trade, CalibrationRecord

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 5

SCHEMA_SQL = """
-- Markets (Kalshi uses ticker as primary key)
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
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
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL
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

-- Calibration records
CREATE TABLE IF NOT EXISTS calibration_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
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

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


class Database:
    """SQLite database manager with WAL mode."""

    def __init__(self, db_path: str = "data/markets.db", wal_mode: bool = True):
        self.db_path = db_path
        self.wal_mode = wal_mode
        self._ensure_directory()
        self._init_db()

    def _ensure_directory(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if self.wal_mode:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript(SCHEMA_SQL)
            # Set schema version
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
            conn.commit()
            self._run_migrations(conn)
            logger.info(f"Database initialized at {self.db_path}")
        finally:
            conn.close()

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
        conn.commit()

    # ──────────────────────────────────────
    # Market Operations
    # ──────────────────────────────────────

    def upsert_market(self, market: Market):
        """Insert or update a market."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO markets (
                    ticker, question, description, category, tags, tokens,
                    end_date, volume_24h, volume_total, liquidity, spread,
                    active, closed, resolution_source, slug, subtitle, event_ticker,
                    first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
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
                    last_updated=excluded.last_updated
            """, (
                market.ticker,
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
                now,
                now,
            ))
            conn.commit()
        finally:
            conn.close()

    def upsert_markets(self, markets: list[Market]):
        """Bulk upsert markets."""
        for market in markets:
            self.upsert_market(market)

    def get_active_markets(self) -> list[dict]:
        """Get all active markets from database."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM markets WHERE active=1 AND closed=0 ORDER BY volume_24h DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_market(self, ticker: str) -> Optional[dict]:
        """Get a single market by ticker."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM markets WHERE ticker=?", (ticker,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_market_count(self) -> int:
        """Get total active market count."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE active=1").fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Snapshot Operations
    # ──────────────────────────────────────

    def log_snapshot(self, snapshot: MarketSnapshot):
        """Log a market price snapshot."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO market_snapshots (market_id, timestamp, yes_price, no_price, spread, volume_1h, liquidity)
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
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Signal Operations
    # ──────────────────────────────────────

    def log_signal(self, signal: Signal) -> int:
        """Log a trading signal, return the row ID."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                INSERT INTO signals (
                    strategy, market_id, market_question, direction,
                    edge, probability_estimate, market_price, confidence,
                    reasoning, timestamp, acted_on, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.strategy.value,
                signal.market_id,
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
        finally:
            conn.close()

    def update_signal_acted_on(self, signal_id: int, order_id: str):
        """Mark a signal as acted on after successful trade execution."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE signals SET acted_on=1, order_id=? WHERE id=?",
                (order_id, signal_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Get recent signals."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Trade Operations
    # ──────────────────────────────────────

    def log_trade(self, trade: Trade) -> int:
        """Log a completed trade."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                INSERT INTO trades (order_id, market_id, token_id, side, price, size, fee, realized_pnl, strategy, paper, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.order_id,
                trade.market_id,
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
        finally:
            conn.close()

    def get_trades_today(self) -> list[dict]:
        """Get all trades from today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC",
                (today,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

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
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC",
                (date_str, next_date_str),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_strategy_pnl(self, date_str: str | None = None) -> dict[str, dict]:
        """Get P&L breakdown by strategy for a date.

        Returns dict of strategy_name -> {"count": int, "pnl": float}
        """
        from datetime import date as date_type, timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT strategy, COUNT(*) as cnt, COALESCE(SUM(realized_pnl), 0) as total "
                "FROM trades WHERE timestamp >= ? AND timestamp < ? GROUP BY strategy",
                (date_str, next_date_str),
            ).fetchall()
            return {
                row["strategy"]: {"count": row["cnt"], "pnl": row["total"]}
                for row in rows
            }
        finally:
            conn.close()

    def get_portfolio_summary(self) -> dict:
        """Get portfolio-level summary stats."""
        conn = self._get_conn()
        try:
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
        finally:
            conn.close()

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
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM trades "
                "WHERE timestamp >= ? AND timestamp < ?",
                (date_str, next_date_str),
            ).fetchone()
            return row["total"] if row else 0.0
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Calibration Operations
    # ──────────────────────────────────────

    def log_calibration(self, record: CalibrationRecord) -> int:
        """Log a calibration prediction."""
        conn = self._get_conn()
        try:
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
        finally:
            conn.close()

    def get_unresolved_predictions(self) -> list[dict]:
        """Get predictions that haven't been resolved yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM calibration_records WHERE actual_outcome IS NULL"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_all_calibration_records(self) -> list[dict]:
        """Get all calibration records for analysis."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM calibration_records ORDER BY predicted_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

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
        try:
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
        finally:
            conn.close()

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
        try:
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
        finally:
            conn.close()

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
        try:
            cursor = conn.execute(
                """UPDATE calibration_records
                   SET actual_outcome = ?, resolved_at = ?,
                       brier_score = ?, profit_loss = ?
                   WHERE market_id = ? AND actual_outcome IS NULL""",
                (actual_outcome, now, brier_score, profit_loss, market_id),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

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
        try:
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
        finally:
            conn.close()

    def load_circuit_breaker_state(self) -> Optional[dict]:
        """Load persisted circuit breaker state. Returns None if no state saved."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM circuit_breaker_state WHERE id=1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Cooldowns
    # ──────────────────────────────────────

    def save_cooldown(self, market_id: str, exit_time: datetime):
        """Persist a cooldown entry."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cooldowns (market_id, exit_time) VALUES (?, ?)",
                (market_id, exit_time.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def load_cooldowns(self, max_age_seconds: int = 3600) -> dict[str, datetime]:
        """Load valid cooldowns, deleting expired ones.

        Args:
            max_age_seconds: Maximum cooldown age in seconds.

        Returns:
            Dict of market_id -> exit_time for still-active cooldowns.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT market_id, exit_time FROM cooldowns").fetchall()
            now = datetime.now(timezone.utc)
            active: dict[str, datetime] = {}
            expired: list[str] = []

            for row in rows:
                exit_time = datetime.fromisoformat(row["exit_time"])
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
        finally:
            conn.close()

    def delete_cooldown(self, market_id: str):
        """Remove a cooldown entry."""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM cooldowns WHERE market_id=?", (market_id,))
            conn.commit()
        finally:
            conn.close()

    # ──────────────────────────────────────
    # Stats
    # ──────────────────────────────────────

    def get_stats(self) -> dict:
        """Get database stats summary."""
        conn = self._get_conn()
        try:
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
        finally:
            conn.close()
