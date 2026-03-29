"""Risk engine — 10-point pre-trade risk check.

Every trade must pass ALL checks before execution.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Market, RiskCheckResult, Signal, StrategyName
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.portfolio_risk import PortfolioRisk
from src.storage.database import Database

logger = logging.getLogger(__name__)


class RiskEngine:
    """Central risk gate — all trades must pass."""

    def __init__(
        self,
        settings: Settings,
        position_manager: PositionManager,
        circuit_breaker: CircuitBreaker,
        db: Database | None = None,
        portfolio_risk: PortfolioRisk | None = None,
    ):
        self.settings = settings
        self.positions = position_manager
        self.circuit_breaker = circuit_breaker
        self.db = db
        self.portfolio_risk = portfolio_risk
        self._bankroll_override: float | None = None  # Live-synced bankroll
        self.cooldown_seconds = 3600  # 1 hour cooldown after exit
        # Load persisted cooldowns if DB available, otherwise start empty
        if db is not None:
            self._cooldowns: dict[str, datetime] = db.load_cooldowns(self.cooldown_seconds)
            if self._cooldowns:
                logger.info(f"Loaded {len(self._cooldowns)} active cooldowns from DB")
        else:
            self._cooldowns = {}

    @property
    def bankroll(self) -> float:
        """Current bankroll — uses live-synced value if available, else config."""
        if self._bankroll_override is not None:
            return self._bankroll_override
        return self.settings.trading.bankroll

    def update_bankroll(self, live_balance: float) -> None:
        """Update bankroll from live balance sync."""
        self._bankroll_override = live_balance

    def check_all(
        self,
        signal: Signal,
        market: Market,
        proposed_size: float,
        proposed_cost: float,
        pending_order_cost: float = 0.0,
    ) -> RiskCheckResult:
        """Run all 10 risk checks on a proposed trade.

        Args:
            signal: The trading signal
            market: Market being traded
            proposed_size: Number of contracts
            proposed_cost: Total cost in dollars (price * size)
            pending_order_cost: Total cost of unfilled pending orders

        Returns:
            RiskCheckResult with pass/fail and details
        """
        failed = []
        warnings = []
        bankroll = self.bankroll

        # 1. Balance check — includes both filled positions and pending orders
        total_exposure = self.positions.get_total_exposure()
        committed = total_exposure + pending_order_cost
        logger.debug(
            "Exposure check: filled=$%.2f + pending=$%.2f = $%.2f committed",
            total_exposure, pending_order_cost, committed,
        )
        available = bankroll - committed
        if proposed_cost > available:
            failed.append(f"Insufficient balance: need ${proposed_cost:.2f}, available ${available:.2f}")

        # 2. Position size limit (max 5% of bankroll per position)
        max_position = bankroll * self.settings.trading.max_position_pct
        if proposed_cost > max_position:
            failed.append(
                f"Position too large: ${proposed_cost:.2f} > "
                f"${max_position:.2f} ({self.settings.trading.max_position_pct:.0%} limit)"
            )

        # 3. Total exposure limit (max 40% of bankroll) — includes pending
        new_total = committed + proposed_cost
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        if new_total > max_total:
            failed.append(
                f"Total exposure exceeded: ${new_total:.2f} > "
                f"${max_total:.2f} ({self.settings.trading.max_total_exposure_pct:.0%} limit)"
            )

        # 4. Correlated exposure (max 20% — event-based if available, else strategy-based)
        max_correlated = bankroll * self.settings.trading.max_correlated_exposure_pct
        if self.portfolio_risk is not None:
            correlated_exposure = self.portfolio_risk.get_correlated_exposure(signal.market_id)
            if correlated_exposure + proposed_cost > max_correlated:
                failed.append(
                    f"Correlated exposure exceeded for event group of {signal.market_id}: "
                    f"${correlated_exposure + proposed_cost:.2f} > ${max_correlated:.2f}"
                )
        else:
            strategy_exposure = self.positions.get_strategy_exposure(signal.strategy)
            if strategy_exposure + proposed_cost > max_correlated:
                failed.append(
                    f"Correlated exposure exceeded for {signal.strategy.value}: "
                    f"${strategy_exposure + proposed_cost:.2f} > ${max_correlated:.2f}"
                )

        # 5. Circuit breaker
        if self.circuit_breaker.is_halted():
            halt_reason = self.circuit_breaker.halt_reason or "Unknown"
            failed.append(f"Circuit breaker active: {halt_reason}")

        # 6. Market liquidity check
        if market.liquidity > 0 and proposed_cost > market.liquidity * 0.10:
            failed.append(
                f"Order too large for liquidity: ${proposed_cost:.2f} > "
                f"10% of ${market.liquidity:.2f} book depth"
            )
        elif market.liquidity > 0 and proposed_cost > market.liquidity * 0.05:
            warnings.append("Order >5% of book depth — expect slippage")

        # 7. Existing position check
        if self.positions.has_position(signal.market_id):
            failed.append(f"Already have position in {signal.market_id}")

        # 8a. Minimum trade cost check — Kalshi minimum is 1 contract,
        # so any non-zero size is valid. We only reject truly zero-cost trades.
        if proposed_cost <= 0:
            failed.append("Trade cost is zero")

        # 8b. Edge minimum check — edge must be positive (we have a favorable view)
        # and exceed the strategy-specific threshold. Negative edge means we agree
        # with the market, so there's nothing to trade.
        min_edge = self._get_min_edge(signal.strategy)
        if not math.isfinite(signal.edge) or signal.edge <= 0:
            failed.append(
                f"Invalid or non-positive edge: {signal.edge} — no favorable view"
            )
        elif signal.edge >= signal.probability_estimate:
            failed.append(
                f"Edge ({signal.edge:.1%}) >= probability "
                f"({signal.probability_estimate:.1%}) — implies market_price <= 0"
            )
        elif signal.edge < min_edge:
            failed.append(
                f"Edge too small: {signal.edge:.1%} < {min_edge:.1%} minimum "
                f"for {signal.strategy.value}"
            )

        # 9. Resolution date check
        days = market.days_to_resolution
        if days is not None and days < 1:
            failed.append(f"Market resolves in <1 day ({days:.1f} days)")
        elif days is not None and days > 365:
            warnings.append(f"Long-dated market: {days:.0f} days to resolution")

        # 10. Cooldown check
        if signal.market_id in self._cooldowns:
            last_exit = self._cooldowns[signal.market_id]
            elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                failed.append(f"Cooldown active: {remaining:.0f}s remaining for {signal.market_id}")
            else:
                # Clean up expired cooldown
                del self._cooldowns[signal.market_id]
                if self.db is not None:
                    self.db.delete_cooldown(signal.market_id)

        # Obvious NO specific: max 10% bankroll in obvious-no positions
        if signal.strategy == StrategyName.OBVIOUS_NO:
            no_exposure = self.positions.get_strategy_exposure(StrategyName.OBVIOUS_NO)
            max_no = bankroll * self.settings.trading.max_obvious_no_pct
            if no_exposure + proposed_cost > max_no:
                failed.append(
                    f"Obvious NO exposure limit: ${no_exposure + proposed_cost:.2f} > "
                    f"${max_no:.2f} ({self.settings.trading.max_obvious_no_pct:.0%} limit)"
                )

        passed = len(failed) == 0
        result = RiskCheckResult(
            passed=passed,
            failed_checks=failed,
            warnings=warnings,
            approved_size=proposed_size if passed else 0.0,
        )

        if not passed:
            logger.info(
                f"Risk REJECTED {signal.market_id}: {', '.join(failed)}"
            )
        elif warnings:
            logger.info(
                f"Risk PASSED {signal.market_id} with warnings: {', '.join(warnings)}"
            )

        return result

    def record_exit(self, market_id: str):
        """Record a position exit for cooldown tracking."""
        now = datetime.now(timezone.utc)
        self._cooldowns[market_id] = now
        if self.db is not None:
            self.db.save_cooldown(market_id, now)

    def _get_min_edge(self, strategy: StrategyName) -> float:
        """Get minimum edge threshold for a strategy."""
        if strategy == StrategyName.AI_PROBABILITY:
            return self.settings.trading.min_edge_ai
        elif strategy == StrategyName.CROSS_ARB:
            return self.settings.trading.min_edge_arb
        elif strategy == StrategyName.OBVIOUS_NO:
            return self.settings.trading.min_edge_obvious_no
        else:
            return self.settings.trading.min_edge_ai
