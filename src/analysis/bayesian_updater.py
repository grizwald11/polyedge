"""Bayesian Position Updating — lightweight belief updates between Claude re-assessments.

Once a position is entered, the probability estimate is frozen until the next
full Claude re-assessment (up to 24 hours). This module provides lightweight
Bayesian updates based on market price movements and community forecast shifts,
catching edge evaporation (information priced in) and edge increases (market
lags new info) faster than waiting for full re-assessment.

Full Claude re-assessment is triggered when the Bayesian posterior shifts
significantly (>10%) from the prior, indicating substantial new information.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Default market efficiency parameter: how much to trust market price movements
# as signals of true probability. 0.0 = ignore market entirely, 1.0 = fully efficient.
DEFAULT_MARKET_EFFICIENCY = 0.7

# Thresholds for triggering actions
REASSESS_THRESHOLD = 0.10  # Trigger full Claude re-assessment if posterior shifts >10%
EXIT_EDGE_MINIMUM = 0.02   # If remaining edge drops below 2%, suggest exit


@dataclass
class BeliefState:
    """Tracks the evolving probability belief for an open position."""

    market_id: str
    probability: float  # Current best estimate of true probability
    last_claude_assessment: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_update: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    update_count: int = 0
    entry_price: float = 0.0  # Price at which position was entered
    direction_is_yes: bool = True  # True if we bought YES, False if we bought NO


@dataclass
class UpdateResult:
    """Result of a Bayesian update."""

    prior: float
    posterior: float
    shift: float  # posterior - prior
    reason: str
    should_reassess: bool = False  # True if shift is large enough for full Claude re-assessment
    should_exit: bool = False      # True if edge has evaporated
    exit_reason: str = ""


class BayesianUpdater:
    """Lightweight Bayesian belief updates for open positions."""

    def __init__(
        self,
        market_efficiency: float = DEFAULT_MARKET_EFFICIENCY,
        reassess_threshold: float = REASSESS_THRESHOLD,
        min_edge: float = EXIT_EDGE_MINIMUM,
    ):
        self.market_efficiency = market_efficiency
        self.reassess_threshold = reassess_threshold
        self.min_edge = min_edge

    def update_from_price_movement(
        self,
        belief: BeliefState,
        new_market_price: float,
        old_market_price: float,
    ) -> UpdateResult:
        """Update belief based on market price movement.

        The market price is treated as a noisy signal of true probability.
        The posterior is a weighted average of the prior belief and the new
        market price, where the weight is the market_efficiency parameter.

        Args:
            belief: Current belief state
            new_market_price: Current market YES price
            old_market_price: Previous market YES price (from last update)

        Returns:
            UpdateResult with posterior and action recommendations
        """
        prior = belief.probability
        price_delta = new_market_price - old_market_price

        if abs(price_delta) < 0.005:
            # Negligible price movement — no update needed
            return UpdateResult(
                prior=prior,
                posterior=prior,
                shift=0.0,
                reason="negligible price movement",
            )

        # Bayesian-style update: blend prior with market signal
        # posterior = (1 - efficiency) * prior + efficiency * new_market_price
        posterior = (
            (1.0 - self.market_efficiency) * prior
            + self.market_efficiency * new_market_price
        )
        posterior = max(0.01, min(0.99, posterior))

        shift = posterior - prior

        # Update belief state
        belief.probability = posterior
        belief.update_count += 1
        belief.last_update = datetime.now(timezone.utc)

        # Check if we should trigger actions
        should_reassess = abs(shift) >= self.reassess_threshold
        should_exit, exit_reason = self._check_exit(belief, posterior)

        reason = (
            f"price moved {old_market_price:.2f}→{new_market_price:.2f} "
            f"(Δ={price_delta:+.3f}), posterior={posterior:.3f}"
        )

        return UpdateResult(
            prior=prior,
            posterior=posterior,
            shift=shift,
            reason=reason,
            should_reassess=should_reassess,
            should_exit=should_exit,
            exit_reason=exit_reason,
        )

    def update_from_community_shift(
        self,
        belief: BeliefState,
        old_community: float,
        new_community: float,
        community_weight: float = 0.3,
    ) -> UpdateResult:
        """Update belief based on community forecast shift.

        Community forecasts (Manifold, Metaculus) are a weaker signal than
        market prices, so they get a lower weight.

        Args:
            belief: Current belief state
            old_community: Previous community forecast
            new_community: New community forecast
            community_weight: How much to weight the community shift (0-1)

        Returns:
            UpdateResult with posterior and action recommendations
        """
        prior = belief.probability
        community_delta = new_community - old_community

        if abs(community_delta) < 0.02:
            return UpdateResult(
                prior=prior,
                posterior=prior,
                shift=0.0,
                reason="negligible community shift",
            )

        # Blend: move toward the community signal proportionally
        posterior = prior + community_delta * community_weight
        posterior = max(0.01, min(0.99, posterior))

        shift = posterior - prior

        belief.probability = posterior
        belief.update_count += 1
        belief.last_update = datetime.now(timezone.utc)

        should_reassess = abs(shift) >= self.reassess_threshold
        should_exit, exit_reason = self._check_exit(belief, posterior)

        reason = (
            f"community shifted {old_community:.2f}→{new_community:.2f} "
            f"(Δ={community_delta:+.3f}), posterior={posterior:.3f}"
        )

        return UpdateResult(
            prior=prior,
            posterior=posterior,
            shift=shift,
            reason=reason,
            should_reassess=should_reassess,
            should_exit=should_exit,
            exit_reason=exit_reason,
        )

    def _check_exit(
        self,
        belief: BeliefState,
        posterior: float,
    ) -> tuple[bool, str]:
        """Check if the position should be exited based on updated belief.

        Returns:
            Tuple of (should_exit, reason)
        """
        # Calculate remaining edge
        if belief.direction_is_yes:
            remaining_edge = posterior - belief.entry_price
        else:
            remaining_edge = (1.0 - posterior) - (1.0 - belief.entry_price)
            # Simplified: remaining_edge = belief.entry_price - posterior

        if remaining_edge < self.min_edge:
            if remaining_edge <= 0:
                return True, f"edge flipped: remaining_edge={remaining_edge:+.3f}"
            else:
                return True, f"edge below minimum: remaining_edge={remaining_edge:.3f} < {self.min_edge}"

        return False, ""
