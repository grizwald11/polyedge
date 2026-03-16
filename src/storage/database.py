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

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Markets
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
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
    neg_risk INTEGER DEFAULT 0,
    event_slug TEXT DEFAULT '',
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
    FOREIGN KEY (market_id) REFERENCES markets(condition_id)
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
    FOREIGN KEY (market_id) REFERENCES markets(condition_id)
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
    FOREIGN KEY (market_id) REFERENCES markets(condition_id)
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
    FOREIGN KEY (market_id) REFERENCES markets(condition_id)
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
            logger.info(f"Database initialized at {self.db_path}")
        finally:
            conn.close()

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
                    condition_id, question, description, category, tags, tokens,
                    end_date, volume_24h, volume_total, liquidity, spread,
                    active, closed, resolution_source, slug, neg_risk, event_slug,
                    first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
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
                market.condition_id,
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
                int(market.neg_risk),
                market.event_slug,
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

    def get_market(self, condition_id: str) -> Optional[dict]:
        """Get a single market by condition ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM markets WHERE condition_id=?", (condition_id,)
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

    def get_daily_pnl(self) -> float:
        """Get today's total realized P&L."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM trades WHERE timestamp >= ?",
                (today,)
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
