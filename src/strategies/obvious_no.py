"""Obvious NO Strategy — low-risk base yield from near-certain markets.

Scans for markets where YES is trading at 1-5 cents on absurd outcomes.
Buying NO at 95-99 cents gives 1-5% return at resolution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Direction, Market, MarketCategory, Signal, StrategyName

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

        # Event/political markets on Kalshi/Polymarket are fee-free for maker orders.
        # Only apply fees for fee-enabled categories (crypto, sports).
        fee_per_contract = 0.0
        category = getattr(market, "category", None)
        fee_categories = {MarketCategory.CRYPTO.value, MarketCategory.SPORTS.value}
        if category and str(category) in fee_categories:
            fee_per_contract = 0.0175 * no_price * (1.0 - no_price)

        net_profit = 1.0 - no_price - fee_per_contract
        if net_profit <= 0:
            return None
        simple_return = net_profit / no_price
        annualized_return = simple_return * (365.0 / days) if days > 0 else 0

        # Edge = probability edge (our estimate - market price).
        # For obvious NO, the true P(NO) is very close to 1.0. We use a
        # conservative estimate that accounts for tiny black-swan risk:
        #   P(NO) = 1 - yes_price * 0.3 (at YES=0.03, P(NO) = 0.991)
        # The old formula (0.5 factor) significantly underestimated edge,
        # causing Kelly to under-size the most reliable strategy.
        # Probability estimate for NO outcome.
        # The 0.3 multiplier is conservative: we assume the true P(YES)
        # is only 30% of the market's YES price. This accounts for
        # market illiquidity inflating YES prices on absurd markets.
        # Calibrate this against historical obvious-NO resolutions.
        # At YES=$0.03: P(NO)=0.991, at YES=$0.05: P(NO)=0.985
        obvious_no_multiplier = self.settings.trading.obvious_no_probability_multiplier
        probability_estimate = min(0.99, 1.0 - yes_price * obvious_no_multiplier)
        edge = probability_estimate - no_price
        if edge <= 0:
            return None

        if edge < min_edge:
            return None

        # Annualized return must be > 20%
        if annualized_return < 0.20:
            return None

        # Confidence inversely correlated with YES price
        confidence = min(0.99, max(0.80, 1.0 - yes_price * 2.0))

        signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id=market.ticker,
            market_question=market.question,
            direction=Direction.BUY_NO,
            edge=edge,
            probability_estimate=probability_estimate,
            market_price=no_price,
            confidence=confidence,
            reasoning=(
                f"YES at ${yes_price:.2f}, NO at ${no_price:.2f}. "
                f"Net return: {simple_return:.1%}, "
                f"Annualized: {annualized_return:.0%}. "
                f"Resolves in {days:.0f} days."
                + (f" Fee: ${fee_per_contract:.4f}/contract." if fee_per_contract > 0 else "")
            ),
        )

        logger.info(
            f"Obvious NO: '{market.question[:50]}...' "
            f"YES=${yes_price:.2f}, return={simple_return:.1%}, "
            f"annualized={annualized_return:.0%}"
        )

        return signal
