"""Lifecycle — trading loop, shutdown handling, day boundary logic, main() entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

import anthropic
import httpx

from src.alerts.alert_manager import AlertManager, LogBackend
from src.alerts.daily_report import DailyReport
from src.alerts.imessage_alert import IMessageBackend
from src.analysis.calibration import CalibrationTracker
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.resolution_tracker import ResolutionTracker
from src.config import load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import MarketDiscovery
from src.core.price_monitor import PriceMonitor
from src.core.websocket_client import (
    FillUpdate,
    KalshiWebSocket,
    LifecycleUpdate,
    TickerUpdate,
)
from src.data.data_enricher import DataEnricher
from src.data.market_graph import MarketGraph
from src.data.market_scanner import MarketScanner
from src.data.news_ingestion import NewsIngestion
from src.data.whale_monitor import WhaleMonitor
from src.execution.fill_tracker import FillTracker
from src.execution.order_builder import OrderBuilder
from src.execution.order_router import OrderRouter
from src.execution.position_manager import PositionManager
from src.metrics import Metrics
from src.orchestrator.scan_cycle import scan_and_trade
from src.orchestrator.startup import _acquire_pid_lock, _check_env_security, _release_pid_lock, setup_logging
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.correlation_detector import CorrelationDetector
from src.risk.kelly_sizer import KellySizer
from src.risk.monte_carlo import MonteCarloSimulator
from src.risk.portfolio_risk import PortfolioRisk
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database
from src.strategies.ai_probability import AIProbabilityStrategy
from src.strategies.cross_arb import CrossArbStrategy
from src.strategies.cross_platform_arb import CrossPlatformArbStrategy
from src.strategies.late_resolution import LateResolutionStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.news_reactive import NewsReactiveStrategy
from src.strategies.obvious_no import ObviousNoStrategy
from src.strategies.whale_tracker import WhaleTrackerStrategy


async def run_trading_loop(
    scanner, kalshi, ai_strategy, no_strategy, news_strategy, cross_arb_strategy,
    whale_strategy, market_graph, risk_engine, kelly_sizer,
    circuit_breaker, order_builder, order_router, position_manager,
    calibration, resolution_tracker, calibration_analyzer, fill_tracker,
    alert_manager, daily_report, metrics, settings, interval,
    poly_scanner=None, cross_platform_arb=None,
    mean_reversion_strategy=None, late_resolution_strategy=None,
    price_monitor=None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the scan-assess-trade loop on an interval."""
    logger = logging.getLogger("polyedge.main")
    cycle_count = 0
    last_trading_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _last_report_date: str = ""
    _consecutive_failures: int = 0
    _max_backoff_seconds: int = 300  # Cap at 5 minutes

    while not (shutdown_event and shutdown_event.is_set()):
        try:
            # Day-boundary: record previous day's result, reset daily halt, send report
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != last_trading_day:
                yesterday_pnl = scanner.db.get_daily_pnl(last_trading_day)
                # Include unrealized P&L (discounted) in the daily result
                # so positions held overnight contribute to consecutive loss tracking
                unrealized = position_manager.get_total_unrealized_pnl()
                total_daily_pnl = yesterday_pnl + unrealized * 0.3
                circuit_breaker.record_daily_result(total_daily_pnl)
                circuit_breaker.reset_daily()
                logger.info(
                    f"New trading day: previous day P&L=${yesterday_pnl:.2f} "
                    f"(unrealized=${unrealized:.2f}, weighted total=${total_daily_pnl:.2f})"
                )
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
                mean_reversion_strategy=mean_reversion_strategy,
                late_resolution_strategy=late_resolution_strategy,
                price_monitor=price_monitor,
            ), timeout=settings.execution.cycle_timeout_seconds)
            stats = scanner.db.get_stats()
            logger.info(
                f"DB stats: {stats['active_markets']} markets, "
                f"{stats['total_signals']} signals, "
                f"{stats['total_trades']} trades, "
                f"P&L: ${stats['total_pnl']:.2f}"
            )
            _consecutive_failures = 0  # Reset on successful cycle
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except asyncio.TimeoutError:
            logger.error(
                f"Trade cycle timed out (>{settings.execution.cycle_timeout_seconds}s) — skipping. "
                f"WARNING: orders submitted before timeout may be resting on exchange but untracked locally.",
                exc_info=True,
            )
            # After timeout, sync positions to detect any orphaned orders
            if settings.trading.mode == "live":
                try:
                    mismatches = await position_manager.sync_with_kalshi(kalshi)
                    if mismatches:
                        logger.warning(f"Post-timeout sync found {mismatches} position mismatches")
                except (httpx.HTTPError, asyncio.TimeoutError, ConnectionError, RuntimeError, OSError) as sync_err:
                    # M-3: Narrowed from bare Exception — catch network/API/runtime errors
                    logger.error(f"Post-timeout position sync failed: {sync_err}", exc_info=True)
                    logger.critical(
                        "CRITICAL: Post-timeout position sync also failed. "
                        "Positions may be desynced. Triggering circuit breaker."
                    )
                    circuit_breaker.trigger_halt("Post-timeout position sync failed")
            _consecutive_failures += 1
        except Exception as e:
            logger.error(f"Trade cycle failed: {e}", exc_info=True)
            _consecutive_failures += 1

        # Exponential backoff on consecutive failures to avoid hammering failing APIs
        if _consecutive_failures > 0:
            backoff_seconds = min(
                2 ** _consecutive_failures,  # 2, 4, 8, 16, 32, 64, 128, 256, 300...
                _max_backoff_seconds,
            )
            logger.warning(
                f"Consecutive failures: {_consecutive_failures} — "
                f"backing off {backoff_seconds}s before next cycle"
            )
            try:
                await asyncio.wait_for(
                    shutdown_event.wait() if shutdown_event else asyncio.sleep(backoff_seconds),
                    timeout=backoff_seconds,
                )
                if shutdown_event and shutdown_event.is_set():
                    break
            except asyncio.TimeoutError:
                pass

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


