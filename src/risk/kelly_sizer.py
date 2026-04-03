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
    """Quarter-Kelly position sizing with dynamic adjustment and caps."""

    # Dynamic Kelly bounds: scale fraction based on rolling win rate
    KELLY_MIN = 0.15  # Floor during losing streaks
    KELLY_MAX = 0.30  # Ceiling during winning streaks
    KELLY_WINDOW = 20  # Rolling window for win rate calculation

    def __init__(self, settings: Settings):
        self.settings = settings
        # Event/political markets on Kalshi are fee-free. Only apply fees
        # for fee-enabled categories (crypto, sports). Default to 0 since
        # our target categories are all fee-free.
        self.fee_rate = 0.0
        self._fee_rate_if_enabled = 0.0175 if settings.trading.prefer_maker else 0.07
        self._calibration_multiplier: float = 1.0
        self._circuit_breaker_multiplier: float = 1.0
        self._regime_multiplier: float = 1.0
        self._edge_multiplier: float = 1.0
        self._recent_outcomes: list[bool] = []  # True=win, False=loss

    def record_outcome(self, won: bool) -> None:
        """Record a trade outcome for dynamic Kelly adjustment."""
        self._recent_outcomes.append(won)
        if len(self._recent_outcomes) > self.KELLY_WINDOW:
            self._recent_outcomes = self._recent_outcomes[-self.KELLY_WINDOW:]

    @property
    def dynamic_kelly_fraction(self) -> float:
        """Compute Kelly fraction scaled by rolling win rate.

        With fewer than KELLY_WINDOW trades, use the configured default.
        With enough data, interpolate between KELLY_MIN (0% wins) and
        KELLY_MAX (100% wins) based on rolling win rate.
        """
        if len(self._recent_outcomes) < self.KELLY_WINDOW:
            return self.settings.trading.kelly_fraction
        win_rate = sum(self._recent_outcomes) / len(self._recent_outcomes)
        # Linear interpolation: 0% win rate → KELLY_MIN, 100% → KELLY_MAX
        fraction = self.KELLY_MIN + win_rate * (self.KELLY_MAX - self.KELLY_MIN)
        return fraction

    # -- Private helpers for calculate_position_size --

    def _validate_inputs(
        self, edge: float, probability: float, bankroll: float, current_exposure: float,
    ) -> bool:
        """Return False if any input is non-finite or out of valid range."""
        if (
            not math.isfinite(edge) or not math.isfinite(probability)
            or not math.isfinite(bankroll) or not math.isfinite(current_exposure)
        ):
            logger.warning(
                f"Kelly: non-finite input detected (edge={edge}, prob={probability}, "
                f"bankroll={bankroll}, exposure={current_exposure}) — returning 0"
            )
            return False
        if edge <= 0 or probability <= 0 or probability >= 1 or bankroll <= 0:
            return False
        return True

    def _apply_edge_decay(self, edge: float) -> float:
        """Apply edge decay multiplier to correct for systematic overestimation."""
        if self._edge_multiplier != 1.0:
            edge = edge * self._edge_multiplier
        return edge

    def _check_price_viability(self, market_price: float, edge: float) -> bool:
        """Return False if the contract price makes it unsuitable for trading.

        Checks:
        - market_price out of (0, 1) range
        - Below $0.03: too volatile
        - $0.03-$0.10: requires 10% edge
        - Above $0.97: requires 10% edge (asymmetric payoff)
        """
        if market_price <= 0 or market_price >= 1.0:
            logger.debug(
                f"Kelly: invalid market_price={market_price:.3f} — skipping"
            )
            return False

        # Risk-based check for cheap contracts. Blanket rejection at $0.10 was
        # too aggressive — it excluded valid high-edge trades in the $0.03-$0.10
        # range. Instead, use tiered rules.
        # IMPORTANT: Always use the Kelly-derived market_price for this check,
        # NOT the stale order_price from signal generation.
        if market_price < 0.03:
            logger.debug("Price below $0.03 — too volatile for reliable sizing")
            return False
        if market_price < 0.10 and edge < 0.10:
            logger.debug(
                f"Low-price contract ({market_price:.2f}) requires 10% edge, got {edge:.1%}"
            )
            return False
        # Contracts above $0.97 have tiny upside but full downside if the
        # market flips — same risk profile as cheap contracts (H-1).
        if market_price > 0.97 and edge < 0.10:
            logger.debug(
                f"High-price contract ({market_price:.2f}) requires 10% edge, got {edge:.1%}"
            )
            return False
        return True

    def _compute_kelly_fraction(self, probability: float, market_price: float) -> float:
        """Compute raw Kelly fraction from probability and market price.

        Returns the fraction of bankroll to wager (before half-Kelly scaling).
        Returns 0.0 if Kelly says no bet.
        """
        b = (1.0 - market_price) / market_price  # odds
        q = 1.0 - probability
        kelly_fraction = (probability * b - q) / b
        return max(kelly_fraction, 0.0)

    def _apply_liquidity_adjustment(
        self, kelly_dollars: float, cost_price: float, market_liquidity: float | None,
    ) -> float:
        """Reduce dollar budget when order is large relative to book depth."""
        if market_liquidity is None or market_liquidity <= 0 or kelly_dollars <= 0:
            return kelly_dollars
        raw_contracts = int(kelly_dollars / cost_price) if cost_price > 0 else 0
        if raw_contracts <= 0:
            return kelly_dollars
        order_pct_of_book = (raw_contracts * cost_price) / market_liquidity
        if order_pct_of_book > 0.10:
            kelly_dollars *= 0.5
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
        return kelly_dollars

    def _apply_caps(
        self,
        kelly_dollars: float,
        bankroll: float,
        probability: float,
        current_exposure: float,
    ) -> tuple[float, float]:
        """Apply position and exposure caps.

        Returns (capped_kelly_dollars, max_position). Returns (0, 0) if
        exposure room is exhausted.
        """
        # Cap 1 — Max position percentage
        max_position = bankroll * self.settings.trading.max_position_pct
        # High-probability trades (P>0.95, typically obvious-NO) have tiny payoffs
        # but full downside if the market flips. Cap at 3% of bankroll.
        if probability > 0.95:
            high_prob_cap = bankroll * 0.03
            max_position = min(max_position, high_prob_cap)
            logger.debug(
                f"Kelly: high-prob cap applied (P={probability:.2f}), "
                f"max_position=${max_position:.2f}"
            )
        kelly_dollars = min(kelly_dollars, max_position)

        # Cap 2 — Don't exceed remaining exposure room
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        remaining = max_total - current_exposure
        if remaining <= 0:
            logger.info(
                f"Kelly: exposure cap reached (${current_exposure:.2f} / "
                f"${max_total:.2f}) — no room for new trades"
            )
            return 0.0, 0.0
        kelly_dollars = min(kelly_dollars, remaining)

        return kelly_dollars, max_position

    def _apply_fee_adjustment(
        self,
        contracts: int,
        cost_price: float,
        kelly_dollars: float,
        fee_rate: float,
    ) -> int:
        """Binary search for max contracts that fit within kelly_dollars after fees.

        Uses worst-case taker fee (0.07) as safety margin even for fee-free
        markets (C-4 FIX).
        """
        if contracts <= 0 or cost_price <= 0 or cost_price >= 1:
            return contracts

        safety_fee_rate = max(fee_rate, 0.07)
        lo, hi, best = 0, contracts, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            fee_cents = math.ceil(safety_fee_rate * mid * cost_price * (1.0 - cost_price))
            fee_dollars = fee_cents / 100.0
            if mid * cost_price + fee_dollars <= kelly_dollars:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _apply_calibration_scaling(
        self,
        contracts: int,
        kelly_fraction: float,
        remaining: float,
        cost_price: float,
    ) -> int:
        """Scale contracts by calibration/circuit-breaker/regime multipliers.

        Also applies the minimum-1-contract floor when there is edge and room.
        """
        effective_multiplier = (
            self._calibration_multiplier
            * self._circuit_breaker_multiplier
            * self._regime_multiplier
        )
        if effective_multiplier < 1.0 and contracts > 0:
            if effective_multiplier <= 0:
                return 0
            if effective_multiplier <= 0.25 and contracts == 1:
                return 0
            scaled = int(contracts * effective_multiplier)
            contracts = max(1, scaled)
        return contracts

    # -- Main orchestrator --

    def calculate_position_size(
        self,
        edge: float,
        probability: float,
        bankroll: float,
        current_exposure: float = 0.0,
        order_price: float | None = None,
        market_liquidity: float | None = None,
        fee_rate: float = 0.0,
        confidence: float | None = None,
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
        if not self._validate_inputs(edge, probability, bankroll, current_exposure):
            return 0

        edge = self._apply_edge_decay(edge)
        if edge <= 0:
            return 0

        market_price = probability - edge
        if not self._check_price_viability(market_price, edge):
            return 0

        kelly_fraction = self._compute_kelly_fraction(probability, market_price)
        if kelly_fraction <= 0:
            return 0

        # Apply dynamic Kelly fraction (scales with rolling win rate)
        half_kelly = kelly_fraction * self.dynamic_kelly_fraction
        kelly_dollars = half_kelly * bankroll

        # Confidence adjustment: confidence^1.5 penalizes low-confidence more
        # aggressively (0.9→0.89x, 0.7→0.67x, 0.5→0.48x)
        if confidence is not None and 0.0 < confidence <= 1.0:
            kelly_dollars *= max(0.2, confidence ** 1.5)

        # Use the higher of market_price and order_price for contract conversion
        cost_price = max(market_price, order_price) if order_price and order_price > 0 else market_price

        kelly_dollars = self._apply_liquidity_adjustment(kelly_dollars, cost_price, market_liquidity)

        kelly_dollars, max_position = self._apply_caps(kelly_dollars, bankroll, probability, current_exposure)
        if kelly_dollars == 0:
            return 0

        # Remaining exposure room (needed for min-1-contract check)
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        remaining = max_total - current_exposure

        # Convert dollars to contracts
        contracts = int(kelly_dollars / cost_price) if cost_price > 0 else 0

        # Hard check: ensure contracts * cost_price doesn't exceed position cap
        if contracts > 0 and contracts * cost_price > max_position:
            contracts = int(max_position / cost_price)
            logger.debug(f"Kelly: clamped contracts to {contracts} (position cap ${max_position:.2f})")

        contracts = self._apply_fee_adjustment(contracts, cost_price, kelly_dollars, fee_rate)

        # Minimum 1 contract if we have any edge and room
        if contracts == 0 and kelly_fraction > 0 and remaining >= cost_price:
            fee_cents = math.ceil(fee_rate * 1 * cost_price * (1.0 - cost_price)) if 0 < cost_price < 1 else 0
            fee_dollars = fee_cents / 100.0
            if cost_price + fee_dollars <= kelly_dollars:
                contracts = 1

        contracts = self._apply_calibration_scaling(contracts, kelly_fraction, remaining, cost_price)

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

    def set_regime_multiplier(self, multiplier: float) -> None:
        """Apply market regime multiplier to Kelly sizing.

        Called each cycle by regime detector. In high-volatility regimes,
        reduces position sizes; in calm markets, allows modest increase.
        """
        multiplier = max(0.0, min(multiplier, 1.2))  # Clamp to reasonable range
        if multiplier != self._regime_multiplier:
            logger.info(
                f"Kelly: regime multiplier {self._regime_multiplier:.2f} → {multiplier:.2f}"
            )
        self._regime_multiplier = multiplier

    def set_edge_multiplier(self, multiplier: float) -> None:
        """Apply edge decay multiplier from the EdgeTracker.

        Corrects for systematic overestimation of predicted edges.
        For example, if predicted edges average 10% but realized edges
        average 5%, the multiplier is 0.5 — Kelly will use half the
        predicted edge for sizing.
        """
        # Floor at 0.7: below this, predicted edges are so unreliable that
        # reduced sizing is better than trading weak edges. Previous floor
        # of 0.5 allowed trading when realized edges were half of predicted,
        # which still passed too much noise through as signal.
        multiplier = max(0.7, min(multiplier, 1.0))
        if multiplier != self._edge_multiplier:
            logger.info(
                f"Kelly: edge multiplier {self._edge_multiplier:.2f} → {multiplier:.2f}"
            )
        self._edge_multiplier = multiplier

    @property
    def calibration_multiplier(self) -> float:
        return self._calibration_multiplier
