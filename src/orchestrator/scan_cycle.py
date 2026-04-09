"""Scan cycle — the core scan -> assess -> signal -> trade loop."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from src.core.models import (
    Market,
    MarketToken,
    Platform,
    TokenOutcome,
)
from src.orchestrator.startup import _check_disk_space, _sync_bankroll
from src.orchestrator.trade_cycle import _execute_signals, _process_exits


async def _check_fills_and_cleanup(fill_tracker, position_manager, order_router, settings, logger, kalshi=None) -> None:
    """Check for fills on pending orders and cancel stale ones."""
    # M-1: Automate Kalshi key freshness check every fill/cleanup cycle
    if kalshi is not None:
        try:
            kalshi.check_key_freshness()
        except Exception as e:
            logger.debug(f"Key freshness check in fill/cleanup: {e}")

    try:
        new_fills = await fill_tracker.check_fills()
        for fill in new_fills:
            position_manager.clear_pending_exit(fill.market_id)
            position_manager.update_from_trade(fill)
    except Exception as e:
        logger.error(f"Fill tracker check failed: {e}", exc_info=True)

    try:
        stale_cancelled = await order_router.cancel_stale_orders(
            max_age_seconds=settings.execution.stale_order_age_seconds
        )
        if stale_cancelled:
            logger.info(f"Cancelled {stale_cancelled} stale open orders")
    except Exception as e:
        logger.error(f"Stale order cancellation failed: {e}", exc_info=True)


async def _scan_markets(scanner, poly_scanner, metrics, logger) -> None:
    """Scan and filter markets from all platforms. Returns (markets, poly_markets)."""
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
        markets = []

    # M-14: Fall back to cached markets from database when scan returns empty
    if not markets:
        try:
            cached = scanner.db.get_active_markets()
            if cached:
                logger.warning(
                    f"M-14: Using {len(cached)} cached markets from database (scan failed or empty)"
                )
                # Convert cached dicts to Market objects
                cached_markets = []
                for row in cached:
                    try:
                        m = Market(
                            ticker=row["ticker"],
                            question=row.get("question", row["ticker"]),
                            tokens=[
                                MarketToken(
                                    token_id=f"{row['ticker']}_yes",
                                    outcome=TokenOutcome.YES,
                                    price=float(row.get("yes_price", 0)),
                                ),
                                MarketToken(
                                    token_id=f"{row['ticker']}_no",
                                    outcome=TokenOutcome.NO,
                                    price=float(row.get("no_price", 0)),
                                ),
                            ],
                        )
                        cached_markets.append(m)
                    except Exception:
                        continue
                if cached_markets:
                    markets = cached_markets
        except Exception as cache_err:
            logger.warning(f"M-14: Cached market fallback also failed: {cache_err}")

    if not markets:
        return None, poly_markets
    return markets, poly_markets


async def _update_position_prices(
    markets, position_manager, kalshi, poly_scanner, logger,
):
    """Update prices for all open positions, fetching missing ones from API."""
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

    # Fetch prices for positions not covered by the scan
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
                        if yes_price <= 0 and no_price <= 0:
                            logger.warning(f"No valid price data for {ticker} — skipping")
                            continue
                        position_manager.update_price(ticker, yes_price, no_price)
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


async def _generate_all_signals(
    markets, poly_markets, ai_strategy, no_strategy, news_strategy,
    cross_arb_strategy, whale_strategy, cross_platform_arb,
    alert_manager, metrics, logger,
    mean_reversion_strategy=None, late_resolution_strategy=None,
    macro_dip_sniper_strategy=None, regime_analysis=None,
):
    """Generate signals from all strategies. Returns (all_signals, ai_signals, no_signals)."""
    all_signals: list = []
    ai_signals: list = []
    no_signals: list = []
    _strategy_failures: list[str] = []
    _strategies_attempted = 2

    try:
        ai_signals = await ai_strategy.scan_for_opportunities(markets)
        all_signals.extend(ai_signals)
    except Exception as e:
        logger.error(f"AI probability strategy failed: {e}", exc_info=True)
        _strategy_failures.append("ai_probability")

    try:
        no_signals = no_strategy.scan_for_opportunities(markets)
        all_signals.extend(no_signals)
    except Exception as e:
        logger.error(f"Obvious NO strategy failed: {e}", exc_info=True)
        _strategy_failures.append("obvious_no")

    if news_strategy is not None:
        _strategies_attempted += 1
        try:
            news_signals = await news_strategy.scan_for_opportunities(markets)
            all_signals.extend(news_signals)
        except Exception as e:
            logger.error(f"News strategy failed: {e}", exc_info=True)
            _strategy_failures.append("news_reactive")

    if cross_arb_strategy is not None:
        _strategies_attempted += 1
        try:
            arb_signals = await cross_arb_strategy.scan_for_opportunities(markets)
            all_signals.extend(arb_signals)
        except Exception as e:
            logger.error(f"Cross-arb strategy failed: {e}", exc_info=True)
            _strategy_failures.append("cross_arb")

    if whale_strategy is not None:
        _strategies_attempted += 1
        try:
            whale_signals = whale_strategy.scan_for_opportunities(markets)
            all_signals.extend(whale_signals)
        except Exception as e:
            logger.error(f"Whale strategy failed: {e}", exc_info=True)
            _strategy_failures.append("whale_tracker")

    if cross_platform_arb is not None and poly_markets:
        _strategies_attempted += 1
        try:
            kalshi_markets = [m for m in markets if getattr(m, "platform", Platform.KALSHI) == Platform.KALSHI]
            xplat_signals = await cross_platform_arb.scan_for_opportunities(kalshi_markets, poly_markets)
            all_signals.extend(xplat_signals)
        except Exception as e:
            logger.error(f"Cross-platform arb strategy failed: {e}", exc_info=True)
            _strategy_failures.append("cross_platform_arb")

    if mean_reversion_strategy is not None:
        _strategies_attempted += 1
        try:
            mr_signals = mean_reversion_strategy.generate_signals(markets)
            all_signals.extend(mr_signals)
        except Exception as e:
            logger.error(f"Mean reversion strategy failed: {e}", exc_info=True)
            _strategy_failures.append("mean_reversion")

    if late_resolution_strategy is not None:
        _strategies_attempted += 1
        try:
            lr_signals = await late_resolution_strategy.generate_signals(markets)
            all_signals.extend(lr_signals)
        except Exception as e:
            logger.error(f"Late resolution strategy failed: {e}", exc_info=True)
            _strategy_failures.append("late_resolution")

    if macro_dip_sniper_strategy is not None:
        _strategies_attempted += 1
        try:
            dip_signals = await macro_dip_sniper_strategy.generate_signals(
                markets, regime_analysis=regime_analysis,
            )
            all_signals.extend(dip_signals)
        except Exception as e:
            logger.error(f"Macro dip sniper strategy failed: {e}", exc_info=True)
            _strategy_failures.append("macro_dip_sniper")

    # M-20: Warn if ANY strategies failed (degraded mode), escalate if ALL failed
    if _strategy_failures and _strategies_attempted > 0:
        if len(_strategy_failures) >= _strategies_attempted:
            fail_msg = (
                f"ALL {_strategies_attempted} strategies failed: {', '.join(_strategy_failures)}. "
                f"No signals can be generated until at least one strategy recovers."
            )
            logger.critical(fail_msg)
            if metrics is not None:
                metrics.record_error("all_strategies", fail_msg)
            try:
                await alert_manager.send_circuit_breaker_alert(
                    f"ALL STRATEGIES FAILED: {', '.join(_strategy_failures)}"
                )
            except Exception as e:
                logger.error(f"Failed to send strategy failure alert: {e}", exc_info=True)
        else:
            working = _strategies_attempted - len(_strategy_failures)
            logger.warning(
                f"DEGRADED MODE: {len(_strategy_failures)}/{_strategies_attempted} strategies "
                f"failed ({', '.join(_strategy_failures)}). Running with {working} strategy(ies)."
            )
            if metrics is not None:
                metrics.record_error("degraded_strategies", f"Failed: {', '.join(_strategy_failures)}")

    return all_signals, ai_signals, no_signals


async def _periodic_calibration_and_edge(
    calibration_analyzer, kelly_sizer, circuit_breaker,
    scanner, alert_manager, kalshi, logger,
    settings=None,
) -> None:
    """Run calibration report, edge tracker update, and key rotation check.

    Called every 10 cycles (~50 min) for periodic housekeeping.
    """
    # Calibration report
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

            kelly_sizer.update_calibration_multiplier(report.overall_brier)
            kelly_sizer.set_circuit_breaker_multiplier(circuit_breaker.get_kelly_multiplier())

            trend_status, trend_delta = calibration_analyzer.check_trend(report.overall_brier)
            if trend_status == "deteriorating":
                logger.warning(
                    f"Calibration DETERIORATING: Brier worsened by {trend_delta:+.3f} "
                    f"(now {report.overall_brier:.3f})"
                )
                await alert_manager.send_error_alert(
                    error=f"Calibration deteriorating: Brier score worsened by {trend_delta:+.3f} to {report.overall_brier:.3f}",
                    context="Consider reviewing recent predictions and prompt templates",
                )
            elif trend_status == "improving":
                logger.info(
                    f"Calibration improving: Brier improved by {trend_delta:+.3f} "
                    f"(now {report.overall_brier:.3f})"
                )

            # Absolute Brier score threshold alert
            brier_threshold = 0.30
            if settings is not None:
                brier_threshold = settings.alerts.brier_alert_threshold
            if report.overall_brier > brier_threshold and report.total_resolved >= 10:
                logger.critical(
                    f"STRATEGY DEGRADATION: Brier score {report.overall_brier:.3f} "
                    f"exceeds threshold {brier_threshold:.2f} "
                    f"({report.total_resolved} resolved predictions)"
                )
                await alert_manager.send_error_alert(
                    error=(
                        f"Strategy degradation: Brier score {report.overall_brier:.3f} "
                        f"exceeds {brier_threshold:.2f} threshold"
                    ),
                    context=(
                        f"Based on {report.total_resolved} resolved predictions. "
                        f"Win rate: {report.overall_win_rate:.1%}. "
                        f"Consider pausing AI strategy and reviewing prompt templates."
                    ),
                )
        else:
            logger.info("Calibration: no resolved predictions yet")
    except Exception as e:
        logger.error(f"Calibration report failed: {e}", exc_info=True)

    # Edge tracker: update Kelly edge multiplier based on realized vs predicted edge
    try:
        from src.analysis.edge_tracker import EdgeTracker
        edge_tracker = EdgeTracker(scanner.db)
        shrinkage = edge_tracker.compute_edge_shrinkage()
        kelly_sizer.set_edge_multiplier(shrinkage)
        summary = edge_tracker.get_summary()
        if summary["count"] > 0:
            logger.info(
                f"Edge tracker: {summary['count']} resolved, "
                f"avg_predicted={summary['avg_predicted']:.3f}, "
                f"avg_realized={summary['avg_realized']:.3f}, "
                f"shrinkage={summary['shrinkage']:.2f}, "
                f"win_rate={summary['win_rate']:.1%}"
            )
    except Exception as e:
        logger.debug(f"Edge tracker update skipped: {e}")

    # Key rotation check — detect rotated credentials
    try:
        if not kalshi.check_key_freshness():
            logger.warning("Kalshi private key was rotated — reloaded automatically")
            try:
                await alert_manager.send_error_alert(
                    error="Kalshi API key rotated (periodic check)",
                    context="Key file modified on disk. Reloaded for next request.",
                )
            except Exception as e:
                logger.debug(f"Key rotation alert failed: {e}")
    except Exception as e:
        logger.debug(f"Key freshness check failed: {e}")


async def _check_closed_markets(markets, position_manager, alert_manager, logger) -> None:
    """Check if any position markets have become closed/settled and mark for exit."""
    market_by_ticker = {m.ticker: m for m in markets}
    for pos in list(position_manager.get_all_positions()):
        m = market_by_ticker.get(pos.market_id)
        if m is not None and not m.active:
            logger.warning(
                f"Position market {pos.market_id} is no longer active "
                f"(status={getattr(m, 'status', 'unknown')}) — marking for exit"
            )
            position_manager.mark_pending_exit(pos.market_id)
            if alert_manager:
                try:
                    await alert_manager.send(
                        f"MARKET CLOSED: {pos.market_id} — position held, marking for exit",
                        level="warning",
                    )
                except Exception as e:
                    logger.debug(f"Alert delivery failed (best-effort): {e}")


async def _sync_positions_live(settings, position_manager, kalshi, circuit_breaker, logger) -> None:
    """Sync positions with Kalshi in live mode to prevent desync."""
    if settings.trading.mode != "live":
        return
    try:
        mismatches = await position_manager.sync_with_kalshi(kalshi)
        if mismatches:
            logger.warning(f"Position sync found {mismatches} mismatches")
        position_manager._consecutive_sync_failures = 0
    except Exception as e:
        logger.error(f"Position sync failed: {e}", exc_info=True)
        position_manager._consecutive_sync_failures = getattr(
            position_manager, "_consecutive_sync_failures", 0
        ) + 1
        if position_manager._consecutive_sync_failures >= 3:
            logger.critical(
                "Position sync failed 3+ consecutive times — halting trading. "
                "Positions may be desynced with Kalshi."
            )
            circuit_breaker.trigger_halt("Position sync failed 3+ times")


async def scan_and_trade(
    scanner,
    kalshi,
    ai_strategy,
    no_strategy,
    news_strategy,
    cross_arb_strategy,
    whale_strategy,
    market_graph,
    risk_engine,
    kelly_sizer,
    circuit_breaker,
    order_builder,
    order_router,
    position_manager,
    calibration,
    resolution_tracker,
    calibration_analyzer,
    fill_tracker,
    alert_manager,
    metrics,
    settings,
    cycle_count: int = 0,
    poly_scanner=None,
    cross_platform_arb=None,
    mean_reversion_strategy=None,
    late_resolution_strategy=None,
    macro_dip_sniper_strategy=None,
    price_monitor=None,
):
    """Execute one complete scan-assess-trade cycle.

    Decomposed into sub-functions for maintainability:
    1. _sync_bankroll — live mode balance reconciliation
    2. _check_fills_and_cleanup — fill tracking + stale order cancellation
    3. _scan_markets — multi-platform market discovery
    4. _update_position_prices — refresh P&L for open positions
    5. _process_exits — close positions at stop-loss/time/edge-gone thresholds
    6. _generate_all_signals — run all strategy engines
    7. _execute_signals — size, risk-check, and route orders
    """
    logger = logging.getLogger("polyedge.main")
    _cycle_start = time.time()
    bankroll = settings.trading.bankroll

    # L-7: Disk space check
    _check_disk_space(settings, logger)

    # 1. Sync bankroll
    bankroll = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, bankroll, logger)

    # H-7: Auto-check Kalshi key freshness every cycle
    try:
        key_fresh = kalshi.check_key_freshness()
        if not key_fresh:
            logger.warning("Kalshi private key was rotated — reloaded automatically")
            try:
                await alert_manager.send_error_alert(
                    error="Kalshi API key was rotated on disk and reloaded",
                    context="Key file was modified since last load. New key will be used for next request.",
                )
            except Exception as e:
                logger.debug(f"Key rotation alert failed: {e}")
    except Exception as e:
        logger.debug(f"Key freshness check: {e}")

    # 2. Check fills and cleanup stale orders
    await _check_fills_and_cleanup(fill_tracker, position_manager, order_router, settings, logger, kalshi=kalshi)

    # 2b. Resolution tracking — runs even when circuit breaker is active,
    # so calibration data keeps accumulating during trading halts.
    try:
        resolved = await resolution_tracker.check_resolutions()
        if resolved > 0:
            logger.info(f"Resolved {resolved} markets (pre-circuit-breaker check)")
    except Exception as e:
        logger.debug(f"Early resolution check failed: {e}")

    # 3. Circuit breaker check
    unrealized_pnl = position_manager.get_total_unrealized_pnl()
    if not circuit_breaker.check(bankroll, unrealized_pnl=unrealized_pnl):
        logger.warning("Circuit breaker active — skipping trade cycle")
        if settings.alerts.alert_on_circuit_breaker:
            try:
                await alert_manager.send_circuit_breaker_alert(
                    circuit_breaker.halt_reason or "Unknown"
                )
            except Exception as e:
                logger.error(f"Failed to send circuit breaker alert: {e}", exc_info=True)
        return

    # 3a. Pre-halt warnings (loss velocity, low balance, drawdown approaching limit)
    try:
        warnings = circuit_breaker.get_warnings(bankroll, unrealized_pnl=unrealized_pnl)
        for w in warnings:
            logger.warning(f"Risk warning: {w['message']}")
            if w["type"] == "loss_velocity":
                await alert_manager.send_loss_velocity_alert(
                    daily_pnl=w["daily_pnl"],
                    daily_limit=w["daily_limit"],
                    pct_of_limit=w["pct_of_limit"],
                )
            elif w["type"] == "low_balance":
                await alert_manager.send_low_balance_alert(
                    bankroll=w["bankroll"],
                    initial_bankroll=w["initial_bankroll"],
                )
            elif w["type"] == "drawdown_warning":
                await alert_manager.send_error_alert(
                    error=w["message"],
                    context="Drawdown approaching circuit breaker threshold",
                )
    except Exception as e:
        logger.debug(f"Warning check failed: {e}")

    # 3b. Regime detection — adaptive thresholds based on market volatility
    regime_analysis = None
    try:
        from src.analysis.regime_detector import RegimeDetector
        _regime_detector = RegimeDetector()
        regime_analysis = _regime_detector.detect_regime(scanner.db)
        # Apply regime multiplier to Kelly sizer
        kelly_sizer.set_regime_multiplier(regime_analysis.multipliers.kelly_multiplier)
        # Pass regime edge multiplier to AI strategy
        ai_strategy.set_regime_edge_multiplier(regime_analysis.multipliers.edge_multiplier)
    except Exception as e:
        logger.debug(f"Regime detection skipped: {e}")

    # 4. Scan markets
    markets, poly_markets = await _scan_markets(scanner, poly_scanner, metrics, logger)
    if markets is None:
        return  # Scan failed
    if not markets:
        logger.info("No qualifying markets found")
        return

    # 5. Update position prices
    await _update_position_prices(markets, position_manager, kalshi, poly_scanner, logger)

    # 5a. Sync price monitor with current positions for adverse move detection
    if price_monitor is not None:
        try:
            price_monitor.update_positions(position_manager.get_all_positions())
        except Exception as e:
            logger.debug(f"Price monitor sync failed: {e}")

    # 5b. Bayesian belief updates on open positions
    try:
        from src.analysis.bayesian_updater import BayesianUpdater
        _bayesian_updater = BayesianUpdater()
        for pos in position_manager.get_all_positions():
            # Only update if we have a stored belief (from a previous assessment)
            belief = getattr(pos, '_belief_state', None)
            if belief is None:
                continue
            old_price = belief.probability  # Use belief as proxy for last known price
            result = _bayesian_updater.update_from_price_movement(
                belief, pos.current_price, old_price,
            )
            if result.should_exit:
                logger.info(
                    f"Bayesian exit signal for {pos.market_id}: {result.exit_reason} "
                    f"(prior={result.prior:.2f}, posterior={result.posterior:.2f})"
                )
            elif result.should_reassess:
                logger.info(
                    f"Bayesian reassess signal for {pos.market_id}: "
                    f"shift={result.shift:+.3f} exceeds threshold"
                )
    except Exception as e:
        logger.debug(f"Bayesian update step skipped: {e}")

    # Build market lookup
    market_lookup = {m.ticker: m for m in markets}

    # 6. Process exits
    await _process_exits(
        position_manager, market_lookup, scanner, settings, kalshi,
        poly_scanner, order_builder, order_router, circuit_breaker,
        fill_tracker, risk_engine, alert_manager, metrics, logger,
    )

    # Index markets in graph
    if market_graph is not None:
        try:
            market_graph.index_markets(markets)
        except Exception as e:
            logger.warning(f"Market graph indexing failed: {e}")

    # 7. Generate signals
    all_signals, ai_signals, no_signals = await _generate_all_signals(
        markets, poly_markets, ai_strategy, no_strategy, news_strategy,
        cross_arb_strategy, whale_strategy, cross_platform_arb,
        alert_manager, metrics, logger,
        mean_reversion_strategy=mean_reversion_strategy,
        late_resolution_strategy=late_resolution_strategy,
        macro_dip_sniper_strategy=macro_dip_sniper_strategy,
        regime_analysis=regime_analysis,
    )

    if not all_signals:
        logger.info("No signals generated this cycle")
        if metrics is not None:
            metrics.record_cycle(
                duration_ms=(time.time() - _cycle_start) * 1000,
                trades=0, signals=0,
                positions=position_manager.get_position_count(),
            )
            metrics.persist_to_db(scanner.db)
        return

    # 8. Execute signals
    trades_executed = await _execute_signals(
        all_signals, ai_signals, no_signals, markets, market_lookup,
        scanner, settings, bankroll, kelly_sizer, circuit_breaker,
        order_builder, order_router, risk_engine, position_manager,
        calibration, fill_tracker, alert_manager, logger,
        metrics=metrics,
    )

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
        metrics.persist_to_db(scanner.db)

    # Check for resolved markets
    try:
        resolved = await resolution_tracker.check_resolutions()
        if resolved > 0:
            logger.info(f"Resolved {resolved} markets this cycle")
    except Exception as e:
        logger.error(f"Resolution check failed: {e}", exc_info=True)

    # Periodic housekeeping every 10 cycles (~50 min)
    if cycle_count > 0 and cycle_count % 10 == 0:
        await _periodic_calibration_and_edge(
            calibration_analyzer, kelly_sizer, circuit_breaker,
            scanner, alert_manager, kalshi, logger,
            settings=settings,
        )

    # WAL checkpoint every 12 cycles (~1 hour) to prevent unbounded WAL growth
    if cycle_count > 0 and cycle_count % 12 == 0:
        try:
            scanner.db.wal_checkpoint()
        except Exception as e:
            logger.debug(f"WAL checkpoint skipped: {e}")

    # Automated backup every 288 cycles (~24 hours)
    if cycle_count > 0 and cycle_count % 288 == 0:
        try:
            backup_path = scanner.db.backup()
            logger.info(f"Automated daily backup: {backup_path}")
            # Clean up backups older than 7 days
            _cleanup_old_backups(scanner.db.db_path, max_age_days=7, logger=logger)
        except Exception as e:
            logger.error(f"Automated backup failed: {e}")

    # Check for closed/settled position markets and sync with Kalshi
    await _check_closed_markets(markets, position_manager, alert_manager, logger)
    await _sync_positions_live(settings, position_manager, kalshi, circuit_breaker, logger)


def _cleanup_old_backups(db_path: str, max_age_days: int = 7, logger=None) -> int:
    """Remove backup files older than max_age_days. Returns count deleted."""
    import glob
    backup_pattern = f"{db_path}.bak-*"
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    for path in glob.glob(backup_pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                deleted += 1
        except OSError:
            pass
    if deleted and logger:
        logger.info(f"Cleaned up {deleted} old backup(s)")
    return deleted
