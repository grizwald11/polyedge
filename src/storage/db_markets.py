"""Market and snapshot database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import Market, MarketSnapshot

logger = logging.getLogger(__name__)


class MarketsMixin:
    """Database mixin for market and snapshot CRUD operations."""

    # ──────────────────────────────────────
    # Market Operations
    # ──────────────────────────────────────

    def upsert_market(self, market: Market):
        """Insert or update a market."""
        now = datetime.now(timezone.utc).isoformat()
        platform = market.platform.value if hasattr(market.platform, 'value') else str(market.platform)
        with self._write("upsert_market") as conn:
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
        with self._write("upsert_markets") as conn:
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
        with self._write("log_snapshot") as conn:
            conn.execute("""
                INSERT OR REPLACE INTO market_snapshots (market_id, timestamp, yes_price, no_price, spread, volume_1h, liquidity, is_synthetic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.market_id,
                snapshot.timestamp.isoformat(),
                snapshot.yes_price,
                snapshot.no_price,
                snapshot.spread,
                snapshot.volume_1h,
                snapshot.liquidity,
                int(snapshot.is_synthetic),
            ))
            conn.commit()

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

    def cleanup_old_snapshots(self, max_age_days: int = 30) -> int:
        """Delete market snapshots older than max_age_days.

        Prevents unbounded growth of the snapshots table in long-running
        deployments. Call this periodically (e.g., daily).

        Returns number of rows deleted.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._write("cleanup_old_snapshots") as conn:
            cursor = conn.execute(
                "DELETE FROM market_snapshots WHERE timestamp < ?", (cutoff,)
            )
            conn.commit()
            deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} snapshots older than {max_age_days} days")
        return deleted

    def cleanup_orphaned_records(self) -> dict[str, int]:
        """Remove orphaned child records whose parent markets no longer exist.

        Foreign keys are now enforced (PRAGMA foreign_keys=ON), so new orphans
        cannot be created. This method cleans up any legacy orphans that
        pre-date FK enforcement. Call periodically (e.g., daily alongside
        cleanup_old_snapshots).

        Returns dict of {table_name: rows_deleted}.
        """
        with self._write("cleanup_orphaned_records") as conn:
            deleted: dict[str, int] = {}
            child_tables = [
                "market_snapshots", "signals", "orders", "trades",
                "calibration_records", "prices",
            ]
            for table in child_tables:
                try:
                    cursor = conn.execute(f"""
                        DELETE FROM {table}
                        WHERE market_id NOT IN (SELECT ticker FROM markets)
                    """)
                    if cursor.rowcount > 0:
                        deleted[table] = cursor.rowcount
                except Exception as e:
                    logger.debug(f"Orphan cleanup skipped for {table}: {e}")
            if deleted:
                conn.commit()
                logger.info(f"Orphan cleanup: {deleted}")
            return deleted
