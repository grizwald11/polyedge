"""PolyEdge — Main orchestrator.

Initializes all components and runs the scan → assess → trade loop.
Phase 3: Paper trading with AI probability + obvious NO strategies
through the risk engine and execution pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from src.config import load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import MarketDiscovery
from src.data.market_scanner import MarketScanner
from src.storage.database import Database
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.calibration import CalibrationTracker
from src.analysis.resolution_tracker import ResolutionTracker
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.strategies.ai_probability import AIProbabilityStrategy
from src.strategies.obvious_no import ObviousNoStrategy
from src.execution.order_builder import OrderBuilder
from src.execution.order_router import OrderRouter
from src.execution.position_manager import PositionManager
from src.risk.risk_engine import RiskEngine
from src.risk.kelly_sizer import KellySizer
from src.risk.circuit_breaker import CircuitBreaker


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
    ai_strategy: AIProbabilityStrategy,
    no_strategy: ObviousNoStrategy,
    risk_engine: RiskEngine,
    kelly_sizer: KellySizer,
    circuit_breaker: CircuitBreaker,
    order_builder: OrderBuilder,
    order_router: OrderRouter,
    position_manager: PositionManager,
    calibration: CalibrationTracker,
    resolution_tracker: ResolutionTracker,
    calibration_analyzer: CalibrationAnalyzer,
    settings,
    cycle_count: int = 0,
):
    """Execute one scan-assess-trade cycle."""
    logger = logging.getLogger("polyedge.main")
    bankroll = settings.trading.bankroll

    # Check circuit breaker
    if not circuit_breaker.check(bankroll):
        logger.warning("Circuit breaker active — skipping trade cycle")
        return

    # Scan and filter markets
    markets = await scanner.run_scan_cycle()
    if not markets:
        logger.info("No qualifying markets found")
        return

    # Generate signals from both strategies
    ai_signals = await ai_strategy.scan_for_opportunities(markets[:30])
    no_signals = no_strategy.scan_for_opportunities(markets)
    all_signals = ai_signals + no_signals

    if not all_signals:
        logger.info("No signals generated this cycle")
        return

    # Sort by edge descending — best opportunities first
    all_signals.sort(key=lambda s: abs(s.edge), reverse=True)

    logger.info(f"Processing {len(all_signals)} signals ({len(ai_signals)} AI, {len(no_signals)} NO)")

    # Build market lookup
    market_lookup = {m.ticker: m for m in markets}

    trades_executed = 0
    for signal in all_signals:
        market = market_lookup.get(signal.market_id)
        if market is None:
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

        # Apply circuit breaker multiplier
        cb_mult = circuit_breaker.get_kelly_multiplier()
        if cb_mult < 1.0:
            contracts = max(1, int(contracts * cb_mult))

        if contracts <= 0:
            continue

        # Calculate cost
        price = signal.market_price
        proposed_cost = price * contracts

        # Risk check
        risk_result = risk_engine.check_all(signal, market, contracts, proposed_cost)
        if not risk_result.passed:
            continue

        # Build order (prefer maker/limit)
        if settings.trading.prefer_maker:
            order = order_builder.build_limit_order(market, signal, contracts, price)
        else:
            order = order_builder.build_market_order(market, signal, contracts)

        # Route order
        result = await order_router.route_order(order)
        if result.success and result.trade:
            # Update position tracker
            position_manager.update_from_trade(result.trade, market.question)

            # Log calibration prediction
            calibration.log_prediction(
                market_id=signal.market_id,
                market_question=signal.market_question,
                predicted_probability=signal.probability_estimate,
                market_price=signal.market_price,
                strategy=signal.strategy,
            )

            # Log signal as acted on
            signal.acted_on = True
            signal.order_id = order.id
            scanner.db.log_signal(signal)

            trades_executed += 1

    logger.info(
        f"Cycle complete: {trades_executed} trades executed, "
        f"{position_manager.get_position_count()} open positions, "
        f"exposure: ${position_manager.get_total_exposure():.2f}"
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
            else:
                logger.info("Calibration: no resolved predictions yet")
        except Exception as e:
            logger.error(f"Calibration report failed: {e}")


async def run_trading_loop(
    scanner, ai_strategy, no_strategy, risk_engine, kelly_sizer,
    circuit_breaker, order_builder, order_router, position_manager,
    calibration, resolution_tracker, calibration_analyzer, settings, interval,
):
    """Run the scan-assess-trade loop on an interval."""
    logger = logging.getLogger("polyedge.main")
    cycle_count = 0

    while True:
        try:
            cycle_count += 1
            await scan_and_trade(
                scanner, ai_strategy, no_strategy, risk_engine, kelly_sizer,
                circuit_breaker, order_builder, order_router, position_manager,
                calibration, resolution_tracker, calibration_analyzer, settings,
                cycle_count,
            )
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

    # Strategies
    ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
    no_strategy = ObviousNoStrategy(settings)

    # Execution
    order_builder = OrderBuilder(settings)
    order_router = OrderRouter(settings, kalshi, db)
    position_manager = PositionManager(db, settings.trading.bankroll)

    # Risk
    circuit_breaker = CircuitBreaker(settings, db)
    kelly_sizer = KellySizer(settings)
    risk_engine = RiskEngine(settings, position_manager, circuit_breaker)

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

    # Enter trading loop
    logger.info(f"\nEntering trading loop (every {settings.scanning.interval_seconds}s)...")
    try:
        await run_trading_loop(
            scanner, ai_strategy, no_strategy, risk_engine, kelly_sizer,
            circuit_breaker, order_builder, order_router, position_manager,
            calibration, resolution_tracker, calibration_analyzer,
            settings, settings.scanning.interval_seconds,
        )
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await discovery.close()
        logger.info("PolyEdge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
