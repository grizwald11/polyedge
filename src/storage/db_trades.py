"""Trade, order, position, and signal database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import Signal, Trade

logger = logging.getLogger(__name__)


class TradesMixin:
    """Database mixin for trade, order, signal, and position operations."""

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
            "UPDATE signals SET acted_on=1, order_id=?, status='executed' WHERE id=?",
            (order_id, signal_id),
        )
        conn.commit()

    def update_signal_risk_result(
        self, signal_id: int, passed: bool,
        failed_checks: list[str], warnings: list[str],
    ) -> None:
        """Record risk gate results for a signal (H-1/H-5)."""
        conn = self._get_conn()
        status = "generated" if passed else "risk_gated"
        conn.execute(
            """UPDATE signals
               SET risk_passed=?, risk_failed_checks=?, risk_warnings=?, status=?
               WHERE id=?""",
            (
                int(passed),
                ", ".join(failed_checks),
                ", ".join(warnings),
                status,
                signal_id,
            ),
        )
        conn.commit()

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Get recent signals."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_skipped_signals_for_date(self, date_str: str | None = None) -> list[dict]:
        """H-6: Get signals that were generated but risk-gated for a specific date.

        Returns list of dicts with edge, market_id, risk_failed_checks.
        """
        from datetime import date as date_type
        from datetime import timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT market_id, edge, risk_failed_checks FROM signals "
            "WHERE timestamp >= ? AND timestamp < ? AND status = 'risk_gated' "
            "ORDER BY timestamp DESC",
            (date_str, next_date_str),
        ).fetchall()
        return [dict(row) for row in rows]

    # ──────────────────────────────────────
    # Trade Operations
    # ──────────────────────────────────────

    def log_trade(self, trade: Trade) -> int:
        """Log a completed trade. Ignores duplicates (same order_id + side)."""
        conn = self._get_conn()
        platform = trade.platform.value if hasattr(trade.platform, 'value') else str(trade.platform)
        if not self._write_lock.acquire(timeout=60):
            logger.error("Database write lock timeout (60s) in log_trade — concurrent write contention")
            raise TimeoutError("Database write lock acquisition timed out in log_trade")
        try:
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
        finally:
            self._write_lock.release()

    # ──────────────────────────────────────
    # Pending Order Operations (H-1)
    # ──────────────────────────────────────

    def save_pending_order(self, order_id: str, cost: float) -> None:
        """Persist a resting (open) order so it survives process restarts.

        Called by OrderRouter after adding an order to the in-memory pending dict.
        On restart, load_pending_orders() rebuilds the dict from this table.
        """
        conn = self._get_conn()
        if not self._write_lock.acquire(timeout=60):
            logger.error("Database write lock timeout (60s) in save_pending_order — concurrent write contention")
            raise TimeoutError("Database write lock acquisition timed out in save_pending_order")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pending_orders (order_id, cost) VALUES (?, ?)",
                (order_id, cost),
            )
            conn.commit()
        finally:
            self._write_lock.release()

    def delete_pending_order(self, order_id: str) -> None:
        """Remove a pending order record (order filled, cancelled, or expired)."""
        conn = self._get_conn()
        if not self._write_lock.acquire(timeout=60):
            logger.error("Database write lock timeout (60s) in delete_pending_order — concurrent write contention")
            raise TimeoutError("Database write lock acquisition timed out in delete_pending_order")
        try:
            conn.execute(
                "DELETE FROM pending_orders WHERE order_id = ?",
                (order_id,),
            )
            conn.commit()
        finally:
            self._write_lock.release()

    def load_pending_orders(self) -> dict[str, float]:
        """Load all persisted pending orders on startup.

        Returns dict of order_id -> cost for rebuilding _pending_orders in OrderRouter.
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT order_id, cost FROM pending_orders").fetchall()
        return {row["order_id"]: row["cost"] for row in rows}

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
        if not self._write_lock.acquire(timeout=60):
            logger.error("Database write lock timeout (60s) in log_exit_reason — concurrent write contention")
            raise TimeoutError("Database write lock acquisition timed out in log_exit_reason")
        try:
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
        finally:
            self._write_lock.release()

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
        from datetime import date as date_type
        from datetime import timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC",
            (date_str, next_date_str),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_pnl(self, date_str: str | None = None) -> float:
        """Get total realized P&L for a given day.

        Args:
            date_str: Date in YYYY-MM-DD format. Defaults to today (UTC).
        """
        from datetime import date as date_type
        from datetime import timedelta
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_date_str = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total FROM trades "
            "WHERE timestamp >= ? AND timestamp < ?",
            (date_str, next_date_str),
        ).fetchone()
        return row["total"] if row else 0.0

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
            # Determine if YES or NO position from token_id suffix
            # M-10: Use suffix check (-no, _no, :no) to avoid false matches
            # on token IDs that happen to contain "no" as a substring (e.g. "innovation")
            token_id = r.get("token_id") or ""
            token_lower = token_id.lower()
            is_no = (
                token_lower.endswith("-no")
                or token_lower.endswith("_no")
                or token_lower.endswith(":no")
                or token_lower == "no"
            )
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

    def get_portfolio_summary(self) -> dict:
        """Get portfolio-level summary stats."""
        conn = self._get_conn()
        trades = conn.execute(
            "SELECT COUNT(*) as cnt, ROUND(COALESCE(SUM(realized_pnl), 0), 4) as total FROM trades"
        ).fetchone()
        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE realized_pnl > 0"
        ).fetchone()
        return {
            "total_trades": trades["cnt"] if trades else 0,
            "total_pnl": trades["total"] if trades else 0.0,
            "winning_trades": wins["cnt"] if wins else 0,
        }