class _Components:
    """Container for all initialized components, used to pass them between lifecycle stages."""

    def __init__(self) -> None:
        self.db: Database | None = None
        self.kalshi: KalshiClient | None = None
        self.discovery: MarketDiscovery | None = None
        self.scanner: MarketScanner | None = None
        self.forecaster: ClaudeForecaster | None = None
        self.calibration: CalibrationTracker | None = None
        self.resolution_tracker: ResolutionTracker | None = None
        self.calibration_analyzer: CalibrationAnalyzer | None = None
        self.data_enricher: DataEnricher | None = None
        self.ai_strategy: AIProbabilityStrategy | None = None
        self.no_strategy: ObviousNoStrategy | None = None
        self.news_strategy: NewsReactiveStrategy | None = None
        self.cross_arb_strategy: CrossArbStrategy | None = None
        self.whale_strategy: WhaleTrackerStrategy | None = None
        self.market_graph: MarketGraph | None = None
        self.portfolio_risk: PortfolioRisk | None = None
        self.mean_reversion_strategy: MeanReversionStrategy | None = None
        self.late_resolution_strategy: LateResolutionStrategy | None = None
        self.poly_scanner = None
        self.cross_platform_arb: CrossPlatformArbStrategy | None = None
        self.polymarket_client = None
        self.order_builder: OrderBuilder | None = None
        self.position_manager: PositionManager | None = None
        self.order_router: OrderRouter | None = None
        self.fill_tracker: FillTracker | None = None
        self.alert_manager: AlertManager | None = None
        self.daily_report: DailyReport | None = None
        self.circuit_breaker: CircuitBreaker | None = None
        self.kelly_sizer: KellySizer | None = None
        self.risk_engine: RiskEngine | None = None
        self.metrics: Metrics | None = None
        self.correlation_detector: CorrelationDetector | None = None
        self.price_monitor: PriceMonitor | None = None
        self.ws_client: KalshiWebSocket | None = None
        self.ws_task = None
        self.dashboard_task = None
        self.kalshi_healthy: bool = False


