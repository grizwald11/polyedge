"""Position manager — tracks open positions, P&L, and exposure.

Aggregates trades into positions and provides portfolio-level metrics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import (
    Direction, Order, OrderStatus, Position, Side, StrategyName, Trade,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)


class PositionManager:
    """Tracks all open positions and portfolio metrics."""

    def __init__(self, db: Database, bankroll: float = 500.0):
        self.db = db
        self.bankroll = bankroll
        self._positions: dict[str, Position] = {}  # market_id -> Position

    def update_from_trade(self, trade: Trade, market_question: str = "") -> Position:
        """Update positions based on a new trade fill.

        Args:
            trade: The completed trade
            market_question: Human-readable market question

        Returns:
            Updated position
        """
        key = trade.market_id
        existing = self._positions.get(key)

        if existing is None:
            # New position
            direction = self._direction_from_trade(trade)
            position = Position(
                market_id=trade.market_id,
                market_question=market_question,
                token_id=trade.token_id,
                direction=direction,
                size=trade.size,
                avg_entry_price=trade.price,
                current_price=trade.price,
                unrealized_pnl=0.0,
                strategy=trade.strategy,
                paper=trade.paper,
                opened_at=trade.timestamp,
                last_updated=trade.timestamp,
            )
            self._positions[key] = position
            logger.info(
                f"Opened position: {direction.value} {trade.size:.0f}x "
                f"{trade.market_id} @ ${trade.price:.2f}"
            )
            return position
        else:
            # Update existing position
            if trade.side == Side.BUY:
                # Adding to position — weighted average entry
                total_cost = existing.avg_entry_price * existing.size + trade.price * trade.size
                existing.size += trade.size
                existing.avg_entry_price = total_cost / existing.size if existing.size > 0 else 0
            else:
                # Reducing position
                existing.size -= trade.size
                if existing.size <= 0:
                    # Position closed
                    self._positions.pop(key, None)
                    logger.info(f"Closed position: {trade.market_id}")
                    return existing

            existing.last_updated = trade.timestamp
            logger.info(
                f"Updated position: {existing.direction.value} {existing.size:.0f}x "
                f"{trade.market_id} @ avg ${existing.avg_entry_price:.2f}"
            )
            return existing

    def update_price(self, market_id: str, current_price: float):
        """Update current price and recalculate unrealized P&L."""
        position = self._positions.get(market_id)
        if position is None:
            return

        position.current_price = current_price
        # P&L = (current - entry) * size for BUY, (entry - current) * size for SELL
        if position.direction in (Direction.BUY_YES, Direction.BUY_NO):
            position.unrealized_pnl = (current_price - position.avg_entry_price) * position.size
        else:
            position.unrealized_pnl = (position.avg_entry_price - current_price) * position.size
        position.last_updated = datetime.now(timezone.utc)

    def get_position(self, market_id: str) -> Optional[Position]:
        """Get position for a specific market."""
        return self._positions.get(market_id)

    def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        return list(self._positions.values())

    def get_total_exposure(self) -> float:
        """Total capital deployed across all positions."""
        return sum(p.cost_basis for p in self._positions.values())

    def get_total_exposure_pct(self) -> float:
        """Total exposure as percentage of bankroll."""
        if self.bankroll <= 0:
            return 0.0
        return self.get_total_exposure() / self.bankroll

    def get_total_unrealized_pnl(self) -> float:
        """Sum of unrealized P&L across all positions."""
        return sum(p.unrealized_pnl for p in self._positions.values())

    def get_strategy_exposure(self, strategy: StrategyName) -> float:
        """Total exposure for a specific strategy."""
        return sum(
            p.cost_basis for p in self._positions.values()
            if p.strategy == strategy
        )

    def has_position(self, market_id: str) -> bool:
        """Check if we already have a position in this market."""
        return market_id in self._positions

    def get_position_count(self) -> int:
        """Number of open positions."""
        return len(self._positions)

    def _direction_from_trade(self, trade: Trade) -> Direction:
        """Infer direction from trade side and token."""
        is_yes = "yes" in trade.token_id.lower()
        if trade.side == Side.BUY:
            return Direction.BUY_YES if is_yes else Direction.BUY_NO
        else:
            return Direction.SELL_YES if is_yes else Direction.SELL_NO
