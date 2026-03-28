"""PolyEdge — Main orchestrator.

Initializes all components and runs the scan → assess → trade loop.
Phase 3: Paper trading with AI probability + obvious NO strategies
through the risk engine and execution pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from datetime import datetime, timezone

from src.config import load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import MarketDiscovery
from src.core.models import (
    Direction, Market, MarketToken, Order, OrderStatus, OrderType, Side,
    StrategyName, TokenOutcome,
)
from src.data.market_scanner import MarketScanner
from src.storage.database import Database
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.calibration import CalibrationTracker
from src.analysis.resolution_tracker import ResolutionTracker
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.strategies.ai_probability import AIProbabilityStrategy
from src.strategies.obvious_no import ObviousNoStrategy
from src.data.data_enricher import DataEnricher
from src.execution.order_builder import OrderBuilder
from src.execution.order_router import OrderRouter
from src.execution.position_manager import PositionManager
from src.execution.fill_tracker import FillTracker
from src.alerts.alert_manager import AlertManager, LogBackend
from src.alerts.imessage_alert import IMessageBackend
from src.alerts.daily_report import DailyReport
from src.risk.risk_engine import RiskEngine
from src.risk.kelly_sizer import KellySizer
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.portfolio_risk import PortfolioRisk
from src.strategies.news_reactive import NewsReactiveStrategy
from src.strategies.cross_arb import CrossArbStrategy
from src.strategies.whale_tracker import WhaleTrackerStrategy
from src.data.news_ingestion import NewsIngestion
from src.data.market_graph import MarketGraph
from src.data.whale_monitor import WhaleMonitor
from src.core.websocket_client import KalshiWebSocket, TickerUpdate, FillUpdate
from src.core.models import Platform
from src.strategies.cross_platform_arb import CrossPlatformArbStrategy
from src.metrics import Metrics


def setup_logging(level: str = "INFO", log_file: str = "data/logs/polyedge.log"):
    """Configure structured logging to both console and file."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Clear any existing handlers to prevent duplicates on restart
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def scan_and_trade(
    scanner: MarketScanner,
    kalshi: KalshiClient,
    ai_strategy: AIProbabilityStrategy,
    no_strategy: ObviousNoStrategy,
    news_strategy: NewsReactiveStrategy | None,
    cross_arb_strategy: CrossArbStrategy | None,
    whale_strategy: WhaleTrackerStrategy | None,
    market_graph: MarketGraph | None,
    risk_engine: RiskEngine,
    kelly_sizer: KellySizer,
    circuit_breaker: CircuitBreaker,
    order_builder: OrderBuilder,
    order_router: OrderRouter,
    position_manager: PositionManager,
    calibration: CalibrationTracker,
    resolution_tracker: ResolutionTracker,
    calibration_analyzer: CalibrationAnalyzer,
    fill_tracker: FillTracker,
    alert_manager: AlertManager,
    metrics: Metrics | None,
    settings,
    cycle_count: int = 0,
    poly_scanner=None,
    cross_platform_arb: CrossPlatformArbStrategy | None = None,
):
    """Execute one scan-assess-trade cycle."""
    logger = logging.getLogger("polyedge.main")
    _cycle_start = time.time()
    bankroll = settings.trading.bankroll

    # Re-sync bankroll from Kalshi balance in live mode (every cycle)
    if settings.trading.mode == "live":
        try:
            live_balance = await kalshi.get_balance()
            if live_balance is not None and live_balance > 0:
                if abs(live_balance - bankroll) > 1.0:  # Only log if >$1 drift
                    logger.info(f"Bankroll sync: config=${bankroll:.2f} → live=${live_balance:.2f}")
                bankroll = live_balance
                # Propagate to all components that use bankroll
                risk_engine.update_bankroll(live_balance)
                position_manager.bankroll = live_balance
        except Exception as e:
            logger.warning(f"Failed to sync live balance: {e} — using config bankroll")

    # Check for fills on pending live orders
    try:
        new_fills = await fill_tracker.check_fills()
        for fill in new_fills:
            position_manager.clear_pending_exit(fill.market_id)
            position_manager.update_from_trade(fill)
    except Exception as e:
        logger.error(f"Fill tracker check failed: {e}", exc_info=True)

    # Cancel stale open orders (resting > 30 min with no fill)
    try:
        stale_cancelled = await order_router.cancel_stale_orders(
            max_age_seconds=settings.execution.stale_order_age_seconds
        )
        if stale_cancelled:
            logger.info(f"Cancelled {stale_cancelled} stale open orders")
    except Exception as e:
        logger.error(f"Stale order cancellation failed: {e}", exc_info=True)

    # Check circuit breaker (include unrealized losses from open positions)
    unrealized_pnl = position_manager.get_total_unrealized_pnl()
    if not circuit_breaker.check(bankroll, unrealized_pnl=unrealized_pnl):
        logger.warning("Circuit breaker active — skipping trade cycle")
        if settings.alerts.alert_on_circuit_breaker:
            try:
                await alert_manager.send_circuit_breaker_alert(
                    circuit_breaker.halt_reason or "Unknown"
                )
            except Exception as e:
                logger.warning(f"Failed to send circuit breaker alert: {e}")
        return

    # Scan and filter markets (both platforms in parallel when available)
    poly_markets: list[Market] = []
    try:
        if poly_scanner is not None:
            kalshi_task = scanner.run_scan_cycle()
            poly_task = poly_scanner.run_scan_cycle()
            kalshi_results, poly_results = await asyncio.gather(
                kalshi_task, poly_task, return_exceptions=True,
            )
            if isinstance(kalshi_results, Exception):
                logger.error(f"Kalshi scan failed: {kalshi_results}")
                markets = []
            else:
                markets = kalshi_results
            if isinstance(poly_results, Exception):
                logger.error(f"Polymarket scan failed: {poly_results}")
            else:
                poly_markets = poly_results
                markets = markets + poly_markets
        else:
            markets = await scanner.run_scan_cycle()
    except Exception as e:
        logger.error(f"Market scan failed: {e}", exc_info=True)
        if metrics is not None:
            metrics.record_error("scanner", str(e))
        return  # Skip this cycle, try again next time

    if not markets:
        logger.info("No qualifying markets found")
        return

    # Update unrealized P&L with latest market prices
    scanned_tickers = set()
    pos_tickers = {p.market_id for p in position_manager.get_all_positions()}
    positions_updated_from_scan = 0
    for market in markets:
        position_manager.update_price(market.ticker, market.yes_price, market.no_price)
        scanned_tickers.add(market.ticker)
        if market.ticker in pos_tickers:
            positions_updated_from_scan += 1
    if pos_tickers:
        logger.info(
            f"Price update: {positions_updated_from_scan}/{len(pos_tickers)} positions "
            f"updated from scan ({len(scanned_tickers)} markets scanned)"
        )

    # Fetch prices for open positions not covered by the scan.
    # This prevents $0.00 unrealized P&L on positions whose markets
    # don't rank in the top scanned markets by volume/opportunity.
    missing_positions = [
        p for p in position_manager.get_all_positions()
        if p.market_id not in scanned_tickers
    ]
    if missing_positions:
        logger.info(f"Fetching prices for {len(missing_positions)} position markets not in scan")
        for pos in missing_positions:
            ticker = pos.market_id
            pos_platform = getattr(pos, "platform", Platform.KALSHI)
            try:
                if pos_platform == Platform.POLYMARKET and poly_scanner is not None:
                    # Polymarket positions: use Gamma API for price lookup
                    from src.core.polymarket_discovery import parse_polymarket_market
                    raw = await poly_scanner.discovery.get_market_by_condition_id(ticker)
                    if raw:
                        pm = parse_polymarket_market(raw)
                        if pm:
                            position_manager.update_price(ticker, pm.yes_price, pm.no_price)
                            markets.append(pm)
                else:
                    raw = await kalshi.get_market(ticker)
                    if raw:
                        yes_bid = float(raw.get("yes_bid_dollars") or raw.get("yes_bid") or 0)
                        yes_ask = float(raw.get("yes_ask_dollars") or raw.get("yes_ask") or 0)
                        yes_price = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else max(yes_bid, yes_ask)
                        no_price = 1.0 - yes_price if 0 < yes_price < 1 else 0.0
                        # Skip if we got no valid price — don't create a Market with price=0
                        if yes_price <= 0 and no_price <= 0:
                            logger.warning(f"No valid price data for {ticker} — skipping")
                            continue
                        position_manager.update_price(ticker, yes_price, no_price)
                        # Build minimal Market so exit logic can process this position
                        minimal_market = Market(
                            ticker=ticker,
                            question=raw.get("title", ticker),
                            tokens=[
                                MarketToken(token_id=f"{ticker}_yes", outcome=TokenOutcome.YES, price=yes_price),
                                MarketToken(token_id=f"{ticker}_no", outcome=TokenOutcome.NO, price=no_price),
                            ],
                        )
                        markets.append(minimal_market)
            except Exception as e:
                logger.warning(f"Failed to fetch price for position market {ticker}: {e}")

    # Build market lookup (used by both exit logic and signal processing)
    market_lookup = {m.ticker: m for m in markets}

    # Process exit candidates — close positions that hit stop-loss, time limit, or lost edge
    # Log position state for debugging
    all_pos = position_manager.get_all_positions()
    if all_pos:
        pos_with_market = sum(1 for p in all_pos if p.market_id in market_lookup)
        pos_with_pnl = sum(1 for p in all_pos if p.unrealized_pnl != 0)
        logger.info(
            f"Exit scan: {len(all_pos)} positions, {pos_with_market} have market data, "
            f"{pos_with_pnl} have non-zero P&L, "
            f"total unrealized: ${position_manager.get_total_unrealized_pnl():.2f}"
        )
    exit_candidates = position_manager.get_exit_candidates(markets=market_lookup)
    for position, exit_reason in exit_candidates:
        market = market_lookup.get(position.market_id)
        if market is None:
            continue

        # Skip if there's already a resting exit order for this position
        if position_manager.has_pending_exit(position.market_id):
            logger.debug(f"Skipping exit for {position.market_id}: resting exit order already in flight")
            continue

        # Dedup: skip if we already sold this market recently (prevents duplicate
        # exits from overlapping scan cycles or pm2 restart races)
        if scanner.db.has_recent_exit(position.market_id):
            logger.info(f"Skipping exit for {position.market_id}: recent exit exists (dedup)")
            continue

        # Determine exit price — re-fetch in live mode for freshness
        if settings.trading.mode == "live":
            try:
                pos_platform = getattr(position, "platform", Platform.KALSHI)
                if pos_platform == Platform.POLYMARKET and poly_scanner is not None:
                    from src.core.polymarket_discovery import parse_polymarket_market
                    raw = await poly_scanner.discovery.get_market_by_condition_id(position.market_id)
                    if raw:
                        pm = parse_polymarket_market(raw)
                        if pm:
                            market = pm  # Use fresh data
                else:
                    raw = await kalshi.get_market(position.market_id)
                    if raw:
                        yb = float(raw.get("yes_bid_dollars") or raw.get("yes_bid") or 0)
                        ya = float(raw.get("yes_ask_dollars") or raw.get("yes_ask") or 0)
                        if yb > 0 and ya > 0:
                            fresh_yes = (yb + ya) / 2
                            position_manager.update_price(position.market_id, fresh_yes, 1.0 - fresh_yes)
            except Exception as e:
                logger.debug(f"Live exit price refresh failed for {position.market_id}: {e}")

        if position.direction in (Direction.BUY_YES, Direction.SELL_NO):
            exit_price = market.yes_price
        else:
            exit_price = market.no_price

        if exit_price <= 0:
            logger.warning(f"Skipping exit for {position.market_id}: invalid price ${exit_price}")
            continue

        exit_order = Order(
            id=order_builder._generate_order_id(),
            market_id=position.market_id,
            platform=getattr(position, "platform", Platform.KALSHI),
            token_id=position.token_id,
            side=Side.SELL,
            price=exit_price,
            size=position.size,
            cost=exit_price * position.size,
            order_type=OrderType.GTC if settings.trading.prefer_maker else OrderType.FOK,
            status=OrderStatus.PENDING,
            strategy=position.strategy,
            paper=position.paper,
            created_at=datetime.now(timezone.utc),
        )

        # Exit orders skip full risk checks (we WANT to close), but still
        # verify circuit breaker isn't halted (prevents panic selling) and
        # validate the order is sane.
        if circuit_breaker.is_halted() and "stop_loss" not in exit_reason.lower():
            logger.warning(f"Skipping exit for {position.market_id}: circuit breaker active (non-stop-loss)")
            continue

        result = await order_router.route_order(exit_order)
        if result is not None and result.success and result.trade is None:
            # Resting live order — register with fill tracker for polling
            fill_tracker.track(exit_order)
            position_manager.mark_pending_exit(position.market_id)
        if result is not None and result.success and result.trade:
            position_manager.clear_pending_exit(position.market_id)
            position_manager.update_from_trade(result.trade)
            # Record cooldown to prevent immediate re-entry
            risk_engine.record_exit(position.market_id)
            logger.info(f"[EXIT] {position.market_id} — {exit_reason}")
            # Log exit reason to DB for post-hoc analysis
            try:
                scanner.db.log_exit_reason(
                    market_id=position.market_id,
                    exit_reason=exit_reason,
                    exit_price=exit_price,
                    position_size=position.size,
                    realized_pnl=result.trade.realized_pnl if result.trade else 0.0,
                    strategy=position.strategy.value if hasattr(position.strategy, 'value') else str(position.strategy),
                    platform=position.platform.value if hasattr(position.platform, 'value') else str(getattr(position, 'platform', 'kalshi')),
                )
            except Exception as e:
                logger.debug(f"Failed to log exit reason for {position.market_id}: {e}")
            if settings.alerts.alert_on_trade:
                try:
                    await alert_manager.send_trade_alert(
                        market_id=position.market_id,
                        direction="EXIT",
                        size=int(position.size),
                        price=exit_price,
                        cost=exit_order.cost,
                        strategy=position.strategy.value,
                        edge=0.0,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send exit trade alert for {position.market_id}: {e}")

    # Index markets in graph (if available)
    if market_graph is not None:
        try:
            market_graph.index_markets(markets)
        except Exception as e:
            logger.warning(f"Market graph indexing failed: {e}")

    # Generate signals from all strategies
    all_signals: list = []
    ai_signals: list = []
    no_signals: list = []

    try:
        ai_signals = await ai_strategy.scan_for_opportunities(markets)
        all_signals.extend(ai_signals)
    except Exception as e:
        logger.error(f"AI probability strategy failed: {e}", exc_info=True)

    try:
        no_signals = no_strategy.scan_for_opportunities(markets)
        all_signals.extend(no_signals)
    except Exception as e:
        logger.error(f"Obvious NO strategy failed: {e}", exc_info=True)

    if news_strategy is not None:
        try:
            news_signals = await news_strategy.scan_for_opportunities(markets)
            all_signals.extend(news_signals)
        except Exception as e:
            logger.error(f"News strategy failed: {e}", exc_info=True)

    if cross_arb_strategy is not None:
        try:
            arb_signals = await cross_arb_strategy.scan_for_opportunities(markets)
            all_signals.extend(arb_signals)
        except Exception as e:
            logger.error(f"Cross-arb strategy failed: {e}", exc_info=True)

    if whale_strategy is not None:
        try:
            whale_signals = whale_strategy.scan_for_opportunities(markets)
            all_signals.extend(whale_signals)
        except Exception as e:
            logger.error(f"Whale strategy failed: {e}", exc_info=True)

    if cross_platform_arb is not None and poly_markets:
        try:
            kalshi_markets = [m for m in markets if getattr(m, "platform", Platform.KALSHI) == Platform.KALSHI]
            xplat_signals = await cross_platform_arb.scan_for_opportunities(kalshi_markets, poly_markets)
            all_signals.extend(xplat_signals)
        except Exception as e:
            logger.error(f"Cross-platform arb strategy failed: {e}", exc_info=True)

    if not all_signals:
        logger.info("No signals generated this cycle")
        if metrics is not None:
            metrics.record_cycle(
                duration_ms=(time.time() - _cycle_start) * 1000,
                trades=0, signals=0,
                positions=position_manager.get_position_count(),
            )
        return

    # Separate obvious_no from other signals so they get their own trade slot.
    # Obvious_no has tiny edges (1-2%) and would always lose to AI/arb signals
    # (5%+) in a single sorted list, never getting executed.
    edge_signals = [s for s in all_signals if s.strategy != StrategyName.OBVIOUS_NO]
    obvious_no_signals = [s for s in all_signals if s.strategy == StrategyName.OBVIOUS_NO]

    # Sort each pool by edge descending
    edge_signals.sort(key=lambda s: abs(s.edge), reverse=True)
    obvious_no_signals.sort(key=lambda s: abs(s.edge), reverse=True)

    # Reserve up to 2 slots for obvious_no — low-risk diversification
    max_trades = settings.trading.max_trades_per_cycle
    no_slots = min(2, len(obvious_no_signals)) if obvious_no_signals else 0
    max_edge_trades = max_trades - no_slots

    # Interleave: edge signals first, then obvious_no
    ordered_signals = edge_signals + obvious_no_signals

    logger.info(
        f"Processing {len(all_signals)} signals "
        f"({len(ai_signals)} AI, {len(no_signals)} NO, "
        f"{len(edge_signals)} edge, {len(obvious_no_signals)} obvious_no)"
    )

    trades_executed = 0
    edge_trades = 0
    acted_markets: set[str] = set()  # Dedup: one trade per market per cycle
    for signal in ordered_signals:
        # Enforce max trades per cycle to prevent overtrading
        if trades_executed >= max_trades:
            logger.info(f"Max trades per cycle ({max_trades}) reached — deferring remaining signals")
            break
        # Per-pool limit: edge strategies share max_edge_trades slots
        is_obvious_no = signal.strategy == StrategyName.OBVIOUS_NO
        if not is_obvious_no and edge_trades >= max_edge_trades:
            continue  # Skip remaining edge signals, move to obvious_no pool

        # Dedup: skip if we already acted on this market this cycle
        if signal.market_id in acted_markets:
            logger.debug(f"Skipping duplicate signal for {signal.market_id}")
            continue

        # DB-level dedup: prevent duplicate trades from concurrent pm2 instances.
        # If another process already placed a trade on this market in the last 5
        # minutes, skip it. This catches the race condition where pm2 restarts
        # overlap and both instances try to trade the same signal.
        if scanner.db.has_recent_trade(signal.market_id):
            logger.info(f"Skipping {signal.market_id}: recent trade exists (dedup)")
            continue

        # Log every signal immediately for analysis (acted_on=False by default)
        signal_id = scanner.db.log_signal(signal)

        market = market_lookup.get(signal.market_id)
        if market is None:
            logger.warning(
                f"Signal for unknown market {signal.market_id} "
                f"(strategy={signal.strategy.value}) — skipped"
            )
            continue

        # Kelly sizing with circuit breaker multiplier
        current_exposure = position_manager.get_total_exposure()
        contracts = kelly_sizer.calculate_position_size(
            edge=signal.edge,
            probability=signal.probability_estimate,
            bankroll=bankroll,
            current_exposure=current_exposure,
            order_price=signal.market_price,
        )

        # Apply circuit breaker multiplier (reduces sizing after consecutive losses)
        cb_mult = circuit_breaker.get_kelly_multiplier()
        if cb_mult < 1.0:
            contracts = int(contracts * cb_mult)

        if contracts <= 0:
            logger.debug(
                f"Kelly sized to 0 contracts for {signal.market_id} "
                f"(exposure=${current_exposure:.2f}, edge={signal.edge:.1%})"
            )
            continue

        # Build order first to get fee-inclusive cost
        price = signal.market_price
        if settings.trading.prefer_maker:
            order = order_builder.build_limit_order(market, signal, contracts, price)
        else:
            order = order_builder.build_market_order(market, signal, contracts)
        proposed_cost = order.cost

        # Risk check
        risk_result = risk_engine.check_all(signal, market, contracts, proposed_cost)
        if not risk_result.passed:
            continue

        # Route order
        result = await order_router.route_order(order)
        if result is not None and result.success and result.trade is None:
            # Resting live order — register with fill tracker for polling
            fill_tracker.track(order)
        if result is not None and result.success and result.trade:
            # Update position tracker
            position_manager.update_from_trade(result.trade, market.question)

            # Log calibration prediction
            # Convert to YES probability for calibration (Brier expects YES=1, NO=0)
            if signal.direction in (Direction.BUY_NO, Direction.SELL_NO):
                cal_probability = 1.0 - signal.probability_estimate
                # market_price must also be YES price for calibration consistency
                cal_market_price = 1.0 - signal.market_price
            else:
                cal_probability = signal.probability_estimate
                cal_market_price = signal.market_price

            calibration.log_prediction(
                market_id=signal.market_id,
                market_question=signal.market_question,
                predicted_probability=cal_probability,
                market_price=cal_market_price,
                strategy=signal.strategy,
            )

            # Update signal as acted on
            scanner.db.update_signal_acted_on(signal_id, order.id)

            # Send trade alert
            if settings.alerts.alert_on_trade:
                try:
                    await alert_manager.send_trade_alert(
                        market_id=signal.market_id,
                        direction=signal.direction.value,
                        size=int(order.size),
                        price=order.price,
                        cost=order.cost,
                        strategy=signal.strategy.value,
                        edge=signal.edge,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send trade alert for {signal.market_id}: {e}")

            trades_executed += 1
            if not is_obvious_no:
                edge_trades += 1
            acted_markets.add(signal.market_id)

    logger.info(
        f"Cycle complete: {trades_executed} trades executed, "
        f"{position_manager.get_position_count()} open positions, "
        f"exposure: ${position_manager.get_total_exposure():.2f}"
    )

    # Record metrics
    if metrics is not None:
        _cycle_duration_ms = (time.time() - _cycle_start) * 1000
        metrics.record_cycle(
            duration_ms=_cycle_duration_ms,
            trades=trades_executed,
            signals=len(all_signals),
            positions=position_manager.get_position_count(),
        )

    # Check for resolved markets
    try:
        resolved = await resolution_tracker.check_resolutions()
        if resolved > 0:
            logger.info(f"Resolved {resolved} markets this cycle")
    except Exception as e:
        logger.error(f"Resolution check failed: {e}", exc_info=True)

    # Every 10 cycles (~50 min), generate and log calibration report
    if cycle_count > 0 and cycle_count % 10 == 0:
        try:
            report = calibration_analyzer.generate_report()
            if report.total_resolved > 0:
                logger.info(
                    f"Calibration report: "
                    f"Brier={report.overall_brier:.3f}, "
                    f"Win rate={report.overall_win_rate:.1%}, "
                    f"Resolved={report.total_resolved}, "
                    f"Unresolved={report.total_unresolved}"
                )
                if report.best_category:
                    logger.info(f"  Best category: {report.best_category}")
                if report.worst_category and report.worst_category != report.best_category:
                    logger.info(f"  Worst category: {report.worst_category}")

                adjustments = calibration_analyzer.get_category_adjustments()
                if adjustments:
                    for cat, adj in adjustments.items():
                        direction = "underestimates" if adj > 0 else "overestimates"
                        logger.info(f"  Claude {direction} {cat} by {abs(adj):.1%}")

                # Adapt Kelly sizing based on calibration quality
                kelly_sizer.update_calibration_multiplier(report.overall_brier)
            else:
                logger.info("Calibration: no resolved predictions yet")
        except Exception as e:
            logger.error(f"Calibration report failed: {e}", exc_info=True)

    # Periodic position sync with Kalshi (every 10 cycles, live mode only)
    if settings.trading.mode == "live" and cycle_count % 10 == 0:
        try:
            mismatches = await position_manager.sync_with_kalshi(kalshi)
            if mismatches:
                logger.warning(f"Periodic sync found {mismatches} position mismatches")
        except Exception as e:
            logger.debug(f"Periodic position sync failed: {e}")


async def run_trading_loop(
    scanner, kalshi, ai_strategy, no_strategy, news_strategy, cross_arb_strategy,
    whale_strategy, market_graph, risk_engine, kelly_sizer,
    circuit_breaker, order_builder, order_router, position_manager,
    calibration, resolution_tracker, calibration_analyzer, fill_tracker,
    alert_manager, daily_report, metrics, settings, interval,
    poly_scanner=None, cross_platform_arb=None,
    shutdown_event: asyncio.Event | None = None,
):
    """Run the scan-assess-trade loop on an interval."""
    logger = logging.getLogger("polyedge.main")
    cycle_count = 0
    last_trading_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _last_report_date: str = ""

    while not (shutdown_event and shutdown_event.is_set()):
        try:
            # Day-boundary: record previous day's result, reset daily halt, send report
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != last_trading_day:
                yesterday_pnl = scanner.db.get_daily_pnl(last_trading_day)
                circuit_breaker.record_daily_result(yesterday_pnl)
                circuit_breaker.reset_daily()
                logger.info(f"New trading day: previous day P&L=${yesterday_pnl:.2f}")
                last_trading_day = today

                # Daily maintenance: clean up old snapshots and orphaned records
                try:
                    scanner.db.cleanup_old_snapshots(
                        max_age_days=settings.database.snapshot_retention_days
                    )
                    scanner.db.cleanup_orphaned_records()
                except Exception as e:
                    logger.warning(f"Daily cleanup failed: {e}")

            # Send daily report at configured time (once per day)
            now = datetime.now(timezone.utc)
            report_time = settings.alerts.daily_report_time
            try:
                report_h, report_m = map(int, report_time.split(":"))
                past_report_time = (now.hour > report_h) or (now.hour == report_h and now.minute >= report_m)
            except (ValueError, AttributeError):
                past_report_time = False
            if (
                settings.alerts.enabled
                and past_report_time
                and _last_report_date != today
            ):
                try:
                    await daily_report.generate_and_send(today)
                    _last_report_date = today
                except Exception as e:
                    logger.error(f"Daily report failed: {e}", exc_info=True)

            cycle_count += 1
            # Hard timeout: if any individual scan cycle hangs (stuck API call,
            # unresponsive Claude, etc.), abort it and try again next cycle.
            await asyncio.wait_for(scan_and_trade(
                scanner, kalshi, ai_strategy, no_strategy, news_strategy,
                cross_arb_strategy, whale_strategy, market_graph,
                risk_engine, kelly_sizer,
                circuit_breaker, order_builder, order_router, position_manager,
                calibration, resolution_tracker, calibration_analyzer,
                fill_tracker, alert_manager, metrics, settings, cycle_count,
                poly_scanner=poly_scanner,
                cross_platform_arb=cross_platform_arb,
            ), timeout=settings.execution.cycle_timeout_seconds)
            stats = scanner.db.get_stats()
            logger.info(
                f"DB stats: {stats['active_markets']} markets, "
                f"{stats['total_signals']} signals, "
                f"{stats['total_trades']} trades, "
                f"P&L: ${stats['total_pnl']:.2f}"
            )
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except asyncio.TimeoutError:
            logger.error(
                f"Trade cycle timed out (>{settings.execution.cycle_timeout_seconds}s) — skipping. "
                f"WARNING: orders submitted before timeout may be resting on exchange but untracked locally. "
                f"Run position sync on next live cycle to reconcile.",
                exc_info=True,
            )
        except Exception as e:
            logger.error(f"Trade cycle failed: {e}", exc_info=True)

        if shutdown_event and shutdown_event.is_set():
            logger.info("Shutdown requested — exiting trading loop cleanly")
            break
        logger.info(f"Next cycle in {interval} seconds...")
        try:
            await asyncio.wait_for(
                shutdown_event.wait() if shutdown_event else asyncio.sleep(interval),
                timeout=interval,
            )
            # If we get here via shutdown_event, break
            if shutdown_event and shutdown_event.is_set():
                logger.info("Shutdown requested during sleep — exiting trading loop cleanly")
                break
        except asyncio.TimeoutError:
            pass  # Normal timeout — proceed to next cycle


async def main():
    """Main entry point."""
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.file)
    logger = logging.getLogger("polyedge.main")

    # Validate required API keys early
    settings.validate_required_keys()

    logger.info("=" * 60)
    logger.info("PolyEdge Starting — Phase 3: Paper Trading")
    logger.info(f"  Mode: {settings.trading.mode}")
    logger.info(f"  Bankroll: ${settings.trading.bankroll:,.2f}")
    logger.info(f"  Kelly fraction: {settings.trading.kelly_fraction}")
    logger.info(f"  Max position: {settings.trading.max_position_pct:.0%}")
    logger.info(f"  Daily loss limit: {settings.trading.daily_loss_limit_pct:.0%}")
    logger.info(f"  Scan interval: {settings.scanning.interval_seconds}s")
    logger.info(f"  Kalshi API: {settings.kalshi.active_host}")
    logger.info(f"  Polymarket: {'enabled' if settings.polymarket.enabled else 'disabled'}")
    logger.info("=" * 60)

    # Initialize core components
    db = Database(settings.database.path, settings.database.wal_mode)

    kalshi = KalshiClient(
        host=settings.kalshi.active_host,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    # Check Kalshi API health
    healthy = await kalshi.health_check()
    if healthy:
        logger.info("Kalshi API: healthy")
    else:
        logger.warning("Kalshi API: unreachable (continuing in offline mode)")

    if settings.kalshi_api_key_id and settings.kalshi_private_key_path:
        balance = await kalshi.get_balance()
        if balance is not None:
            logger.info(f"Account balance: ${balance:,.2f}")

    # Market discovery and scanning
    discovery = MarketDiscovery(kalshi)
    scanner = MarketScanner(discovery, db, settings)

    # Analysis — validate Anthropic key early to fail fast
    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set — Claude forecasting will not work")
    forecaster = ClaudeForecaster(settings)
    calibration = CalibrationTracker(db)
    resolution_tracker = ResolutionTracker(kalshi, db)
    calibration_analyzer = CalibrationAnalyzer(db)

    # Data enrichment
    data_enricher = DataEnricher(settings)

    # Strategies — core
    ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer, data_enricher)
    no_strategy = ObviousNoStrategy(settings)

    # Strategies — optional (gracefully skip if deps missing)
    news_strategy: NewsReactiveStrategy | None = None
    cross_arb_strategy: CrossArbStrategy | None = None
    whale_strategy: WhaleTrackerStrategy | None = None
    market_graph: MarketGraph | None = None
    portfolio_risk: PortfolioRisk | None = None

    try:
        news_ingestion = NewsIngestion(
            rss_feeds=settings.news.rss_feeds,
            max_article_age_minutes=settings.news.max_article_age_minutes,
            min_relevance=settings.news.min_relevance,
        )
        news_strategy = NewsReactiveStrategy(forecaster, news_ingestion, settings, db)
        logger.info("News-reactive strategy enabled")
    except Exception as e:
        logger.info(f"News-reactive strategy disabled: {e}")

    try:
        market_graph = MarketGraph()
        cross_arb_strategy = CrossArbStrategy(market_graph, forecaster, settings, db)
        logger.info("Cross-arb strategy enabled")
    except Exception as e:
        logger.info(f"Cross-arb strategy disabled: {e}")

    try:
        whale_monitor = WhaleMonitor(settings, db)
        if whale_monitor.basket_size > 0:
            whale_strategy = WhaleTrackerStrategy(whale_monitor, settings, db)
            logger.info(f"Whale tracker strategy enabled ({whale_monitor.basket_size} whales)")
        else:
            logger.info("Whale tracker strategy disabled: empty basket")
    except Exception as e:
        logger.info(f"Whale tracker strategy disabled: {e}")

    # Polymarket integration (conditional)
    poly_scanner = None
    cross_platform_arb: CrossPlatformArbStrategy | None = None
    polymarket_client = None
    if settings.polymarket.enabled:
        try:
            from src.core.polymarket_client import PolymarketClient
            from src.core.polymarket_discovery import PolymarketDiscovery
            from src.data.polymarket_scanner import PolymarketScanner
            from src.data.polymarket_cross_ref import PolymarketCrossRef

            poly_discovery = PolymarketDiscovery(settings.polymarket.gamma_host)

            if settings.polymarket_private_key:
                polymarket_client = PolymarketClient(
                    host=settings.polymarket.clob_host,
                    private_key=settings.polymarket_private_key,
                    chain_id=settings.polymarket.chain_id,
                    signature_type=settings.polymarket.signature_type,
                )
                await polymarket_client.initialize()
                logger.info("Polymarket client initialized (trading enabled)")
            else:
                logger.info("Polymarket: no private key — read-only mode (scanning only)")

            poly_scanner = PolymarketScanner(poly_discovery, db, settings)
            logger.info("Polymarket scanner enabled")

            cross_ref = PolymarketCrossRef(ttl_seconds=300)
            cross_platform_arb = CrossPlatformArbStrategy(settings, db, cross_ref)
            logger.info("Cross-platform arbitrage strategy enabled")

            # Enable Polymarket resolution tracking
            resolution_tracker.polymarket_discovery = poly_discovery
        except Exception as e:
            logger.warning(f"Polymarket integration failed to initialize: {e}")
            poly_scanner = None
            cross_platform_arb = None
            polymarket_client = None
    else:
        logger.info("Polymarket integration disabled (polymarket.enabled=false)")

    # Execution
    order_builder = OrderBuilder(settings)
    position_manager = PositionManager(db, settings.trading.bankroll)
    order_router = OrderRouter(settings, kalshi, db, position_manager=position_manager, polymarket=polymarket_client)
    fill_tracker = FillTracker(kalshi, db, poll_timeout=settings.execution.order_poll_timeout_seconds, polymarket=polymarket_client)

    try:
        portfolio_risk = PortfolioRisk(position_manager, db)
    except Exception as e:
        logger.info(f"Portfolio risk module disabled: {e}")

    # Sync positions with Kalshi on startup (live mode only)
    if settings.trading.mode == "live" and healthy:
        mismatches = await position_manager.sync_with_kalshi(kalshi)
        if mismatches:
            logger.warning(f"Position sync found {mismatches} mismatches — review manually")

    # Alerts
    alert_manager = AlertManager()
    alert_manager.register(LogBackend())
    if settings.alerts.imessage_enabled and settings.alerts.imessage_endpoint:
        alert_manager.register(IMessageBackend(settings.alerts.imessage_endpoint))
        logger.info(f"iMessage alerts enabled: {settings.alerts.imessage_endpoint}")
    daily_report = DailyReport(db, alert_manager, settings)

    # Risk
    circuit_breaker = CircuitBreaker(settings, db)
    kelly_sizer = KellySizer(settings)
    risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db, portfolio_risk)

    # Metrics
    metrics = Metrics()

    # Run initial scan
    logger.info("Running initial scan cycle...")
    markets = await scanner.run_scan_cycle()
    logger.info(f"Initial scan: {len(markets)} qualifying markets")

    for i, m in enumerate(markets[:10], 1):
        logger.info(
            f"  #{i:2d} [{m.category.value:12s}] "
            f"YES={m.yes_price:.2f} NO={m.no_price:.2f} "
            f"vol=${m.volume_24h:>10,.0f} | "
            f"{m.question[:65]}"
        )

    # Start WebSocket for real-time price feeds
    ws_client: KalshiWebSocket | None = None
    ws_task = None
    if settings.kalshi_api_key_id and settings.kalshi_private_key_path:
        try:
            # Build WebSocket URL: strip the REST path suffix and add WS path
            base = settings.kalshi.active_host.replace("https://", "wss://")
            # Remove /trade-api/v2 or /v2 suffix if present to get the base host
            for suffix in ("/trade-api/v2", "/v2"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            ws_host = base.rstrip("/") + "/trade-api/ws/v2"
            ws_client = KalshiWebSocket(
                host=ws_host,
                api_key_id=settings.kalshi_api_key_id,
                private_key_path=settings.kalshi_private_key_path,
            )
            ws_client.set_channels(["ticker", "fill", "market_lifecycle_v2"])

            async def _on_price(update: TickerUpdate):
                # Use yes_bid as the YES price when available (more accurate than last trade)
                yes_price = update.yes_bid if update.yes_bid > 0 else update.price
                no_price = 1.0 - yes_price if 0 < yes_price < 1 else 0.0
                position_manager.update_price(update.market_ticker, yes_price, no_price)

            async def _on_fill(update: FillUpdate):
                trade = await fill_tracker.handle_ws_fill(update)
                if trade:
                    position_manager.update_from_trade(trade)

            ws_client.on_price_update(_on_price)
            ws_client.on_fill(_on_fill)
            ws_task = asyncio.create_task(ws_client.connect())
            logger.info(f"WebSocket client starting: {ws_host}")
        except Exception as e:
            logger.info(f"WebSocket client disabled: {e}")
    else:
        logger.info("WebSocket client disabled (no API keys)")

    # Start dashboard as background task (if FastAPI available)
    dashboard_task = None
    try:
        from src.dashboard.server import start_dashboard
        dashboard_task = asyncio.create_task(start_dashboard(
            db,
            metrics=metrics,
            position_manager=position_manager,
            calibration_tracker=calibration,
            calibration_analyzer=calibration_analyzer,
            circuit_breaker=circuit_breaker,
            bankroll=settings.trading.bankroll,
        ))
        logger.info("Dashboard starting at http://0.0.0.0:8080")
    except ImportError:
        logger.info("Dashboard disabled (install fastapi + uvicorn)")
    except Exception as e:
        logger.warning(f"Dashboard failed to start: {e}")

    # Graceful shutdown event — set by signal handlers so the trading loop
    # can exit cleanly between cycles instead of being killed mid-trade.
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, _frame):
        logger.info(f"Received signal {sig}, requesting graceful shutdown...")
        shutdown_event.set()

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    # Enter trading loop
    logger.info(f"\nEntering trading loop (every {settings.scanning.interval_seconds}s)...")
    try:
        await run_trading_loop(
            scanner, kalshi, ai_strategy, no_strategy, news_strategy,
            cross_arb_strategy, whale_strategy, market_graph,
            risk_engine, kelly_sizer,
            circuit_breaker, order_builder, order_router, position_manager,
            calibration, resolution_tracker, calibration_analyzer,
            fill_tracker, alert_manager, daily_report, metrics,
            settings, settings.scanning.interval_seconds,
            poly_scanner=poly_scanner,
            cross_platform_arb=cross_platform_arb,
            shutdown_event=shutdown_event,
        )
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        # Graceful shutdown: close each component independently so one
        # failure doesn't prevent cleanup of the others.
        try:
            if ws_client is not None:
                await ws_client.close()
        except Exception as e:
            logger.warning(f"WebSocket close failed: {e}")

        if ws_task is not None:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"WebSocket task cleanup error: {e}")

        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Dashboard task cleanup error: {e}")

        try:
            await discovery.close()
        except Exception as e:
            logger.warning(f"Discovery close failed: {e}")

        try:
            await kalshi.close()
        except Exception as e:
            logger.warning(f"Kalshi client close failed: {e}")

        try:
            db.close()
        except Exception as e:
            logger.warning(f"Database close failed: {e}")

        logger.info("PolyEdge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
