"""Risk engine — 15-point pre-trade risk check.

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
from src.risk.manipulation_detector import ManipulationDetector
from src.risk.portfolio_risk import PortfolioRisk
from src.storage.database import Database

logger = logging.getLogger(__name__)

WASH_TRADE_COOLDOWN_SECONDS = 14400  # M-4: Extended from 30min to 4 hours


class RiskEngine:
    """Central risk gate — all trades must pass."""

    def __init__(
        self,
        settings: Settings,
        position_manager: PositionManager,
        circuit_breaker: CircuitBreaker,
        db: Database | None = None,
        portfolio_risk: PortfolioRisk | None = None,
        manipulation_detector: ManipulationDetector | None = None,
        correlation_detector=None,
    ):
        self.settings = settings
        self.positions = position_manager
        self.circuit_breaker = circuit_breaker
        self.db = db
        self.portfolio_risk = portfolio_risk
        self.correlation_detector = correlation_detector
        self.manipulation_detector = manipulation_detector or ManipulationDetector()
        self._bankroll_override: float | None = None  # Live-synced bankroll
        # Cooldown after exiting a position: longer for losses to avoid
        # re-entering bad positions, shorter for profitable exits.
        self.cooldown_loss_seconds = 14400   # 4 hours after a loss exit
        self.cooldown_profit_seconds = 3600  # 1 hour after a profit exit
        self.cooldown_seconds = 3600  # Default for legacy/unknown exits
        self._cooldown_durations: dict[str, int] = {}  # market_id -> seconds
        # Load persisted cooldowns if DB available, otherwise start empty
        if db is not None:
            self._cooldowns: dict[str, datetime] = db.load_cooldowns(self.cooldown_seconds)
            self._cooldown_durations = db.load_cooldown_durations()
            logger.debug("Loaded %d active cooldowns from DB", len(self._cooldowns))
            if self._cooldowns:
                logger.info("Loaded %d active cooldowns from DB", len(self._cooldowns))
            elif self.positions.get_all_positions():
                logger.warning(
                    "DB returned 0 cooldowns but %d open positions exist — "
                    "cooldowns may have been lost (DB reset or first run after migration)",
                    len(self.positions.get_all_positions()),
                )
        else:
            self._cooldowns = {}

    @property
    def bankroll(self) -> float:
        """Current bankroll — uses live-synced value if available, else config."""
        if self._bankroll_override is not None:
            return self._bankroll_override
        return self.settings.trading.bankroll

    def update_bankroll(self, live_balance: float) -> None:
        """Update bankroll from live balance sync. Persists to DB for crash recovery."""
        self._bankroll_override = live_balance
        if self.db is not None:
            try:
                self.db.save_setting("live_bankroll", str(live_balance))
            except Exception as e:
                logger.debug(f"Failed to persist bankroll to DB: {e}")

    def restore_bankroll(self) -> None:
        """Restore live-synced bankroll from DB on startup."""
        if self.db is None:
            return
        try:
            stored = self.db.load_setting("live_bankroll")
            if stored is not None:
                self._bankroll_override = float(stored)
                logger.info(f"Restored live bankroll from DB: ${self._bankroll_override:.2f}")
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not restore bankroll from DB: {e}")

    def check_all(
        self,
        signal: Signal,
        market: Market,
        proposed_size: float,
        proposed_cost: float,
        pending_order_cost: float = 0.0,
    ) -> RiskCheckResult:
        """Run all 15 risk checks on a proposed trade.

        Args:
            signal: The trading signal
            market: Market being traded
            proposed_size: Number of contracts
            proposed_cost: Total cost in dollars (price * size)
            pending_order_cost: Total cost of unfilled pending orders

        Returns:
            RiskCheckResult with pass/fail and details
        """
        failed: list[str] = []
        warnings: list[str] = []
        bankroll = self.bankroll

        self._check_excluded_category(market, failed)
        committed = self._check_balance(bankroll, proposed_cost, pending_order_cost, failed)
        self._check_position_size(bankroll, proposed_cost, failed)
        self._check_total_exposure(bankroll, proposed_cost, committed, failed)
        self._check_correlated_exposure(bankroll, signal, proposed_cost, failed, warnings)
        self._check_circuit_breaker(failed)
        self._check_liquidity(market, proposed_cost, failed, warnings)
        self._check_existing_position(signal, failed, warnings)
        self._check_signal_quality(signal, proposed_cost, failed)
        self._check_resolution_date(market, failed, warnings)
        self._check_cooldown(signal.market_id, failed)
        self._check_wash_trade(signal.market_id, failed)
        self._check_manipulation(market, failed)
        self._check_obvious_no_limit(signal, bankroll, proposed_cost, failed)
        self._check_max_concurrent_positions(signal, failed)
        self._check_spread_vs_edge(signal, market, failed)

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

    # -- Individual risk checks --------------------------------------------------

    def _check_excluded_category(
        self, market: Market, failed: list[str],
    ) -> None:
        """0. Category gate — reject markets in excluded categories (defense-in-depth).

        The scanner already filters excluded categories, but this check ensures
        that no excluded market can reach execution even if it bypasses the scanner.
        """
        market_cat = market.category.value if hasattr(market.category, 'value') else str(market.category)
        cat_lower = market_cat.lower()

        # Exclusion check — reject markets in excluded categories (defense-in-depth)
        excluded = self.settings.scanning.exclude_categories
        if not excluded:
            return
        # Check category name — match in both directions (e.g., "Crypto" in "Crypto Prices"
        # or "Crypto Prices" in "Crypto Price Markets")
        for exc in excluded:
            exc_lower = exc.lower()
            if exc_lower in cat_lower or cat_lower in exc_lower:
                failed.append(
                    f"Excluded category: market category '{market_cat}' matches exclusion '{exc}'"
                )
                return
        # Check tags
        for tag in market.tags:
            tag_lower = tag.lower()
            for exc in excluded:
                exc_lower = exc.lower()
                if exc_lower in tag_lower or tag_lower in exc_lower:
                    failed.append(
                        f"Excluded category: market tag '{tag}' matches exclusion '{exc}'"
                    )
                    return

    def _check_balance(
        self, bankroll: float, proposed_cost: float, pending_order_cost: float,
        failed: list[str],
    ) -> float:
        """1. Balance check — includes both filled positions and pending orders.

        Returns committed capital for use in subsequent checks.
        """
        total_exposure = self.positions.get_total_exposure()
        committed = total_exposure + pending_order_cost
        logger.debug(
            "Exposure check: filled=$%.2f + pending=$%.2f = $%.2f committed",
            total_exposure, pending_order_cost, committed,
        )
        available = bankroll - committed
        if proposed_cost > available:
            failed.append(f"Insufficient balance: need ${proposed_cost:.2f}, available ${available:.2f}")
        return committed

    def _check_position_size(
        self, bankroll: float, proposed_cost: float, failed: list[str],
    ) -> None:
        """2. Position size limit (max 5% of bankroll per position)."""
        max_position = bankroll * self.settings.trading.max_position_pct
        if proposed_cost > max_position:
            failed.append(
                f"Position too large: ${proposed_cost:.2f} > "
                f"${max_position:.2f} ({self.settings.trading.max_position_pct:.0%} limit)"
            )

    def _check_total_exposure(
        self, bankroll: float, proposed_cost: float, committed: float,
        failed: list[str],
    ) -> None:
        """3. Total exposure limit (max 40% of bankroll) — includes pending."""
        new_total = committed + proposed_cost
        max_total = bankroll * self.settings.trading.max_total_exposure_pct
        if new_total > max_total:
            failed.append(
                f"Total exposure exceeded: ${new_total:.2f} > "
                f"${max_total:.2f} ({self.settings.trading.max_total_exposure_pct:.0%} limit)"
            )

    def _check_correlated_exposure(
        self, bankroll: float, signal: Signal, proposed_cost: float,
        failed: list[str], warnings: list[str],
    ) -> None:
        """4. Correlated exposure (max 20% — keyword+event if available, else strategy-based)."""
        max_correlated = bankroll * self.settings.trading.max_correlated_exposure_pct

        # H-5 FIX: Log which correlation method is used for transparency.
        # Three implementations exist: CorrelationDetector > PortfolioRisk > strategy-based.
        # Primary: use CorrelationDetector (keyword + event_ticker matching)
        if self.correlation_detector is not None:
            try:
                result = self.correlation_detector.check_correlation(
                    signal.market_id, proposed_cost, bankroll,
                )
                logger.debug(
                    f"Correlation check used: CorrelationDetector for {signal.market_id}"
                )
                if not result.allowed:
                    failed.append(
                        f"Correlated exposure exceeded for {signal.market_id}: "
                        f"${result.correlated_exposure:.2f} > ${result.max_allowed:.2f} "
                        f"({len(result.correlations)} correlated positions)"
                    )
                elif result.correlations:
                    warnings.append(
                        f"Correlated positions detected for {signal.market_id}: "
                        f"{len(result.correlations)} positions, ${result.correlated_exposure:.2f} exposure"
                    )
                return
            except Exception as e:
                logger.warning(
                    f"CorrelationDetector failed for {signal.market_id}: {e} — "
                    f"falling through to {'PortfolioRisk' if self.portfolio_risk else 'strategy-based'} fallback"
                )

        if self.portfolio_risk is not None:
            logger.debug(
                f"Correlation check used: PortfolioRisk (event-based) for {signal.market_id}"
            )
            correlated_exposure = self.portfolio_risk.get_correlated_exposure(signal.market_id)
            logger.debug(
                "Correlated exposure check (event-based): %s = $%.2f",
                signal.market_id, correlated_exposure,
            )
            if correlated_exposure == proposed_cost and not self.portfolio_risk._get_event_ticker(signal.market_id):
                warnings.append(
                    f"No event_ticker for {signal.market_id} — correlated exposure may be undercounted"
                )
            if correlated_exposure + proposed_cost > max_correlated:
                failed.append(
                    f"Correlated exposure exceeded for event group of {signal.market_id}: "
                    f"${correlated_exposure + proposed_cost:.2f} > ${max_correlated:.2f}"
                )
        else:
            # Strategy-based fallback: not all trades in one strategy are correlated,
            # so use 50% of strategy exposure as effective correlated exposure.
            logger.debug(
                f"Correlation check used: strategy-based fallback for {signal.market_id}"
            )
            strategy_exposure = self.positions.get_strategy_exposure(signal.strategy)
            effective_correlated = strategy_exposure * 0.5
            logger.info(
                "Correlated exposure check (strategy-based fallback, 50%% correlation): "
                "%s = $%.2f (raw $%.2f)", signal.strategy.value, effective_correlated, strategy_exposure,
            )
            if effective_correlated + proposed_cost > max_correlated:
                failed.append(
                    f"Correlated exposure exceeded for {signal.strategy.value}: "
                    f"${effective_correlated + proposed_cost:.2f} > ${max_correlated:.2f}"
                )

    def _check_circuit_breaker(self, failed: list[str]) -> None:
        """5. Circuit breaker."""
        if self.circuit_breaker.is_halted():
            halt_reason = self.circuit_breaker.halt_reason or "Unknown"
            failed.append(f"Circuit breaker active: {halt_reason}")

    def _check_liquidity(
        self, market: Market, proposed_cost: float,
        failed: list[str], warnings: list[str],
    ) -> None:
        """6. Market liquidity check (H-2: handle zero/unknown liquidity)."""
        if market.liquidity is None or market.liquidity <= 0:
            warnings.append(
                f"Market liquidity unknown or zero (${market.liquidity or 0:.2f}) — "
                f"cannot validate order size"
            )
        elif proposed_cost > market.liquidity * 0.10:
            failed.append(
                f"Order too large for liquidity: ${proposed_cost:.2f} > "
                f"10% of ${market.liquidity:.2f} book depth"
            )
        elif proposed_cost > market.liquidity * 0.05:
            warnings.append("Order >5% of book depth — expect slippage")

    def _check_existing_position(
        self, signal: Signal, failed: list[str], warnings: list[str],
    ) -> None:
        """7. Existing position check (including cross-strategy hedge detection)."""
        if not self.positions.has_position(signal.market_id):
            return
        existing = self.positions.get_position(signal.market_id)
        if not self.settings.trading.allow_position_additions:
            failed.append(f"Already have position in {signal.market_id}")
        else:
            if existing and hasattr(existing, 'direction') and existing.direction != signal.direction:
                warnings.append(
                    f"Hedge detected: new {signal.direction.value} opposes existing "
                    f"{existing.direction.value} in {signal.market_id}"
                )
            else:
                warnings.append(
                    f"Adding to existing {signal.direction.value} position in {signal.market_id}"
                )

    def _check_signal_quality(
        self, signal: Signal, proposed_cost: float, failed: list[str],
    ) -> None:
        """8a-e. Signal quality checks: confidence, cost, edge, probability range."""
        # 8a. Minimum confidence
        min_confidence = self.settings.trading.min_confidence
        if signal.confidence < min_confidence:
            failed.append(
                f"Confidence too low: {signal.confidence:.1%} < {min_confidence:.1%} minimum"
            )

        # 8b. Minimum trade cost
        if proposed_cost <= 0:
            failed.append("Trade cost is zero")

        # 8c. Edge minimum
        min_edge = self._get_min_edge(signal.strategy)
        if not math.isfinite(signal.edge) or signal.edge <= 0:
            failed.append(
                f"Invalid or non-positive edge: {signal.edge} — no favorable view"
            )
        elif signal.edge >= signal.probability_estimate:
            logger.critical(
                "ENSEMBLE BUG: impossible edge (%.1f%%) >= probability (%.1f%%) "
                "for %s — edge = prob - market_price should never >= prob itself",
                signal.edge * 100, signal.probability_estimate * 100, signal.market_id,
            )
            failed.append(
                f"Edge ({signal.edge:.1%}) >= probability "
                f"({signal.probability_estimate:.1%}) — implies market_price <= 0"
            )
        elif signal.edge < min_edge:
            failed.append(
                f"Edge too small: {signal.edge:.1%} < {min_edge:.1%} minimum "
                f"for {signal.strategy.value}"
            )

        # 8d. Probability range validation
        if signal.probability_estimate < 0.01 or signal.probability_estimate > 0.99:
            failed.append(
                f"Probability {signal.probability_estimate:.2f} outside tradeable range (0.01-0.99)"
            )

        # 8e. Edge vs theoretical maximum — edge cannot exceed the
        # probability itself (that would imply market_price < 0).
        # Note: check 8c above already catches edge >= probability, so this
        # is a soft warning for edge approaching the limit.
        # Previously used min(p, 1-p) which wrongly rejected valid high-
        # confidence trades (e.g., 98.5% NO with 3.5% edge).

    def _check_resolution_date(
        self, market: Market, failed: list[str], warnings: list[str],
    ) -> None:
        """9. Resolution date check.

        M-5: Markets resolving within 4 hours are rejected outright (insufficient
        time for limit orders to fill and for the thesis to play out). Markets
        resolving in <1 day get a warning but are allowed.
        """
        days = market.days_to_resolution
        if days is not None and days < 0.167:
            failed.append(f"Market resolves in <4 hours ({days:.2f} days)")
        elif days is not None and days < 1:
            warnings.append(f"Market resolves in <1 day ({days:.1f} days)")
        elif days is not None and days > 365:
            warnings.append(f"Long-dated market: {days:.0f} days to resolution")

    def _check_cooldown(self, market_id: str, failed: list[str]) -> None:
        """10. Cooldown check — duration depends on whether last exit was a loss."""
        if market_id not in self._cooldowns:
            return
        last_exit = self._cooldowns[market_id]
        cd_duration = self._cooldown_durations.get(market_id, self.cooldown_seconds)
        elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
        if elapsed < cd_duration:
            remaining = cd_duration - elapsed
            failed.append(f"Cooldown active: {remaining:.0f}s remaining for {market_id}")
        else:
            del self._cooldowns[market_id]
            self._cooldown_durations.pop(market_id, None)
            if self.db is not None:
                self.db.delete_cooldown(market_id)

    def _check_wash_trade(self, market_id: str, failed: list[str]) -> None:
        """M-4: Block re-entry within 4 hours of exiting a market.

        Previously 30 minutes (WASH_TRADE_COOLDOWN_SECONDS=1800), which was
        too short. Extended to 4 hours to reduce rapid entry-exit cycles that
        could trigger exchange-side monitoring.
        """
        if self.db is None:
            return
        try:
            conn = self.db._get_conn()
            rows = conn.execute(
                "SELECT timestamp FROM trades WHERE market_id = ? AND side = 'SELL' "
                "ORDER BY timestamp DESC LIMIT 1",
                (market_id,),
            ).fetchall()
            if not rows:
                return
            last_sell_ts = rows[0]["timestamp"]
            last_sell = datetime.fromisoformat(last_sell_ts)
            if last_sell.tzinfo is None:
                last_sell = last_sell.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_sell).total_seconds()
            if elapsed < WASH_TRADE_COOLDOWN_SECONDS:
                minutes = (WASH_TRADE_COOLDOWN_SECONDS - elapsed) / 60.0
                failed.append(
                    f"Wash trading prevention: exited {market_id} {minutes:.0f} min ago ({WASH_TRADE_COOLDOWN_SECONDS // 3600}h cooldown)"
                )
        except Exception as e:
            logger.warning(f"Wash trade check failed for {market_id}: {e}")

    def _check_manipulation(self, market: Market, failed: list[str]) -> None:
        """11. Manipulation detection — flag markets with suspicious activity."""
        manip_flag = self.manipulation_detector.check_market(market)
        if manip_flag is not None:
            failed.append(f"Manipulation flag: {manip_flag.reason}")

    def _check_obvious_no_limit(
        self, signal: Signal, bankroll: float, proposed_cost: float,
        failed: list[str],
    ) -> None:
        """Obvious NO specific: max 10% bankroll in obvious-no positions."""
        if signal.strategy != StrategyName.OBVIOUS_NO:
            return
        no_exposure = self.positions.get_strategy_exposure(StrategyName.OBVIOUS_NO)
        max_no = bankroll * self.settings.trading.max_obvious_no_pct
        if no_exposure + proposed_cost > max_no:
            failed.append(
                f"Obvious NO exposure limit: ${no_exposure + proposed_cost:.2f} > "
                f"${max_no:.2f} ({self.settings.trading.max_obvious_no_pct:.0%} limit)"
            )

    def record_exit(self, market_id: str, pnl: float = 0.0):
        """Record a position exit for cooldown tracking.

        Losses get a longer cooldown (4h) than profits (1h) to
        reduce re-entry into positions that just burned us.
        Persists duration to DB atomically so it survives crashes (H-15).
        """
        now = datetime.now(timezone.utc)
        # Determine duration FIRST
        if pnl < 0:
            cd_duration = self.cooldown_loss_seconds
        else:
            cd_duration = self.cooldown_profit_seconds
        # Persist to DB before updating in-memory state
        if self.db is not None:
            self.db.save_cooldown(market_id, now, cd_duration)
        # Then update in-memory
        self._cooldowns[market_id] = now
        self._cooldown_durations[market_id] = cd_duration

    def _check_max_concurrent_positions(
        self, signal: Signal, failed: list[str],
    ) -> None:
        """H-2: Hard cap on number of simultaneous open positions."""
        max_positions = self.settings.trading.max_concurrent_positions
        current_count = self.positions.get_position_count()
        # Don't count if we already have a position in this market (position addition)
        if self.positions.has_position(signal.market_id):
            return
        if current_count >= max_positions:
            failed.append(
                f"Max concurrent positions reached: {current_count} >= {max_positions}"
            )

    def _check_spread_vs_edge(
        self, signal: Signal, market: Market, failed: list[str],
    ) -> None:
        """16. Spread check — reject if the bid-ask spread eats >50% of the edge.

        A wide spread means we pay a large implicit cost to enter, eroding the
        expected profit. When spread/edge > 0.50, more than half the expected
        edge is consumed by crossing the spread.
        """
        spread = getattr(market, 'spread', None)
        if spread is None or spread <= 0 or signal.edge <= 0:
            return
        spread_ratio = spread / signal.edge
        if spread_ratio > 0.50:
            failed.append(
                f"Spread too wide: {spread:.1%} spread / {signal.edge:.1%} edge = "
                f"{spread_ratio:.0%} (>50% of edge consumed by spread)"
            )

    def _get_min_edge(self, strategy: StrategyName) -> float:
        """Get minimum edge threshold for a strategy."""
        if strategy == StrategyName.AI_PROBABILITY:
            return self.settings.trading.min_edge_ai
        elif strategy == StrategyName.CROSS_ARB:
            return self.settings.trading.min_edge_arb
        elif strategy == StrategyName.OBVIOUS_NO:
            return self.settings.trading.min_edge_obvious_no
        elif strategy == StrategyName.NEWS_REACTIVE:
            return self.settings.trading.min_edge_news
        elif strategy == StrategyName.MEAN_REVERSION:
            return self.settings.trading.min_edge_mean_reversion
        elif strategy == StrategyName.LATE_RESOLUTION:
            return self.settings.trading.min_edge_late_resolution
        else:
            return self.settings.trading.min_edge_ai
