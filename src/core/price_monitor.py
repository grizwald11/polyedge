"""Real-time price monitor — detects adverse moves and triggers auto-exit.

Plugs into the WebSocket price feed and monitors open positions for large
adverse price movements. When a position moves >15% against entry, triggers
an auto-exit signal and alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from src.core.models import Direction, Position

logger = logging.getLogger(__name__)

# Configuration
ADVERSE_MOVE_THRESHOLD = 0.15  # 15% adverse move triggers auto-exit
ALERT_THRESHOLD = 0.10          # 10% adverse move triggers alert (no exit)
MIN_POSITION_AGE_SECONDS = 60   # Don't auto-exit within 60s of entry (avoid noise)


@dataclass
class AdverseMoveAlert:
    """Alert for an adverse price movement."""
    market_id: str
    direction: str
    entry_price: float
    current_price: float
    adverse_pct: float
    should_exit: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Callback type for adverse move events
AdverseMoveCallback = Callable[[AdverseMoveAlert], Coroutine]


class PriceMonitor:
    """Monitors WebSocket price updates for adverse moves on open positions.

    Usage:
        monitor = PriceMonitor()
        monitor.on_adverse_move(my_handler)
        monitor.update_positions(position_manager.get_all_positions())
        # Call check_price() on each WebSocket TickerUpdate
        await monitor.check_price("TICKER", 0.35, 0.65)
    """

    def __init__(
        self,
        adverse_threshold: float = ADVERSE_MOVE_THRESHOLD,
        alert_threshold: float = ALERT_THRESHOLD,
    ):
        self._adverse_threshold = adverse_threshold
        self._alert_threshold = alert_threshold
        self._positions: dict[str, Position] = {}  # market_id -> Position
        self._callbacks: list[AdverseMoveCallback] = []
        self._alerted: set[str] = set()  # market_ids already alerted (avoid spam)
        self._exited: set[str] = set()  # market_ids already auto-exited

    def on_adverse_move(self, callback: AdverseMoveCallback):
        """Register a callback for adverse move events."""
        self._callbacks.append(callback)

    def update_positions(self, positions: list[Position]):
        """Refresh tracked positions from position manager."""
        new_ids = {p.market_id for p in positions}
        # Remove positions that were closed
        closed = set(self._positions.keys()) - new_ids
        for mid in closed:
            self._positions.pop(mid, None)
            self._alerted.discard(mid)
            self._exited.discard(mid)
        # Add/update positions
        for p in positions:
            self._positions[p.market_id] = p

    async def check_price(
        self,
        market_id: str,
        yes_price: float,
        no_price: float,
    ) -> Optional[AdverseMoveAlert]:
        """Check if a price update represents an adverse move.

        Should be called on every WebSocket TickerUpdate.

        Returns:
            AdverseMoveAlert if threshold crossed, None otherwise.
        """
        position = self._positions.get(market_id)
        if position is None:
            return None

        if market_id in self._exited:
            return None

        # Determine which price matters for this position
        if position.direction in (Direction.BUY_YES, Direction.SELL_NO):
            current = yes_price
        else:
            current = no_price

        entry = position.avg_entry_price
        if entry <= 0:
            return None

        # Calculate adverse move percentage
        if position.direction in (Direction.BUY_YES, Direction.BUY_NO):
            # Long position: adverse = price dropping
            adverse_pct = (entry - current) / entry
        else:
            # Short position: adverse = price rising
            adverse_pct = (current - entry) / entry

        if adverse_pct <= 0:
            # Price moved favorably, no alert
            return None

        # Check minimum position age to avoid noise on fresh entries
        age = (datetime.now(timezone.utc) - position.opened_at).total_seconds()
        if age < MIN_POSITION_AGE_SECONDS:
            return None

        # Determine severity
        should_exit = adverse_pct >= self._adverse_threshold
        should_alert = adverse_pct >= self._alert_threshold

        if not should_alert:
            return None

        # Create alert
        alert = AdverseMoveAlert(
            market_id=market_id,
            direction=position.direction.value,
            entry_price=entry,
            current_price=current,
            adverse_pct=round(adverse_pct, 4),
            should_exit=should_exit,
        )

        # Deduplicate alerts
        if should_exit and market_id not in self._exited:
            self._exited.add(market_id)
            logger.warning(
                f"AUTO-EXIT: {market_id} adverse move {adverse_pct:.1%} "
                f"(entry={entry:.2f}, current={current:.2f})"
            )
            await self._fire_callbacks(alert)
            return alert
        elif should_alert and market_id not in self._alerted:
            self._alerted.add(market_id)
            logger.warning(
                f"ADVERSE ALERT: {market_id} moved {adverse_pct:.1%} against position "
                f"(entry={entry:.2f}, current={current:.2f})"
            )
            await self._fire_callbacks(alert)
            return alert

        return None

    async def _fire_callbacks(self, alert: AdverseMoveAlert):
        for cb in self._callbacks:
            try:
                await cb(alert)
            except Exception as e:
                logger.error(f"Adverse move callback failed: {e}", exc_info=True)

    def reset_alerts(self, market_id: str):
        """Reset alert state for a market (e.g., after manual review)."""
        self._alerted.discard(market_id)
        self._exited.discard(market_id)

    @property
    def monitored_count(self) -> int:
        return len(self._positions)
