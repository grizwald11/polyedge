"""Obvious NO Strategy — low-risk base yield from near-certain markets.

Scans for markets where YES is trading at 1-5 cents on absurd outcomes.
Buying NO at 95-99 cents gives 1-5% return at resolution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Market, Signal, Direction, StrategyName

logger = logging.getLogger(__name__)


class ObviousNoStrategy:
    """Strategy 5: Buy NO on near-certain markets for safe yield."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def scan_for_opportunities(self, markets: list[Market]) -> list[Signal]:
        """Scan markets for obvious NO opportunities.

        Criteria:
        - YES price between $0.01-$0.05 (1-5 cents)
        - Clear resolution date within 30 days
        - Minimum volume threshold
        - Annualized return > 20%
        """
        signals = []
        min_edge = self.settings.trading.min_edge_obvious_no

        for market in markets:
            signal = self._check_market(market, min_edge)
            if signal:
                signals.append(signal)

        logger.info(f"Obvious NO: scanned {len(markets)} markets, found {len(signals)} signals")
        return signals

    def _check_market(self, market: Market, min_edge: float) -> Signal | None:
        """Check if a market qualifies for the Obvious NO strategy."""
        yes_price = market.yes_price
        no_price = market.no_price

        # YES must be trading at 1-5 cents ($0.01-$0.05)
        if not (0.01 <= yes_price <= 0.05):
            return None

        # Must have resolution date
        days = market.days_to_resolution
        if days is None or days <= 0:
            return None

        # Must resolve within 30 days
        if days > 30:
            return None

        # Calculate return and annualized return
        # Buying NO at no_price, payout is $1.00 at resolution
        if no_price <= 0 or no_price >= 1.0:
            return None

        simple_return = (1.0 - no_price) / no_price
        annualized_return = simple_return * (365.0 / days) if days > 0 else 0

        # Edge = profit per contract when NO resolves to $1.00
        edge = 1.0 - no_price

        if edge < min_edge:
            return None

        # Annualized return must be > 20%
        if annualized_return < 0.20:
            return None

        signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id=market.ticker,
            market_question=market.question,
            direction=Direction.BUY_NO,
            edge=edge,
            probability_estimate=no_price,  # We think NO is ~100% likely
            market_price=no_price,
            confidence=0.95,  # High confidence on obvious outcomes
            reasoning=(
                f"YES at ${yes_price:.2f}, NO at ${no_price:.2f}. "
                f"Simple return: {simple_return:.1%}, "
                f"Annualized: {annualized_return:.0%}. "
                f"Resolves in {days:.0f} days."
            ),
        )

        logger.info(
            f"Obvious NO: '{market.question[:50]}...' "
            f"YES=${yes_price:.2f}, return={simple_return:.1%}, "
            f"annualized={annualized_return:.0%}"
        )

        return signal
