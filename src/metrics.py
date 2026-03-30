"""Metrics tracker — structured JSON logging for 24/7 monitoring.

Tracks cycle counts, trade counts, error rates, and timing for
health checks and operational dashboards.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.database import Database

logger = logging.getLogger(__name__)


class Metrics:
    """Simple metrics tracker — logs structured JSON for monitoring."""

    def __init__(self) -> None:
        self.cycle_count: int = 0
        self.trades_today: int = 0
        self.errors_today: int = 0
        self.last_cycle_time: float | None = None  # epoch seconds
        self.last_cycle_duration_ms: float = 0
        self.last_cycle_signals: int = 0
        self.last_cycle_trades: int = 0
        self.open_positions: int = 0
        self._started_at: float = time.time()
        self._daily_reset_date: str = ""
        # M-22: Signal selection tracking
        self.signals_generated: int = 0
        self.signals_risk_gated: int = 0
        self.signals_executed: int = 0
        # M-21: Edge vs return correlation (bounded to last 1000 entries)
        self._edge_return_log: list[dict] = []
        self._max_edge_return_entries = 1000

    def _check_daily_reset(self) -> None:
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self.trades_today = 0
            self.errors_today = 0
            self._daily_reset_date = today

    def record_cycle(
        self, duration_ms: float, trades: int, signals: int, positions: int
    ):
        """Record a completed scan cycle."""
        self._check_daily_reset()
        self.cycle_count += 1
        self.last_cycle_time = time.time()
        self.last_cycle_duration_ms = duration_ms
        self.last_cycle_trades = trades
        self.last_cycle_signals = signals
        self.open_positions = positions
        self.trades_today += trades

        logger.info(
            json.dumps({
                "event": "cycle_complete",
                "cycle": self.cycle_count,
                "duration_ms": round(duration_ms, 1),
                "signals": signals,
                "trades": trades,
                "positions": positions,
            })
        )

    def record_error(self, component: str, error_msg: str):
        """Record an error."""
        self._check_daily_reset()
        self.errors_today += 1

        logger.warning(
            json.dumps({
                "event": "error",
                "component": component,
                "error": str(error_msg)[:200],
                "errors_today": self.errors_today,
            })
        )

    def record_signal_generated(self) -> None:
        """Record a signal was generated (M-22)."""
        self.signals_generated += 1

    def record_signal_risk_gated(self) -> None:
        """Record a signal was blocked by risk gates (M-22)."""
        self.signals_risk_gated += 1

    def record_signal_executed(self) -> None:
        """Record a signal was executed (M-22)."""
        self.signals_executed += 1

    def record_trade(
        self, market_id: str, direction: str, size: int, price: float,
        predicted_edge: float = 0.0,
    ):
        """Record a trade execution."""
        logger.info(
            json.dumps({
                "event": "trade",
                "market": market_id,
                "direction": direction,
                "size": size,
                "price": price,
                "predicted_edge": round(predicted_edge, 4),
            })
        )

    def record_closed_position(
        self, market_id: str, predicted_edge: float, realized_return: float,
        days_held: float,
    ):
        """Record edge vs realized return for a closed position (M-21)."""
        entry = {
            "market": market_id,
            "predicted_edge": round(predicted_edge, 4),
            "realized_return": round(realized_return, 4),
            "days_held": round(days_held, 1),
        }
        self._edge_return_log.append(entry)
        # Trim to bounded size
        if len(self._edge_return_log) > self._max_edge_return_entries:
            self._edge_return_log = self._edge_return_log[-self._max_edge_return_entries:]
        logger.info(json.dumps({"event": "position_closed", **entry}))

    def get_health_status(self) -> dict:
        """Return current health metrics for dashboard/alerts."""
        now = time.time()
        uptime_seconds = now - self._started_at
        seconds_since_last_cycle = (
            now - self.last_cycle_time if self.last_cycle_time else None
        )

        return {
            "status": "healthy" if self._is_healthy() else "degraded",
            "uptime_seconds": round(uptime_seconds, 0),
            "cycle_count": self.cycle_count,
            "trades_today": self.trades_today,
            "errors_today": self.errors_today,
            "open_positions": self.open_positions,
            "last_cycle_duration_ms": round(self.last_cycle_duration_ms, 1),
            "last_cycle_signals": self.last_cycle_signals,
            "signals_generated": self.signals_generated,
            "signals_risk_gated": self.signals_risk_gated,
            "signals_executed": self.signals_executed,
            "seconds_since_last_cycle": (
                round(seconds_since_last_cycle, 0)
                if seconds_since_last_cycle is not None
                else None
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _is_healthy(self) -> bool:
        """Basic health check — degraded if no cycle in 10 minutes."""
        if self.last_cycle_time is None:
            return True  # Just started, no cycles yet
        return (time.time() - self.last_cycle_time) < 600

    def persist_to_db(self, db: "Database") -> None:
        """Persist current metrics snapshot to database for cross-restart analysis.

        Stores a JSON snapshot in the metrics_snapshots table. Called at the end
        of each cycle so metrics survive pm2 restarts.
        """
        try:
            conn = db._get_conn()
            # Create table if it doesn't exist (idempotent)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    data TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO metrics_snapshots (timestamp, data) VALUES (?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(self.get_health_status()),
                ),
            )
            # Keep only last 1000 snapshots to bound table size
            conn.execute(
                "DELETE FROM metrics_snapshots WHERE id NOT IN "
                "(SELECT id FROM metrics_snapshots ORDER BY id DESC LIMIT 1000)"
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"Failed to persist metrics: {e}")
