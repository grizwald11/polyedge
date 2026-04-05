"""Individual risk check functions extracted from RiskEngine.

Each function implements a single risk check and mutates the ``failed``
and/or ``warnings`` lists that are passed in.  The functions are called
by :class:`~src.risk.risk_engine.RiskEngine.check_all` which remains the
sole orchestrator.

This module is a pure structural refactor -- no behaviour changes.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Market, Signal, StrategyName
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manipulation_detector import ManipulationDetector
from src.risk.portfolio_risk import PortfolioRisk
from src.storage.database import Database

logger = logging.getLogger(__name__)

WASH_TRADE_COOLDOWN_SECONDS = 14400  # M-4: Extended from 30min to 4 hours


def get_min_edge(settings: Settings, strategy: StrategyName) -> float:
    """Get minimum edge threshold for a strategy."""
    if strategy == StrategyName.AI_PROBABILITY:
        return settings.trading.min_edge_ai
    elif strategy == StrategyName.CROSS_ARB:
        return settings.trading.min_edge_arb
    elif strategy == StrategyName.OBVIOUS_NO:
        return settings.trading.min_edge_obvious_no
    elif strategy == StrategyName.NEWS_REACTIVE:
        return settings.trading.min_edge_news
    elif strategy == StrategyName.MEAN_REVERSION:
        return settings.trading.min_edge_mean_reversion
    elif strategy == StrategyName.LATE_RESOLUTION:
        return settings.trading.min_edge_late_resolution
    else:
        return settings.trading.min_edge_ai


def check_excluded_category(
    settings: Settings, market: Market, failed: list[str],
) -> None:
    """0. Category gate -- reject markets in excluded categories (defense-in-depth).

    The scanner already filters excluded categories, but this check ensures
    that no excluded market can reach execution even if it bypasses the scanner.
    """
    market_cat = market.category.value if hasattr(market.category, 'value') else str(market.category)
    cat_lower = market_cat.lower()

    excluded = settings.scanning.exclude_categories
    if not excluded:
        return
    for exc in excluded:
        exc_lower = exc.lower()
        if exc_lower in cat_lower or cat_lower in exc_lower:
            failed.append(
                f"Excluded category: market category '{market_cat}' matches exclusion '{exc}'"
            )
            return
    for tag in market.tags:
        tag_lower = tag.lower()
        for exc in excluded:
            exc_lower = exc.lower()
            if exc_lower in tag_lower or tag_lower in exc_lower:
                failed.append(
                    f"Excluded category: market tag '{tag}' matches exclusion '{exc}'"
                )
                return


def check_balance(
    positions: PositionManager,
    bankroll: float, proposed_cost: float, pending_order_cost: float,
    failed: list[str],
) -> float:
    """1. Balance check -- includes both filled positions and pending orders.

    Returns committed capital for use in subsequent checks.
    """
    total_exposure = positions.get_total_exposure()
    committed = total_exposure + pending_order_cost
    logger.debug(
        "Exposure check: filled=$%.2f + pending=$%.2f = $%.2f committed",
        total_exposure, pending_order_cost, committed,
    )
    available = bankroll - committed
    if proposed_cost > available:
        failed.append(f"Insufficient balance: need ${proposed_cost:.2f}, available ${available:.2f}")
    return committed


def check_position_size(
    settings: Settings, bankroll: float, proposed_cost: float, failed: list[str],
) -> None:
    """2. Position size limit (max 5% of bankroll per position)."""
    max_position = bankroll * settings.trading.max_position_pct
    if proposed_cost > max_position:
        failed.append(
            f"Position too large: ${proposed_cost:.2f} > "
            f"${max_position:.2f} ({settings.trading.max_position_pct:.0%} limit)"
        )


def check_total_exposure(
    settings: Settings, bankroll: float, proposed_cost: float, committed: float,
    failed: list[str],
) -> None:
    """3. Total exposure limit (max 40% of bankroll) -- includes pending."""
    new_total = committed + proposed_cost
    max_total = bankroll * settings.trading.max_total_exposure_pct
    if new_total > max_total:
        failed.append(
            f"Total exposure exceeded: ${new_total:.2f} > "
            f"${max_total:.2f} ({settings.trading.max_total_exposure_pct:.0%} limit)"
        )


def check_correlated_exposure(
    settings: Settings,
    positions: PositionManager,
    bankroll: float, signal: Signal, proposed_cost: float,
    failed: list[str], warnings: list[str],
    correlation_detector=None,
    portfolio_risk: PortfolioRisk | None = None,
) -> None:
    """4. Correlated exposure (max 20% -- keyword+event if available, else strategy-based)."""
    max_correlated = bankroll * settings.trading.max_correlated_exposure_pct

    # H-5 FIX: Log which correlation method is used for transparency.
    # Three implementations exist: CorrelationDetector > PortfolioRisk > strategy-based.
    # Primary: use CorrelationDetector (keyword + event_ticker matching)
    if correlation_detector is not None:
        try:
            result = correlation_detector.check_correlation(
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
                f"falling through to {'PortfolioRisk' if portfolio_risk else 'strategy-based'} fallback"
            )

    if portfolio_risk is not None:
        logger.debug(
            f"Correlation check used: PortfolioRisk (event-based) for {signal.market_id}"
        )
        correlated_exposure = portfolio_risk.get_correlated_exposure(signal.market_id)
        logger.debug(
            "Correlated exposure check (event-based): %s = $%.2f",
            signal.market_id, correlated_exposure,
        )
        if correlated_exposure == proposed_cost and not portfolio_risk._get_event_ticker(signal.market_id):
            warnings.append(
                f"No event_ticker for {signal.market_id} — correlated exposure may be undercounted"
            )
        if correlated_exposure + proposed_cost > max_correlated:
            failed.append(
                f"Correlated exposure exceeded for event group of {signal.market_id}: "
                f"${correlated_exposure + proposed_cost:.2f} > ${max_correlated:.2f}"
            )
    else:
        # M-8: Reduced from 50% to 30%. Same-strategy trades aren't necessarily
        # correlated — e.g., two AI_PROBABILITY trades on unrelated markets
        # (Fed rate cut vs. sports regulation) have near-zero correlation.
        logger.debug(
            f"Correlation check used: strategy-based fallback for {signal.market_id}"
        )
        strategy_exposure = positions.get_strategy_exposure(signal.strategy)
        effective_correlated = strategy_exposure * 0.3
        logger.info(
            "Correlated exposure check (strategy-based fallback, 30%% correlation): "
            "%s = $%.2f (raw $%.2f)", signal.strategy.value, effective_correlated, strategy_exposure,
        )
        if effective_correlated + proposed_cost > max_correlated:
            failed.append(
                f"Correlated exposure exceeded for {signal.strategy.value}: "
                f"${effective_correlated + proposed_cost:.2f} > ${max_correlated:.2f}"
            )


def check_circuit_breaker(
    circuit_breaker: CircuitBreaker, failed: list[str],
) -> None:
    """5. Circuit breaker."""
    if circuit_breaker.is_halted():
        halt_reason = circuit_breaker.halt_reason or "Unknown"
        failed.append(f"Circuit breaker active: {halt_reason}")


def check_liquidity(
    market: Market, proposed_cost: float,
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


def check_existing_position(
    settings: Settings, positions: PositionManager,
    signal: Signal, failed: list[str], warnings: list[str],
) -> None:
    """7. Existing position check (including cross-strategy hedge detection)."""
    if not positions.has_position(signal.market_id):
        return
    existing = positions.get_position(signal.market_id)
    if not settings.trading.allow_position_additions:
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


def check_signal_quality(
    settings: Settings, signal: Signal, proposed_cost: float, failed: list[str],
) -> None:
    """8a-e. Signal quality checks: confidence, cost, edge, probability range."""
    # 8a. Minimum confidence
    min_confidence = settings.trading.min_confidence
    if signal.confidence < min_confidence:
        failed.append(
            f"Confidence too low: {signal.confidence:.1%} < {min_confidence:.1%} minimum"
        )

    # 8b. Minimum trade cost
    if proposed_cost <= 0:
        failed.append("Trade cost is zero")

    # 8c. Edge minimum
    min_edge = get_min_edge(settings, signal.strategy)
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

    # 8e. Edge vs theoretical maximum -- edge cannot exceed the
    # probability itself (that would imply market_price < 0).
    # Note: check 8c above already catches edge >= probability, so this
    # is a soft warning for edge approaching the limit.
    # Previously used min(p, 1-p) which wrongly rejected valid high-
    # confidence trades (e.g., 98.5% NO with 3.5% edge).


def check_resolution_date(
    market: Market, failed: list[str], warnings: list[str],
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


def check_cooldown(
    market_id: str,
    cooldowns: dict[str, datetime],
    cooldown_durations: dict[str, int],
    default_cooldown_seconds: int,
    failed: list[str],
    db: Database | None = None,
) -> None:
    """10. Cooldown check -- duration depends on whether last exit was a loss."""
    if market_id not in cooldowns:
        return
    last_exit = cooldowns[market_id]
    cd_duration = cooldown_durations.get(market_id, default_cooldown_seconds)
    elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds()
    if elapsed < cd_duration:
        remaining = cd_duration - elapsed
        failed.append(f"Cooldown active: {remaining:.0f}s remaining for {market_id}")
    else:
        del cooldowns[market_id]
        cooldown_durations.pop(market_id, None)
        if db is not None:
            db.delete_cooldown(market_id)


def check_wash_trade(
    market_id: str, db: Database | None, failed: list[str],
) -> None:
    """M-4: Block re-entry within 4 hours of exiting a market.

    Previously 30 minutes (WASH_TRADE_COOLDOWN_SECONDS=1800), which was
    too short. Extended to 4 hours to reduce rapid entry-exit cycles that
    could trigger exchange-side monitoring.
    """
    if db is None:
        return
    try:
        conn = db._get_conn()
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


def check_manipulation(
    manipulation_detector: ManipulationDetector, market: Market, failed: list[str],
) -> None:
    """11. Manipulation detection -- flag markets with suspicious activity."""
    manip_flag = manipulation_detector.check_market(market)
    if manip_flag is not None:
        failed.append(f"Manipulation flag: {manip_flag.reason}")


def check_obvious_no_limit(
    settings: Settings, positions: PositionManager,
    signal: Signal, bankroll: float, proposed_cost: float,
    failed: list[str],
) -> None:
    """Obvious NO specific: max 10% bankroll in obvious-no positions."""
    if signal.strategy != StrategyName.OBVIOUS_NO:
        return
    no_exposure = positions.get_strategy_exposure(StrategyName.OBVIOUS_NO)
    max_no = bankroll * settings.trading.max_obvious_no_pct
    if no_exposure + proposed_cost > max_no:
        failed.append(
            f"Obvious NO exposure limit: ${no_exposure + proposed_cost:.2f} > "
            f"${max_no:.2f} ({settings.trading.max_obvious_no_pct:.0%} limit)"
        )


def check_max_concurrent_positions(
    settings: Settings, positions: PositionManager,
    signal: Signal, failed: list[str],
) -> None:
    """H-2: Hard cap on number of simultaneous open positions."""
    max_positions = settings.trading.max_concurrent_positions
    current_count = positions.get_position_count()
    # Don't count if we already have a position in this market (position addition)
    if positions.has_position(signal.market_id):
        return
    if current_count >= max_positions:
        failed.append(
            f"Max concurrent positions reached: {current_count} >= {max_positions}"
        )


def check_spread_vs_edge(
    signal: Signal, market: Market, failed: list[str],
) -> None:
    """16. Spread check -- reject if the bid-ask spread eats >50% of the edge.

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
