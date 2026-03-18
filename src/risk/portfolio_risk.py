"""Portfolio risk — cross-position correlation tracking.

Groups positions by event_ticker (Kalshi event grouping) to detect correlated
exposure. Markets in the same event are treated as fully correlated.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.models import MarketCategory, StrategyName
from src.execution.position_manager import PositionManager
from src.storage.database import Database

logger = logging.getLogger(__name__)


class PortfolioRisk:
    """Cross-position correlation and diversification tracking."""

    def __init__(self, position_manager: PositionManager, db: Database):
        self.positions = position_manager
        self.db = db

    def get_correlated_exposure(self, market_id: str) -> float:
        """Get total exposure correlated with a market.

        Markets in the same Kalshi event (same event_ticker) are fully correlated.
        Returns total cost_basis of positions in the same event.
        """
        event_ticker = self._get_event_ticker(market_id)
        if not event_ticker:
            return 0.0

        total = 0.0
        for pos in self.positions.get_all_positions():
            pos_event = self._get_event_ticker(pos.market_id)
            if pos_event == event_ticker:
                total += pos.cost_basis

        return total

    def get_event_exposure(self, event_ticker: str) -> float:
        """Get total exposure for all positions in an event."""
        total = 0.0
        for pos in self.positions.get_all_positions():
            pos_event = self._get_event_ticker(pos.market_id)
            if pos_event == event_ticker:
                total += pos.cost_basis
        return total

    def get_category_exposure(self, category: str) -> float:
        """Get total exposure for a market category."""
        total = 0.0
        for pos in self.positions.get_all_positions():
            market_data = self.db.get_market(pos.market_id)
            if market_data and market_data.get("category") == category:
                total += pos.cost_basis
        return total

    def check_diversification(self, bankroll: float) -> list[str]:
        """Check portfolio diversification and return warnings.

        Args:
            bankroll: Total bankroll for percentage calculations

        Returns:
            List of warning strings
        """
        warnings: list[str] = []
        if bankroll <= 0:
            return warnings

        # Check event concentration
        event_exposure: dict[str, float] = {}
        for pos in self.positions.get_all_positions():
            event = self._get_event_ticker(pos.market_id) or pos.market_id
            event_exposure[event] = event_exposure.get(event, 0) + pos.cost_basis

        for event, exposure in event_exposure.items():
            pct = exposure / bankroll
            if pct > 0.15:
                warnings.append(
                    f"High event concentration: {event} = {pct:.0%} of bankroll"
                )

        # Check strategy concentration
        strategy_exposure: dict[str, float] = {}
        for pos in self.positions.get_all_positions():
            strat = pos.strategy.value
            strategy_exposure[strat] = strategy_exposure.get(strat, 0) + pos.cost_basis

        for strat, exposure in strategy_exposure.items():
            pct = exposure / bankroll
            if pct > 0.25:
                warnings.append(
                    f"High strategy concentration: {strat} = {pct:.0%} of bankroll"
                )

        return warnings

    def _get_event_ticker(self, market_id: str) -> Optional[str]:
        """Look up the event_ticker for a market from the database."""
        market_data = self.db.get_market(market_id)
        if market_data:
            return market_data.get("event_ticker") or None
        return None