async def _initialize_services(settings, logger) -> _Components:
    """Initialize all core services: database, API clients, analysis, data enrichment."""
    c = _Components()
    c.db = Database(settings.database.path, settings.database.wal_mode)

    c.kalshi = KalshiClient(
        host=settings.kalshi.active_host,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    c.kalshi_healthy = await c.kalshi.health_check()
    if c.kalshi_healthy:
        logger.info("Kalshi API: healthy")
    else:
        logger.warning("Kalshi API: unreachable (continuing in offline mode)")

    if settings.kalshi_api_key_id and settings.kalshi_private_key_path:
        balance = await c.kalshi.get_balance()
        if balance is not None:
            logger.info(f"Account balance: ${balance:,.2f}")

    c.discovery = MarketDiscovery(c.kalshi)
    c.scanner = MarketScanner(c.discovery, c.db, settings)

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set — Claude forecasting will not work")
    c.forecaster = ClaudeForecaster(settings)
    if settings.anthropic_api_key:
        try:
            await c.forecaster.health_check()
            logger.info("Anthropic API: key validated successfully")
        except (
            anthropic.APIError,
            anthropic.AuthenticationError,
            anthropic.APIConnectionError,
            asyncio.TimeoutError,
            RuntimeError,
        ) as e:
            # M-3: Narrowed from bare Exception — catch Anthropic API and runtime errors
            logger.critical(
                f"Anthropic API key validation FAILED: {e}. "
                "WARNING: The bot will have DEGRADED SIGNAL GENERATION. "
                "AI probability, cross-market arbitrage validation, and news-reactive "
                "strategies will NOT produce signals until the Anthropic API is reachable. "
                "Check ANTHROPIC_API_KEY and API status at https://status.anthropic.com",
                exc_info=True,
            )
    c.calibration = CalibrationTracker(c.db)
    c.resolution_tracker = ResolutionTracker(c.kalshi, c.db, calibration_tracker=c.calibration)
    c.calibration_analyzer = CalibrationAnalyzer(c.db)
    c.data_enricher = DataEnricher(settings)
    return c


async def _setup_strategies(settings, c: _Components, logger) -> None:
    """Initialize all trading strategies (core + optional)."""
    c.ai_strategy = AIProbabilityStrategy(
        c.forecaster, settings, c.db, c.calibration_analyzer, c.data_enricher,
    )
    c.no_strategy = ObviousNoStrategy(settings)

    try:
        c.mean_reversion_strategy = MeanReversionStrategy(settings, c.db)
        logger.info("Mean reversion strategy enabled")
    except Exception as e:
        logger.info(f"Mean reversion strategy disabled: {e}")

    try:
        news_researcher = None  # Will be set below if news ingestion is available
        c.late_resolution_strategy = LateResolutionStrategy(settings, c.db)
        logger.info("Late resolution strategy enabled")
    except Exception as e:
        logger.info(f"Late resolution strategy disabled: {e}")

    try:
        news_ingestion = NewsIngestion(
            rss_feeds=settings.news.rss_feeds,
            max_article_age_minutes=settings.news.max_article_age_minutes,
            min_relevance=settings.news.min_relevance,
        )
        c.news_strategy = NewsReactiveStrategy(c.forecaster, news_ingestion, settings, c.db)
        logger.info("News-reactive strategy enabled")
        # Wire news ingestion into late resolution strategy if available
        if c.late_resolution_strategy is not None:
            c.late_resolution_strategy.news_researcher = news_ingestion
            logger.info("Late resolution: news researcher connected")
    except Exception as e:
        logger.info(f"News-reactive strategy disabled: {e}")

    try:
        c.market_graph = MarketGraph()
        c.cross_arb_strategy = CrossArbStrategy(c.market_graph, c.forecaster, settings, c.db)
        logger.info("Cross-arb strategy enabled")
    except Exception as e:
        logger.info(f"Cross-arb strategy disabled: {e}")

    try:
        whale_monitor = WhaleMonitor(settings, c.db)
        if whale_monitor.basket_size > 0:
            c.whale_strategy = WhaleTrackerStrategy(whale_monitor, settings, c.db)
            logger.info(f"Whale tracker strategy enabled ({whale_monitor.basket_size} whales)")
        else:
            logger.info("Whale tracker strategy disabled: empty basket")
    except Exception as e:
        logger.info(f"Whale tracker strategy disabled: {e}")

    # Polymarket integration (conditional)
    if settings.polymarket.enabled:
        logger.warning(
            "LEGAL WARNING: Polymarket is not available to US residents per Terms of Service. "
            "Ensure you are eligible before enabling Polymarket trading. "
            "Set polymarket.enabled=false in settings.yaml if you are a US resident."
        )
        try:
            from src.core.polymarket_client import PolymarketClient
            from src.core.polymarket_discovery import PolymarketDiscovery
            from src.data.polymarket_cross_ref import PolymarketCrossRef
            from src.data.polymarket_scanner import PolymarketScanner

            poly_discovery = PolymarketDiscovery(settings.polymarket.gamma_host)

            if settings.polymarket_private_key:
                c.polymarket_client = PolymarketClient(
                    host=settings.polymarket.clob_host,
                    private_key=settings.polymarket_private_key,
                    chain_id=settings.polymarket.chain_id,
                    signature_type=settings.polymarket.signature_type,
                )
                await c.polymarket_client.initialize()
                logger.info("Polymarket client initialized (trading enabled)")
            else:
                logger.info("Polymarket: no private key — read-only mode (scanning only)")

            c.poly_scanner = PolymarketScanner(poly_discovery, c.db, settings)
            logger.info("Polymarket scanner enabled")

            cross_ref = PolymarketCrossRef(ttl_seconds=300)
            c.cross_platform_arb = CrossPlatformArbStrategy(settings, c.db, cross_ref)
            logger.info("Cross-platform arbitrage strategy enabled")

            c.resolution_tracker.polymarket_discovery = poly_discovery
        except Exception as e:
            logger.warning(f"Polymarket integration failed to initialize: {e}")
            c.poly_scanner = None
            c.cross_platform_arb = None
            c.polymarket_client = None
    else:
        logger.info("Polymarket integration disabled (polymarket.enabled=false)")


async def _setup_execution_and_risk(settings, c: _Components, logger) -> None:
    """Initialize execution layer, risk management, and alerts."""
    c.order_builder = OrderBuilder(settings)
    c.position_manager = PositionManager(
        c.db,
        bankroll=settings.trading.bankroll,
        stop_loss_pct=settings.execution.stop_loss_pct,
        max_hold_days=settings.execution.max_hold_days,
        edge_gone_threshold=settings.execution.edge_gone_threshold,
        trailing_stop_activate=settings.execution.trailing_stop_activate,
        trailing_stop_distance=settings.execution.trailing_stop_distance,
        take_profit_pct=settings.execution.take_profit_pct,
        capital_rotation_edge=settings.execution.capital_rotation_edge,
    )
    c.order_router = OrderRouter(
        settings, c.kalshi, c.db,
        position_manager=c.position_manager,
        polymarket=c.polymarket_client,
    )
    c.fill_tracker = FillTracker(
        c.kalshi, c.db,
        poll_timeout=settings.execution.order_poll_timeout_seconds,
        polymarket=c.polymarket_client,
    )

    try:
        c.portfolio_risk = PortfolioRisk(c.position_manager, c.db)
    except Exception as e:
        logger.info(f"Portfolio risk module disabled: {e}")

    # Sync positions with Kalshi on startup (live mode only)
    if settings.trading.mode == "live" and c.kalshi_healthy:
        mismatches = await c.position_manager.sync_with_kalshi(c.kalshi)
        if mismatches:
            logger.warning(f"Position sync found {mismatches} mismatches — review manually")

        try:
            open_orders = await asyncio.wait_for(c.kalshi.get_open_orders(), timeout=10.0)
            if open_orders:
                logger.warning(
                    f"Found {len(open_orders)} open orders on Kalshi at startup — "
                    f"these may be orphaned from a previous crash. "
                    f"Order IDs: {[o.get('order_id', '?') for o in open_orders[:5]]}"
                )
        except (httpx.HTTPError, asyncio.TimeoutError, ConnectionError) as e:
            # M-3: Narrowed from bare Exception — only catch network/API errors
            logger.info(f"Could not check for orphaned orders: {e}")

        # H-5: Reconcile DB pending orders against Kalshi actual order states.
        # Orders may have filled, cancelled, or expired while we were offline.
        try:
            db_pending = c.db.load_pending_orders()
            if db_pending:
                logger.info(f"Reconciling {len(db_pending)} DB pending orders against Kalshi...")
                reconciled = 0
                for order_id, cost in list(db_pending.items()):
                    try:
                        status = await asyncio.wait_for(
                            c.kalshi.get_order(order_id), timeout=10.0
                        )
                        if status is None:
                            continue
                        kalshi_status = status.get("status", "").lower()
                        if kalshi_status in ("executed", "canceled", "cancelled"):
                            c.db.delete_pending_order(order_id)
                            reconciled += 1
                            logger.info(
                                f"  Reconciled order {order_id}: {kalshi_status} "
                                f"(removed from pending)"
                            )
                    except (httpx.HTTPError, asyncio.TimeoutError, KeyError) as e:
                        # M-3: Narrowed — catch API errors + KeyError for malformed responses
                        logger.warning(f"  Failed to reconcile order {order_id}: {e}")
                if reconciled:
                    logger.info(f"Fill reconciliation: resolved {reconciled}/{len(db_pending)} stale pending orders")
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as e:
            # M-3: Narrowed — catch network/API/DB errors for reconciliation
            logger.warning(f"Fill reconciliation failed (non-fatal): {e}")

    # Alerts
    c.alert_manager = AlertManager()
    c.alert_manager.register(LogBackend())
    if settings.alerts.imessage_enabled and settings.alerts.imessage_endpoint:
        c.alert_manager.register(IMessageBackend(settings.alerts.imessage_endpoint))
        logger.info(f"iMessage alerts enabled: {settings.alerts.imessage_endpoint}")
    c.daily_report = DailyReport(c.db, c.alert_manager, settings)

    # Risk
    c.circuit_breaker = CircuitBreaker(settings, c.db)
    c.kelly_sizer = KellySizer(settings)
    try:
        c.correlation_detector = CorrelationDetector(c.position_manager, c.db, settings)
        logger.info("Correlation detector enabled")
    except Exception as e:
        c.correlation_detector = None
        logger.info(f"Correlation detector disabled: {e}")

    c.risk_engine = RiskEngine(
        settings, c.position_manager, c.circuit_breaker, c.db, c.portfolio_risk,
        correlation_detector=c.correlation_detector,
    )
    c.risk_engine.restore_bankroll()

    # Price monitor for adverse move detection
    c.price_monitor = PriceMonitor()
    c.price_monitor.update_positions(c.position_manager.get_all_positions())

    async def _on_adverse_move(alert):
        msg = (
            f"ADVERSE MOVE on {alert.market_id}: {alert.adverse_pct:.1%} "
            f"(entry={alert.entry_price:.2f}, now={alert.current_price:.2f})"
        )
        if alert.should_exit:
            msg = f"AUTO-EXIT triggered — {msg}"
        try:
            await c.alert_manager.send_circuit_breaker_alert(msg)
        except Exception as e:
            logger.warning(f"Failed to send adverse move alert: {e}")

    c.price_monitor.on_adverse_move(_on_adverse_move)
    logger.info("Price monitor initialized")

    # Metrics
    c.metrics = Metrics()
    # L-5: Wire metrics into KalshiClient for API latency tracking
    c.kalshi._metrics = c.metrics
    # Wire metrics into OrderRouter for fill rate tracking
    c.order_router.metrics = c.metrics


async def _setup_background_tasks(settings, c: _Components, logger) -> None:
    """Start WebSocket client and dashboard as background tasks."""
    # Start WebSocket for real-time price feeds
    if settings.kalshi_api_key_id and settings.kalshi_private_key_path:
        try:
            base = settings.kalshi.active_host.replace("https://", "wss://")
            for suffix in ("/trade-api/v2", "/v2"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            ws_host = base.rstrip("/") + "/trade-api/ws/v2"
            c.ws_client = KalshiWebSocket(
                host=ws_host,
                api_key_id=settings.kalshi_api_key_id,
                private_key_path=settings.kalshi_private_key_path,
            )
            c.ws_client.set_channels(["ticker", "fill", "market_lifecycle_v2"])

            async def _on_price(update: TickerUpdate):
                yes_price = update.yes_bid if update.yes_bid > 0 else update.price
                no_price = 1.0 - yes_price if 0 < yes_price < 1 else 0.0
                c.position_manager.update_price(update.market_ticker, yes_price, no_price)
                # Check for adverse moves on open positions
                if c.price_monitor is not None:
                    alert = await c.price_monitor.check_price(
                        update.market_ticker, yes_price, no_price
                    )
                    if alert and alert.should_exit:
                        c.position_manager.mark_pending_exit(update.market_ticker)

            async def _on_fill(update: FillUpdate):
                trade = await c.fill_tracker.handle_ws_fill(update)
                if trade:
                    c.position_manager.update_from_trade(trade)

            async def _on_ws_reconnect():
                """Sync tracked market statuses via REST after WebSocket reconnects."""
                logger.info("WebSocket reconnected — syncing tracked market statuses via REST")
                try:
                    tracked = list(c.ws_client._subscriptions)
                    for ticker in tracked:
                        try:
                            market_data = await c.kalshi.get_market(ticker)
                            if market_data:
                                status = market_data.get("status", "")
                                if status in ("closed", "determined", "finalized"):
                                    logger.info(
                                        f"Reconnect sync: market {ticker} is now '{status}' "
                                        f"(may have settled while disconnected)"
                                    )
                        except Exception as me:
                            logger.warning(f"Reconnect sync failed for {ticker}: {me}")
                except Exception as e:
                    logger.warning(f"Reconnect market status sync failed: {e}")

            async def _on_lifecycle(update: LifecycleUpdate):
                if update.status in ("closed", "halted", "determined", "finalized"):
                    resting = c.fill_tracker.get_pending_for_market(
                        update.market_ticker
                    )
                    for order in resting:
                        try:
                            await c.kalshi.cancel_order(order.id)
                            logger.warning(
                                f"Cancelled resting order {order.id} on "
                                f"{update.market_ticker} — market status "
                                f"changed to {update.status}"
                            )
                        except Exception as cancel_err:
                            logger.error(
                                f"Failed to cancel order {order.id} on "
                                f"{update.market_ticker}: {cancel_err}"
                            )

                if update.status in ("closed", "determined", "finalized"):
                    if c.position_manager.has_position(update.market_ticker):
                        logger.warning(
                            f"Market {update.market_ticker} settled via WebSocket "
                            f"(status={update.status}, settlement={update.settlement_value}) "
                            f"— marking for exit"
                        )
                        if update.settlement_value is not None:
                            c.position_manager.record_settlement(
                                update.market_ticker, update.settlement_value
                            )
                        c.position_manager.mark_pending_exit(update.market_ticker)

            c.ws_client.on_price_update(_on_price)
            c.ws_client.on_fill(_on_fill)
            c.ws_client.on_lifecycle(_on_lifecycle)
            c.ws_client.register_reconnect_sync(_on_ws_reconnect)
            c.ws_task = asyncio.create_task(c.ws_client.connect())
            logger.info(f"WebSocket client starting: {ws_host}")
        except Exception as e:
            logger.info(f"WebSocket client disabled: {e}")
    else:
        logger.info("WebSocket client disabled (no API keys)")

    # Start dashboard
    try:
        from src.dashboard.server import start_dashboard
        c.dashboard_task = asyncio.create_task(start_dashboard(
            c.db,
            metrics=c.metrics,
            position_manager=c.position_manager,
            calibration_tracker=c.calibration,
            calibration_analyzer=c.calibration_analyzer,
            circuit_breaker=c.circuit_breaker,
            bankroll=settings.trading.bankroll,
        ))
        logger.info("Dashboard starting at http://0.0.0.0:8080")
    except ImportError:
        logger.info("Dashboard disabled (install fastapi + uvicorn)")
    except Exception as e:
        logger.warning(f"Dashboard failed to start: {e}")


async def _shutdown(c: _Components, logger) -> None:
    """Graceful shutdown: close each component independently."""
    try:
        if c.ws_client is not None:
            await c.ws_client.close()
    except Exception as e:
        logger.warning(f"WebSocket close failed: {e}")

    if c.ws_task is not None:
        c.ws_task.cancel()
        try:
            await c.ws_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"WebSocket task cleanup error: {e}")

    if c.dashboard_task is not None:
        c.dashboard_task.cancel()
        try:
            await c.dashboard_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Dashboard task cleanup error: {e}")

    try:
        if c.forecaster is not None:
            await c.forecaster.close()
    except Exception as e:
        logger.warning(f"Forecaster close failed: {e}")

    try:
        if c.discovery is not None:
            await c.discovery.close()
    except Exception as e:
        logger.warning(f"Discovery close failed: {e}")

    try:
        if c.kalshi is not None:
            await c.kalshi.close()
    except Exception as e:
        logger.warning(f"Kalshi client close failed: {e}")

    if c.polymarket_client is not None:
        try:
            await c.polymarket_client.close()
        except Exception as e:
            logger.warning(f"Polymarket client close failed: {e}")

    try:
        if c.db is not None:
            c.db.close()
    except Exception as e:
        logger.warning(f"Database close failed: {e}")


async def main() -> None:
    """Main entry point — orchestrates initialization, trading loop, and shutdown."""
    if not _acquire_pid_lock():
        logging.critical("Another PolyEdge instance is already running (PID lock exists). Exiting.")
        sys.exit(1)

    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.file)
    logger = logging.getLogger("polyedge.main")

    settings.validate_required_keys()
    _check_env_security(logger)  # C-5: Warn about plaintext keys

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

    # H-12: Warn when running paper mode against production Kalshi API
    if not settings.kalshi.use_demo and settings.trading.mode == "paper":
        logger.warning(
            "Running paper mode against PRODUCTION Kalshi API — "
            "set kalshi.use_demo: true for full sandbox isolation"
        )

    # Phase 1: Initialize all services
    c = await _initialize_services(settings, logger)

    # Phase 2: Setup strategies
    await _setup_strategies(settings, c, logger)

    # Phase 3: Setup execution, risk, alerts
    await _setup_execution_and_risk(settings, c, logger)

    # Run initial scan
    logger.info("Running initial scan cycle...")
    markets = await c.scanner.run_scan_cycle()
    logger.info(f"Initial scan: {len(markets)} qualifying markets")

    for i, m in enumerate(markets[:10], 1):
        logger.info(
            f"  #{i:2d} [{m.category.value:12s}] "
            f"YES={m.yes_price:.2f} NO={m.no_price:.2f} "
            f"vol=${m.volume_24h:>10,.0f} | "
            f"{m.question[:65]}"
        )

    # Startup validation: Monte Carlo risk simulation against historical trade stats
    try:
        conn = c.db._get_conn()
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                                   AS total,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)         AS wins,
                AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl / NULLIF(size, 0) END) AS avg_win,
                AVG(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) / NULLIF(size, 0) END) AS avg_loss
            FROM trades
            WHERE realized_pnl IS NOT NULL AND size > 0
            """
        ).fetchone()
        total_trades = row["total"] if row else 0
        if total_trades >= 10:
            wins = row["wins"] or 0
            win_rate = wins / total_trades
            avg_win = row["avg_win"] if row["avg_win"] is not None else 0.3
            avg_loss = row["avg_loss"] if row["avg_loss"] is not None else 0.2
            simulator = MonteCarloSimulator()
            mc_result = simulator.run(
                starting_bankroll=settings.trading.bankroll,
                win_rate=win_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                kelly_fraction=settings.trading.kelly_fraction,
                num_simulations=5000,
                trades_per_sim=200,
            )
            logger.info(
                f"Monte Carlo validation ({total_trades} historical trades): "
                f"win_rate={win_rate:.1%}, avg_win={avg_win:.3f}, avg_loss={avg_loss:.3f} | "
                f"median_bankroll=${mc_result.median_final_bankroll:.2f}, "
                f"dd95={mc_result.drawdown_95th:.1%}, "
                f"ruin={mc_result.probability_of_ruin:.1%}"
            )
            if mc_result.probability_of_ruin > 0.05:
                logger.warning(
                    f"Monte Carlo WARNING: ruin probability {mc_result.probability_of_ruin:.1%} "
                    f"exceeds 5% threshold — consider reducing kelly_fraction "
                    f"(currently {settings.trading.kelly_fraction}) or position sizing"
                )
        else:
            logger.info(
                f"Monte Carlo validation skipped: only {total_trades} historical trades "
                f"(need >= 10 for meaningful simulation)"
            )
    except Exception as e:
        logger.warning(f"Monte Carlo startup validation failed (non-fatal): {e}")

    # Phase 4: Start background tasks (WebSocket, dashboard)
    await _setup_background_tasks(settings, c, logger)

    # Graceful shutdown event
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
            c.scanner, c.kalshi, c.ai_strategy, c.no_strategy, c.news_strategy,
            c.cross_arb_strategy, c.whale_strategy, c.market_graph,
            c.risk_engine, c.kelly_sizer,
            c.circuit_breaker, c.order_builder, c.order_router, c.position_manager,
            c.calibration, c.resolution_tracker, c.calibration_analyzer,
            c.fill_tracker, c.alert_manager, c.daily_report, c.metrics,
            settings, settings.scanning.interval_seconds,
            poly_scanner=c.poly_scanner,
            cross_platform_arb=c.cross_platform_arb,
            mean_reversion_strategy=c.mean_reversion_strategy,
            late_resolution_strategy=c.late_resolution_strategy,
            price_monitor=c.price_monitor,
            shutdown_event=shutdown_event,
        )
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await _shutdown(c, logger)
        _release_pid_lock()
        logger.info("PolyEdge stopped.")
