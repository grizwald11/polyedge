"""Statistics and reporting database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class StatsMixin:
    """Database mixin for P&L reporting, strategy stats, and timeseries."""

    # ──────────────────────────────────────
    # Stats & Reporting
    # ──────────────────────────────────────

    def get_strategy_pnl(self, date_str: str | None = None) -> dict[str, dict]:
        """Get P&L breakdown by strategy for a date.

        Returns dict of strategy_name -> {"count": int, "pnl": float}
        """
        from datetime import date as date_type
        from datetime import timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT strategy, COUNT(*) as cnt, ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total "
            "FROM trades WHERE timestamp >= ? AND timestamp < ? GROUP BY strategy",
            (date_str, next_date_str),
        ).fetchall()
        return {
            row["strategy"]: {"count": row["cnt"], "pnl": row["total"]}
            for row in rows
        }

    def get_pnl_timeseries(self, days: int = 30) -> list[dict]:
        """Get daily P&L aggregates for charting.

        Returns list of {date, pnl, cumulative_pnl, trade_count} dicts.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT DATE(timestamp) as date,
                   ROUND(COALESCE(SUM(realized_pnl), 0), 4) as pnl,
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
                   ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total_pnl,
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

    def get_pnl_by_strategy(self, days: int = 30) -> dict[str, float]:
        """Get total realized P&L grouped by strategy over the last N days.

        H-7: Enables per-strategy P&L attribution for circuit breaker logging
        and daily reporting.

        Args:
            days: Lookback window in days (default 30).

        Returns:
            Dict of strategy_name -> total realized_pnl (float).
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT strategy, ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total "
            "FROM trades WHERE timestamp >= ? GROUP BY strategy",
            (cutoff,),
        ).fetchall()
        return {row["strategy"]: row["total"] for row in rows}

    def get_stats(self) -> dict:
        """Get database stats summary."""
        conn = self._get_conn()
        markets = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE active=1").fetchone()
        signals = conn.execute("SELECT COUNT(*) as cnt FROM signals").fetchone()
        trades = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
        pnl = conn.execute("SELECT ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total FROM trades").fetchone()

        return {
            "active_markets": markets["cnt"] if markets else 0,
            "total_signals": signals["cnt"] if signals else 0,
            "total_trades": trades["cnt"] if trades else 0,
            "total_pnl": pnl["total"] if pnl else 0.0,
        }
