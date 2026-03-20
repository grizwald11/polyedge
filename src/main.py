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
from src.core.models import Direction, Order, OrderStatus, OrderType, Side
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
from src.metrics import Metrics


def setup_logging(level: str = "INFO", log_file: str = "data/logs/polyedge.log"):
    """Configure structured logging to both console and file."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

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
):
    """Execute one scan-assess-trade cycle."""
    logger = logging.getLogger("polyedge.main")
    _cycle_start = time.time()
    bankroll = settings.trading.bankroll

    # Check for fills on pending live orders
    try:
        new_fills = await fill_tracker.check_fills()
        for fill in new_fills:
            position_manager.update_from_trade(fill)
    except Exception as e:
        logger.error(f"Fill tracker check failed: {e}")

    # Cancel stale open orders (resting > 30 min with no fill)
    try:
        stale_cancelled = await order_router.cancel_stale_orders(
            max_age_seconds=settings.execution.stale_order_age_seconds
        )
        if stale_cancelled:
            logger.info(f"Cancelled {stale_cancelled} stale open orders")
    except Exception as e:
        logger.error(f"Stale order cancellation failed: {e}")

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

    # Scan and filter markets
    try:
        markets = await scanner.run_scan_cycle()
    except Exception as e:
        logger.error(f"Market scan failed: {e}")
        if metrics is not None:
            metrics.record_error("scanner", str(e))
        return  # Skip this cycle, try again next time

    if not markets:
        logger.info("No qualifying markets found")
        return

    # Update unrealized P&L with latest market prices
    scanned_tickers = set()
    for market in markets:
        position_manager.update_price(market.ticker, market.yes_price, market.no_price)
        scanned_tickers.add(market.ticker)

    # Fetch prices for open positions not covered by the scan.
    # This prevents $0.00 unrealized P&L on positions whose markets
    # don't rank in the top scanned markets by volume/opportunity.
    missing_tickers = [
        p.market_id for p in position_manager.get_all_positions()
        if p.market_id not in scanned_tickers
    ]
    if missing_tickers:
        logger.debug(f"Fetching prices for {len(missing_tickers)} position markets not in scan")
        for ticker in missing_tickers:
            try:
                raw = await kalshi.get_market(ticker)
                if raw:
                    yes_bid = float(raw.get("yes_bid_dollars") or raw.get("yes_bid") or 0)
                    yes_ask = float(raw.get("yes_ask_dollars") or raw.get("yes_ask") or 0)
                    yes_price = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else max(yes_bid, yes_ask)
                    no_price = 1.0 - yes_price if 0 < yes_price < 1 else 0.0
                    position_manager.update_price(ticker, yes_price, no_price)
            except Exception as e:
                logger.debug(f"Failed to fetch price for position market {ticker}: {e}")

    # Build market lookup (used by both exit logic and signal processing)
    market_lookup = {m.ticker: m for m in markets}

    # Process exit candidates — close positions that hit stop-loss, time limit, or lost edge
    exit_candidates = position_manager.get_exit_candidates(markets=market_lookup)
    for position, exit_reason in exit_candidates:
        market = market_lookup.get(position.market_id)
        if market is None:
            continue

        # Determine exit price from current market
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

        result = await order_router.route_order(exit_order)
        if result is not None and result.success and result.trade:
            position_manager.update_from_trade(result.trade)
            # Record cooldown to prevent immediate re-entry
            risk_engine.record_exit(position.market_id)
            logger.info(f"[EXIT] {position.market_id} — {exit_reason}")
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
        ai_signals = await ai_strategy.scan_for_opportunities(markets[:30])
        all_signals.extend(ai_signals)
    except Exception as e:
        logger.error(f"AI probability strategy failed: {e}")

    try:
        no_signals = no_strategy.scan_for_opportunities(markets)
        all_signals.extend(no_signals)
    except Exception as e:
        logger.error(f"Obvious NO strategy failed: {e}")

    if news_strategy is not None:
        try:
            news_signals = await news_strategy.scan_for_opportunities(markets)
            all_signals.extend(news_signals)
        except Exception as e:
            logger.error(f"News strategy failed: {e}")

    if cross_arb_strategy is not None:
        try:
            arb_signals = await cross_arb_strategy.scan_for_opportunities(markets)
            all_signals.extend(arb_signals)
        except Exception as e:
            logger.error(f"Cross-arb strategy failed: {e}")

    if whale_strategy is not None:
        try:
            whale_signals = whale_strategy.scan_for_opportunities(markets)
            all_signals.extend(whale_signals)
        except Exception as e:
            logger.error(f"Whale strategy failed: {e}")

    if not all_signals:
        logger.info("No signals generated this cycle")
        if metrics is not None:
            metrics.record_cycle(
                duration_ms=(time.time() - _cycle_start) * 1000,
                trades=0, signals=0,
                positions=position_manager.get_position_count(),
            )
        return

    # Sort by edge descending — best opportunities first
    all_signals.sort(key=lambda s: abs(s.edge), reverse=True)

    logger.info(f"Processing {len(all_signals)} signals ({len(ai_signals)} AI, {len(no_signals)} NO)")

    trades_executed = 0
    max_trades = settings.trading.max_trades_per_cycle
    acted_markets: set[str] = set()  # Dedup: one trade per market per cycle
    for signal in all_signals:
        # Enforce max trades per cycle to prevent overtrading
        if trades_executed >= max_trades:
            logger.info(f"Max trades per cycle ({max_trades}) reached — deferring remaining signals")
            break

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
        logger.error(f"Resolution check failed: {e}")

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
            logger.error(f"Calibration report failed: {e}")


async def run_trading_loop(
    scanner, kalshi, ai_strategy, no_strategy, news_strategy, cross_arb_strategy,
    whale_strategy, market_graph, risk_engine, kelly_sizer,
    circuit_breaker, order_builder, order_router, position_manager,
    calibration, resolution_tracker, calibration_analyzer, fill_tracker,
    alert_manager, daily_report, metrics, settings, interval,
):
    """Run the scan-assess-trade loop on an interval."""
    logger = logging.getLogger("polyedge.main")
    cycle_count = 0
    last_trading_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _last_report_date: str = ""

    while True:
        try:
            # Day-boundary: record previous day's result, reset daily halt, send report
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != last_trading_day:
                yesterday_pnl = scanner.db.get_daily_pnl(last_trading_day)
                circuit_breaker.record_daily_result(yesterday_pnl)
                circuit_breaker.reset_daily()
                logger.info(f"New trading day: previous day P&L=${yesterday_pnl:.2f}")
                last_trading_day = today

                # Daily maintenance: clean up old snapshots to prevent DB bloat
                try:
                    scanner.db.cleanup_old_snapshots(
                        max_age_days=settings.database.snapshot_retention_days
                    )
                except Exception as e:
                    logger.warning(f"Snapshot cleanup failed: {e}")

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
                    logger.error(f"Daily report failed: {e}")

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
            logger.error("Trade cycle timed out (>5 minutes) — skipping")
        except Exception as e:
            logger.error(f"Trade cycle failed: {e}", exc_info=True)

        logger.info(f"Next cycle in {interval} seconds...")
        await asyncio.sleep(interval)


async def main():
    """Main entry point."""
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.file)
    logger = logging.getLogger("polyedge.main")

    logger.info("=" * 60)
    logger.info("PolyEdge Starting — Phase 3: Paper Trading")
    logger.info(f"  Mode: {settings.trading.mode}")
    logger.info(f"  Bankroll: ${settings.trading.bankroll:,.2f}")
    logger.info(f"  Kelly fraction: {settings.trading.kelly_fraction}")
    logger.info(f"  Max position: {settings.trading.max_position_pct:.0%}")
    logger.info(f"  Daily loss limit: {settings.trading.daily_loss_limit_pct:.0%}")
    logger.info(f"  Scan interval: {settings.scanning.interval_seconds}s")
    logger.info(f"  Kalshi API: {settings.kalshi.active_host}")
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

    # Analysis
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

    # Execution
    order_builder = OrderBuilder(settings)
    order_router = OrderRouter(settings, kalshi, db)
    position_manager = PositionManager(db, settings.trading.bankroll)
    fill_tracker = FillTracker(kalshi, db, poll_timeout=settings.execution.order_poll_timeout_seconds)

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
        ))
        logger.info("Dashboard starting at http://0.0.0.0:8080")
    except ImportError:
        logger.info("Dashboard disabled (install fastapi + uvicorn)")
    except Exception as e:
        logger.warning(f"Dashboard failed to start: {e}")

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
