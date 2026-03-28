"""Kelly position sizer — half-Kelly with caps.

Calculates optimal position size based on edge and probability,
then applies half-Kelly fraction and hard caps.
"""

from __future__ import annotations

import logging
import math

from src.config import Settings

logger = logging.getLogger(__name__)


# Brier score thresholds for calibration-based sizing
# Note: 0.25 = random guessing — must reduce aggressively at/above that
BRIER_EXCELLENT = 0.10  # Full sizing
BRIER_GOOD = 0.18       # Full sizing
BRIER_FAIR = 0.22       # Reduce to 50%
BRIER_POOR = 0.28       # Reduce to 25%
# Above 0.28 → reduce to 10%


class KellySizer:
    """Half-Kelly position sizing with configurable caps."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # Event/political markets on Kalshi are fee-free. Only apply fees
        # for fee-enabled categories (crypto, sports). Default to 0 since
        # our target categories are all fee-free.
        self.fee_rate = 0.0
        self._fee_rate_if_enabled = 0.0175 if settings.trading.prefer_maker else 0.07
        self._calibration_multiplier: float = 1.0

    def calculate_position_size(
        self,
        edge: float,
        probability: float,
        bankroll: float,
        current_exposure: float = 0.0,
        order_price: float | None = None,
    ) -> int:
        """Calculate optimal number of contracts to buy.

        Uses half-Kelly formula with hard caps:
        - Max position = bankroll * max_position_pct
        - Reduced if near total exposure limit

        Args:
            edge: Our probability - market price (positive = favorable)
            probability: Our estimated true probability
            bankroll: Total bankroll in dollars
            current_exposure: Current total exposure in dollars
            order_price: Actual price per contract for the order. If None,
                derived from probability - edge. Use this to ensure the
                contract count stays within dollar caps when the order price
                differs from the Kelly-derived market price.

        Returns:
            Number of contracts (integers, minimum 1 if any edge exists)
        """
        # Validate inputs — NaN/infinity can propagate from upstream division
        # by zero or malformed API responses and would corrupt sizing.
        if (
            not math.isfinite(edge) or not math.isfinite(probability)
            or not math.isfinite(bankroll) or not math.isfinite(current_exposure)
        ):
            logger.warning(
                f"Kelly: non-finite input detected (edge={edge}, prob={probability}, "
                f"bankroll={bankroll}, exposure={current_exposure}) — returning 0"
            )
            return 0
        if edge <= 0 or probability <= 0 or probability >= 1 or bankroll <= 0:
            return 0

        # Kelly fraction: f = (p * b - q) / b
        # where p = probability of winning, q = 1-p, b = odds (payout ratio)
        # For binary markets: b = (1 - market_price) / market_price
        # market_price = probability - edge (approx)
        market_price = probability - edge
        if market_price <= 0 or market_price >= 0.99:
            logger.debug(
                f"Kelly: invalid market_price={market_price:.3f} "
                f"(prob={probability:.3f}, edge={edge:.3f}) — skipping"
            )
            return 0

        # Reject cheap contracts (<$0.10): tiny absolute moves wipe out
        # the position, and Kelly produces huge contract counts that amplify losses.
        # $0.05 was too low — contracts at $0.05-$0.10 still caused major losses
        # (KXDHSFUND at $0.05, KXTRUMPADMINLEAVE at $0.15, etc.).
        cost_price_check = order_price if order_price and order_price > 0 else market_price
        if cost_price_check < 0.10:
            logger.debug(
                f"Kelly: rejecting ultra-cheap contract @ ${cost_price_check:.2f} "
                f"(prob={probability:.3f}, edge={edge:.3f})"
            )
            return 0

        # Payout if win: (1 - market_price) per contract
        # Risk if lose: market_price per contract
        b = (1.0 - market_price) / market_price  # odds

        q = 1.0 - probability
        kelly_fraction = (probability * b - q) / b

        if kelly_fraction <= 0:
            return 0

        # Apply half-Kelly
        half_kelly = kelly_fraction * self.settings.trading.kelly_fraction

        # Dollar amount to risk
        kelly_dollars = half_kelly * bankroll

        # Cap 1: Max position percentage
        max_position = bankroll * self.settings.trading.max_position_pct
        kelly_dollars = min(kelly_dollars, max_position)

        # Cap 2: Don't exceed remaining exposure room
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        remaining = max_total - current_exposure
        if remaining <= 0:
            logger.info(
                f"Kelly: exposure cap reached (${current_exposure:.2f} / "
                f"${max_total:.2f}) — no room for new trades"
            )
            return 0
        kelly_dollars = min(kelly_dollars, remaining)

        # Convert dollars to contracts using the actual order price so that
        # contracts * price never exceeds the dollar cap.
        cost_price = order_price if order_price and order_price > 0 else market_price
        contracts = int(kelly_dollars / cost_price) if cost_price > 0 else 0

        # Hard check: ensure contracts * cost_price doesn't exceed position cap
        if contracts > 0 and contracts * cost_price > max_position:
            contracts = int(max_position / cost_price)
            logger.debug(f"Kelly: clamped contracts to {contracts} (position cap ${max_position:.2f})")

        # Account for estimated fee so total cost stays within cap.
        # Fee formula returns cents: ceil(fee_rate * contracts * price * (1 - price))
        # Convert to dollars before comparing.  Loop because removing one
        # contract changes the fee, and a single decrement may not suffice
        # for high-fee expensive contracts.
        if contracts > 0 and 0 < cost_price < 1:
            while contracts > 0:
                fee_cents = math.ceil(self.fee_rate * contracts * cost_price * (1.0 - cost_price))
                fee_dollars = fee_cents / 100.0
                if contracts * cost_price + fee_dollars <= kelly_dollars:
                    break
                contracts -= 1

        # Minimum 1 contract if we have any edge and room,
        # but only if the single contract cost + fee stays within kelly_dollars.
        if contracts == 0 and kelly_fraction > 0 and remaining >= cost_price:
            fee_cents = math.ceil(self.fee_rate * 1 * cost_price * (1.0 - cost_price)) if 0 < cost_price < 1 else 0
            fee_dollars = fee_cents / 100.0
            if cost_price + fee_dollars <= kelly_dollars:
                contracts = 1

        # Apply calibration-based multiplier — reduce sizing when forecasting is poor.
        # For multi-contract positions, scale down. For single-contract positions
        # with very poor calibration (≤25%), skip entirely to protect capital.
        if self._calibration_multiplier < 1.0 and contracts > 0:
            scaled = int(contracts * self._calibration_multiplier)
            if scaled == 0 and self._calibration_multiplier <= 0.25:
                # Very poor calibration — don't trade at all
                return 0
            contracts = max(1, scaled) if contracts > 1 else contracts

        logger.debug(
            f"Kelly sizing: edge={edge:.1%}, prob={probability:.1%}, "
            f"kelly_f={kelly_fraction:.3f}, half={half_kelly:.3f}, "
            f"${kelly_dollars:.2f} → {contracts} contracts @ ${cost_price:.2f}"
            f" (cal_mult={self._calibration_multiplier:.2f})"
        )

        return contracts

    def update_calibration_multiplier(self, brier_score: float | None) -> None:
        """Adjust sizing multiplier based on overall Brier score.

        When calibration is poor, automatically reduces position sizes
        to protect capital until forecasting accuracy improves.

        Args:
            brier_score: Overall Brier score (0=perfect, 0.25=random). None = no data.
        """
        if brier_score is None:
            self._calibration_multiplier = 1.0
            return

        if brier_score <= BRIER_GOOD:
            mult = 1.0
        elif brier_score <= BRIER_FAIR:
            mult = 0.50
        elif brier_score <= BRIER_POOR:
            mult = 0.25
        else:
            mult = 0.10

        if mult != self._calibration_multiplier:
            logger.info(
                f"Kelly calibration multiplier: {self._calibration_multiplier:.2f} → {mult:.2f} "
                f"(Brier={brier_score:.3f})"
            )
        self._calibration_multiplier = mult

    @property
    def calibration_multiplier(self) -> float:
        return self._calibration_multiplier
