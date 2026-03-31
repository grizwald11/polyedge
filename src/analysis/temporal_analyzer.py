"""Price History Temporal Analysis — detects momentum, mean reversion, and "already priced in".

Uses the existing market_snapshots table to analyze price history and adjust
edge calculations. The #1 source of false signals is "already priced in" —
Claude correctly identifies an outcome as likely, but the market price has
already moved most of the way there, leaving insufficient remaining edge.

This module provides temporal signals that discount edge when the price trend
already favors the signal direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Default lookback periods
DEFAULT_LOOKBACK_HOURS = 168  # 7 days
MEAN_REVERSION_HOURS = 48    # 2 days for mean reversion window

# Minimum number of snapshots needed for meaningful analysis
MIN_SNAPSHOTS_FOR_MOMENTUM = 6
MIN_SNAPSHOTS_FOR_MEAN_REVERSION = 4


@dataclass
class TemporalSignals:
    """Temporal analysis results for a market."""

    momentum_7d: float = 0.0
    """Linear regression slope of price over 7 days, normalized to [-1, 1].
    Positive = price trending up (YES more likely).
    Negative = price trending down (NO more likely)."""

    volatility_7d: float = 0.0
    """Standard deviation of price changes over 7 days."""

    already_priced_in_pct: float = 0.0
    """Fraction (0-1) of the expected price movement already captured.
    0.0 = no movement yet, 1.0 = fully priced in."""

    mean_reversion_score: float = 0.0
    """Z-score of current price vs 48h rolling mean.
    >2.0 = likely overbought (may revert down).
    <-2.0 = likely oversold (may revert up)."""

    price_7d_ago: Optional[float] = None
    """Price at the start of the lookback period, if available."""

    snapshot_count: int = 0
    """Number of snapshots used for the analysis."""

    has_sufficient_data: bool = False
    """Whether enough snapshots exist for meaningful analysis."""


class TemporalAnalyzer:
    """Analyzes price history to detect temporal patterns."""

    def __init__(self, discount_weight: float = 0.7):
        """Initialize with configurable discount weight.

        Args:
            discount_weight: How aggressively to discount edge based on
                "already priced in" signal. 0.0 = no discount, 1.0 = full discount.
                Default 0.7 provides moderate discounting.
        """
        self.discount_weight = discount_weight

    def analyze(
        self,
        market_id: str,
        current_price: float,
        claude_estimate: float,
        db,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    ) -> TemporalSignals:
        """Analyze price history for a market.

        Args:
            market_id: Market ticker
            current_price: Current YES price
            claude_estimate: Claude's probability estimate
            db: Database instance with get_snapshots_for_market method
            lookback_hours: How far back to look

        Returns:
            TemporalSignals with all computed metrics
        """
        signals = TemporalSignals()

        # Fetch price history
        snapshots = self._get_price_history(db, market_id, lookback_hours)
        signals.snapshot_count = len(snapshots)

        if len(snapshots) < MIN_SNAPSHOTS_FOR_MEAN_REVERSION:
            logger.debug(
                f"Temporal: {market_id} has only {len(snapshots)} snapshots, "
                f"need {MIN_SNAPSHOTS_FOR_MEAN_REVERSION} — skipping analysis"
            )
            return signals

        signals.has_sufficient_data = True
        prices = [s["yes_price"] for s in snapshots]

        # Extract price from lookback start
        signals.price_7d_ago = prices[0]

        # Compute momentum (linear regression slope)
        if len(prices) >= MIN_SNAPSHOTS_FOR_MOMENTUM:
            signals.momentum_7d = self._compute_momentum(prices)

        # Compute volatility
        signals.volatility_7d = self._compute_volatility(prices)

        # Compute "already priced in" percentage
        signals.already_priced_in_pct = self._compute_already_priced_in(
            current_price, signals.price_7d_ago, claude_estimate,
        )

        # Compute mean reversion score (z-score)
        recent_prices = prices[-max(MIN_SNAPSHOTS_FOR_MEAN_REVERSION, len(prices) // 3):]
        signals.mean_reversion_score = self._compute_mean_reversion(
            current_price, recent_prices,
        )

        logger.debug(
            f"Temporal {market_id}: momentum={signals.momentum_7d:.3f}, "
            f"vol={signals.volatility_7d:.3f}, priced_in={signals.already_priced_in_pct:.1%}, "
            f"mean_rev={signals.mean_reversion_score:.2f}, "
            f"price_7d_ago={signals.price_7d_ago:.2f}"
        )

        return signals

    def compute_edge_discount(self, signals: TemporalSignals) -> float:
        """Compute a multiplicative discount factor for edge based on temporal signals.

        Returns a value in [0.1, 1.0] where:
        - 1.0 = no discount (price hasn't moved toward our estimate)
        - 0.1 = heavy discount (price has already moved most of the way)

        Args:
            signals: TemporalSignals from analyze()

        Returns:
            Discount factor to multiply the raw edge by
        """
        if not signals.has_sufficient_data:
            return 1.0  # No discount if insufficient data

        # Primary discount: "already priced in"
        # If 80% of the expected move has happened, discount edge by 80% * weight
        api_discount = 1.0 - (signals.already_priced_in_pct * self.discount_weight)

        # Secondary: strong momentum aligned with our signal adds a small penalty
        # (we may be late to the party)
        momentum_penalty = 1.0
        if abs(signals.momentum_7d) > 0.3:
            # Strong momentum — apply a mild penalty (5-15%)
            momentum_penalty = max(0.85, 1.0 - abs(signals.momentum_7d) * 0.15)

        discount = max(0.1, api_discount * momentum_penalty)

        return discount

    @staticmethod
    def _get_price_history(db, market_id: str, lookback_hours: int) -> list[dict]:
        """Fetch price snapshots from the database."""
        start = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
        return db.get_snapshots_for_market(market_id, start=start)

    @staticmethod
    def _compute_momentum(prices: list[float]) -> float:
        """Compute normalized linear regression slope of prices.

        Returns value in approximately [-1, 1] range.
        """
        n = len(prices)
        if n < 2:
            return 0.0

        # Simple linear regression: y = a + b*x
        # where x is normalized time index [0, 1]
        x_mean = (n - 1) / 2.0
        y_mean = sum(prices) / n

        numerator = sum((i - x_mean) * (p - y_mean) for i, p in enumerate(prices))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        # Raw slope (change per time step)
        slope = numerator / denominator

        # Normalize by price range to get [-1, 1] scale
        price_range = max(prices) - min(prices)
        if price_range < 0.001:
            return 0.0

        # Normalize: slope * n_steps / price_range gives relative direction
        normalized = (slope * n) / price_range
        return max(-1.0, min(1.0, normalized))

    @staticmethod
    def _compute_volatility(prices: list[float]) -> float:
        """Compute standard deviation of price changes."""
        if len(prices) < 2:
            return 0.0

        changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        if not changes:
            return 0.0

        mean_change = sum(changes) / len(changes)
        variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
        return variance ** 0.5

    @staticmethod
    def _compute_already_priced_in(
        current_price: float,
        price_7d_ago: float,
        claude_estimate: float,
    ) -> float:
        """Compute what fraction of the expected move has already happened.

        If Claude says 85% and market moved from 60% to 80%, then
        20/25 = 80% of the expected move is already priced in.

        Returns:
            Float in [0.0, 1.0]. 0 = no movement, 1 = fully priced in.
        """
        total_expected = claude_estimate - price_7d_ago
        if abs(total_expected) < 0.01:
            # Claude's estimate is essentially where the price started —
            # no expected movement, so nothing is "priced in"
            return 0.0

        price_movement = current_price - price_7d_ago

        # Check if price moved in the same direction as Claude's estimate
        if total_expected > 0 and price_movement <= 0:
            return 0.0  # Price moved opposite to expectation
        if total_expected < 0 and price_movement >= 0:
            return 0.0  # Price moved opposite to expectation

        ratio = price_movement / total_expected
        return max(0.0, min(1.0, ratio))

    @staticmethod
    def _compute_mean_reversion(
        current_price: float,
        recent_prices: list[float],
    ) -> float:
        """Compute z-score of current price vs recent mean.

        Returns:
            Z-score. >2 = overbought, <-2 = oversold.
        """
        if len(recent_prices) < 2:
            return 0.0

        mean = sum(recent_prices) / len(recent_prices)
        variance = sum((p - mean) ** 2 for p in recent_prices) / len(recent_prices)
        std = variance ** 0.5

        if std < 0.001:
            return 0.0

        return (current_price - mean) / std
