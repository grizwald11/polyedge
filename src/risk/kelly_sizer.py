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
# M-11: Closed gap between GOOD and FAIR (was 0.18/0.22, now 0.18/0.20)
# to add a MEDIOCRE band at 0.20-0.25 with 75% sizing.
BRIER_EXCELLENT = 0.10  # 110% sizing (reward)
BRIER_GOOD = 0.18       # 100% sizing
BRIER_FAIR = 0.20       # 75% sizing (M-11: was 0.22, closed gap)
BRIER_MEDIOCRE = 0.25   # 50% sizing (M-11: new band)
BRIER_POOR = 0.30       # 25% sizing (M-11: was 0.28, aligned with random=0.25)
# Above 0.30 → halt trading (0% sizing)


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
        self._circuit_breaker_multiplier: float = 1.0

    def calculate_position_size(
        self,
        edge: float,
        probability: float,
        bankroll: float,
        current_exposure: float = 0.0,
        order_price: float | None = None,
        market_liquidity: float | None = None,
        fee_rate: float = 0.0,
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
            market_liquidity: Total order book depth in dollars. Used to reduce
                position size when the order would be large relative to
                available liquidity (partial fill risk — H-11).
            fee_rate: Fee rate for the market (0.0 for fee-free event markets,
                ~0.0175 for maker orders in fee-enabled markets). Defaults to
                0.0 for backward compatibility — callers should pass the
                appropriate rate based on market type.

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
        if market_price <= 0 or market_price >= 1.0:
            logger.debug(
                f"Kelly: invalid market_price={market_price:.3f} "
                f"(prob={probability:.3f}, edge={edge:.3f}) — skipping"
            )
            return 0

        # Risk-based check for cheap contracts. Blanket rejection at $0.10 was
        # too aggressive — it excluded valid high-edge trades in the $0.03-$0.10
        # range. Instead, use tiered rules:
        #   < $0.03: always reject (too volatile for reliable sizing)
        #   $0.03-$0.10: require 10% edge (higher bar to compensate for volatility)
        #   >= $0.10: normal min-edge checks apply downstream
        cost_price_check = order_price if order_price and order_price > 0 else market_price
        if cost_price_check < 0.03:
            logger.debug("Price below $0.03 — too volatile for reliable sizing")
            return 0
        # For $0.03-$0.10 range, require higher edge (10% instead of 5%)
        if cost_price_check < 0.10 and edge < 0.10:
            logger.debug(
                f"Low-price contract ({cost_price_check:.2f}) requires 10% edge, got {edge:.1%}"
            )
            return 0
        # Contracts above $0.97 have tiny upside but full downside if the
        # market flips — same risk profile as cheap contracts. Require 10%
        # edge to compensate for the asymmetric payoff (H-1).
        if cost_price_check > 0.97 and edge < 0.10:
            logger.debug(
                f"High-price contract ({cost_price_check:.2f}) requires 10% edge, got {edge:.1%}"
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

        # Use the actual order price for contract conversion so that
        # contracts * price never exceeds the dollar cap.
        cost_price = order_price if order_price and order_price > 0 else market_price

        # Step 1: Apply LIQUIDITY adjustment BEFORE position/exposure caps.
        # This ensures the risk engine sees the true post-liquidity order size
        # rather than the raw Kelly amount. Without this, the caps are checked
        # against an inflated figure and the liquidity reduction happens too late.
        if market_liquidity is not None and market_liquidity > 0 and kelly_dollars > 0:
            # Estimate raw contract count to compute book impact
            raw_contracts = int(kelly_dollars / cost_price) if cost_price > 0 else 0
            if raw_contracts > 0:
                order_pct_of_book = (raw_contracts * cost_price) / market_liquidity
                if order_pct_of_book > 0.10:
                    kelly_dollars *= 0.5  # Halve dollar budget if >10% of book
                    logger.info(
                        f"Liquidity adjustment: halving kelly_dollars to ${kelly_dollars:.2f} "
                        f"(order was {order_pct_of_book:.0%} of book)"
                    )
                elif order_pct_of_book > 0.05:
                    kelly_dollars *= 0.75
                    logger.debug(
                        f"Liquidity adjustment: reducing kelly_dollars to ${kelly_dollars:.2f} "
                        f"(order was {order_pct_of_book:.0%} of book)"
                    )

        # Step 2: Cap 1 — Max position percentage
        max_position = bankroll * self.settings.trading.max_position_pct
        kelly_dollars = min(kelly_dollars, max_position)

        # Step 3: Cap 2 — Don't exceed remaining exposure room
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        remaining = max_total - current_exposure
        if remaining <= 0:
            logger.info(
                f"Kelly: exposure cap reached (${current_exposure:.2f} / "
                f"${max_total:.2f}) — no room for new trades"
            )
            return 0
        kelly_dollars = min(kelly_dollars, remaining)

        # Convert dollars to contracts
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
            # Binary search for max contracts that fit within kelly_dollars after fees.
            lo, hi, best = 0, contracts, 0
            while lo <= hi:
                mid = (lo + hi) // 2
                fee_cents = math.ceil(fee_rate * mid * cost_price * (1.0 - cost_price))
                fee_dollars = fee_cents / 100.0
                if mid * cost_price + fee_dollars <= kelly_dollars:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            contracts = best

        # Minimum 1 contract if we have any edge and room,
        # but only if the single contract cost + fee stays within kelly_dollars.
        if contracts == 0 and kelly_fraction > 0 and remaining >= cost_price:
            fee_cents = math.ceil(fee_rate * 1 * cost_price * (1.0 - cost_price)) if 0 < cost_price < 1 else 0
            fee_dollars = fee_cents / 100.0
            if cost_price + fee_dollars <= kelly_dollars:
                contracts = 1

        # Apply calibration-based multiplier — reduce sizing when forecasting is poor.
        # Zero multiplier = halt all trading (Brier worse than random).
        # For multi-contract positions, scale down but floor at 1 contract.
        effective_multiplier = self._calibration_multiplier * self._circuit_breaker_multiplier
        if effective_multiplier < 1.0 and contracts > 0:
            if effective_multiplier <= 0:
                # Calibration or circuit breaker says halt all trading
                return 0
            if effective_multiplier <= 0.25 and contracts == 1:
                # Very poor calibration on a minimal-conviction trade — don't trade
                return 0
            scaled = int(contracts * effective_multiplier)
            contracts = max(1, scaled)

        logger.debug(
            f"Kelly sizing: edge={edge:.1%}, prob={probability:.1%}, "
            f"kelly_f={kelly_fraction:.3f}, half={half_kelly:.3f}, "
            f"${kelly_dollars:.2f} → {contracts} contracts @ ${cost_price:.2f}"
            f" (cal_mult={self._calibration_multiplier:.2f}, cb_mult={self._circuit_breaker_multiplier:.2f})"
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

        if brier_score <= BRIER_EXCELLENT:
            mult = 1.1  # Reward excellent calibration with modest sizing boost
        elif brier_score <= BRIER_GOOD:
            mult = 1.0
        elif brier_score <= BRIER_FAIR:
            mult = 0.75  # M-11: was 0.50, now gentler step-down
        elif brier_score <= BRIER_MEDIOCRE:
            mult = 0.50  # M-11: new band for mediocre calibration
        elif brier_score <= BRIER_POOR:
            mult = 0.25
        else:
            mult = 0.0  # Worse than random — halt all sizing until calibration improves

        if mult != self._calibration_multiplier:
            logger.info(
                f"Kelly calibration multiplier: {self._calibration_multiplier:.2f} → {mult:.2f} "
                f"(Brier={brier_score:.3f})"
            )
        self._calibration_multiplier = mult

    def set_circuit_breaker_multiplier(self, multiplier: float) -> None:
        """Apply circuit breaker multiplier on top of calibration multiplier.

        Called when circuit breaker detects consecutive losses and wants
        to reduce position sizing as a safety measure.
        """
        if multiplier != self._circuit_breaker_multiplier:
            logger.info(
                f"Kelly: circuit breaker multiplier {self._circuit_breaker_multiplier:.2f} → {multiplier:.2f}"
            )
            self._circuit_breaker_multiplier = multiplier

    @property
    def calibration_multiplier(self) -> float:
        return self._calibration_multiplier
