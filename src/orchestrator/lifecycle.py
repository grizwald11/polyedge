"""Lifecycle — trading loop, shutdown handling, day boundary logic, main() entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

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
from src.core.models import Platform
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
from src.orchestrator.startup import _acquire_pid_lock, _release_pid_lock, setup_logging
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.kelly_sizer import KellySizer
from src.risk.portfolio_risk import PortfolioRisk
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database
from src.strategies.ai_probability import AIProbabilityStrategy
from src.strategies.cross_arb import CrossArbStrategy
from src.strategies.cross_platform_arb import CrossPlatformArbStrategy
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
                f"WARNING: orders submitted before timeout may be resting on exchange but untracked locally.",
                exc_info=True,
            )
            # After timeout, sync positions to detect any orphaned orders
            if settings.trading.mode == "live":
                try:
                    mismatches = await position_manager.sync_with_kalshi(kalshi)
                    if mismatches:
                        logger.warning(f"Post-timeout sync found {mismatches} position mismatches")
                except Exception as sync_err:
                    logger.error(f"Post-timeout position sync failed: {sync_err}", exc_info=True)
                    logger.critical(
                        "CRITICAL: Post-timeout position sync also failed. "
                        "Positions may be desynced. Triggering circuit breaker."
                    )
                    circuit_breaker.trigger_halt("Post-timeout position sync failed")
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
    if not _acquire_pid_lock():
        logging.critical("Another PolyEdge instance is already running (PID lock exists). Exiting.")
        sys.exit(1)

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
    # Validate Anthropic API key works before starting trading loop
    if settings.anthropic_api_key:
        try:
            await forecaster.health_check()
            logger.info("Anthropic API: key validated successfully")
        except Exception as e:
            logger.critical(
                f"Anthropic API key validation FAILED: {e}. "
                "WARNING: The bot will have DEGRADED SIGNAL GENERATION. "
                "AI probability, cross-market arbitrage validation, and news-reactive "
                "strategies will NOT produce signals until the Anthropic API is reachable. "
                "Check ANTHROPIC_API_KEY and API status at https://status.anthropic.com",
                exc_info=True,
            )
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
            from src.data.polymarket_cross_ref import PolymarketCrossRef
            from src.data.polymarket_scanner import PolymarketScanner

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
    position_manager = PositionManager(
        db,
        bankroll=settings.trading.bankroll,
        stop_loss_pct=settings.execution.stop_loss_pct,
        max_hold_days=settings.execution.max_hold_days,
        edge_gone_threshold=settings.execution.edge_gone_threshold,
        trailing_stop_activate=settings.execution.trailing_stop_activate,
        trailing_stop_distance=settings.execution.trailing_stop_distance,
        take_profit_pct=settings.execution.take_profit_pct,
        capital_rotation_edge=settings.execution.capital_rotation_edge,
    )
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

        # M-25: Check for orphaned orders from a previous crash
        try:
            open_orders = await asyncio.wait_for(kalshi.get_open_orders(), timeout=10.0)
            if open_orders:
                logger.warning(
                    f"Found {len(open_orders)} open orders on Kalshi at startup — "
                    f"these may be orphaned from a previous crash. "
                    f"Order IDs: {[o.get('order_id', '?') for o in open_orders[:5]]}"
                )
        except Exception as e:
            logger.info(f"Could not check for orphaned orders: {e}")

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
    risk_engine.restore_bankroll()  # Restore live-synced bankroll from DB

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

            async def _on_ws_reconnect():
                """Sync tracked market statuses via REST after WebSocket reconnects.

                Markets may have closed or settled while disconnected — query
                Kalshi REST API to catch any missed lifecycle changes.
                """
                logger.info("WebSocket reconnected — syncing tracked market statuses via REST")
                try:
                    tracked = list(ws_client._subscriptions)
                    for ticker in tracked:
                        try:
                            market_data = await kalshi.get_market(ticker)
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
                    # Cancel any resting orders on this market to prevent
                    # stale fills (H-2).
                    resting = fill_tracker.get_pending_for_market(
                        update.market_ticker
                    )
                    for order in resting:
                        try:
                            await kalshi.cancel_order(order.id)
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
                    if position_manager.has_position(update.market_ticker):
                        logger.warning(
                            f"Market {update.market_ticker} settled via WebSocket "
                            f"(status={update.status}, settlement={update.settlement_value}) "
                            f"— marking for exit"
                        )
                        # Record settlement value for accurate P&L calculation
                        if update.settlement_value is not None:
                            position_manager.record_settlement(
                                update.market_ticker, update.settlement_value
                            )
                        position_manager.mark_pending_exit(update.market_ticker)

            ws_client.on_price_update(_on_price)
            ws_client.on_fill(_on_fill)
            ws_client.on_lifecycle(_on_lifecycle)
            ws_client.register_reconnect_sync(_on_ws_reconnect)
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
                logger.warning(f"WebSocket task cleanup error: {e}")

        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Dashboard task cleanup error: {e}")

        try:
            await forecaster.close()
        except Exception as e:
            logger.warning(f"Forecaster close failed: {e}")

        try:
            await discovery.close()
        except Exception as e:
            logger.warning(f"Discovery close failed: {e}")

        try:
            await kalshi.close()
        except Exception as e:
            logger.warning(f"Kalshi client close failed: {e}")

        if polymarket_client is not None:
            try:
                await polymarket_client.close()
            except Exception as e:
                logger.warning(f"Polymarket client close failed: {e}")

        try:
            db.close()
        except Exception as e:
            logger.warning(f"Database close failed: {e}")

        _release_pid_lock()
        logger.info("PolyEdge stopped.")
