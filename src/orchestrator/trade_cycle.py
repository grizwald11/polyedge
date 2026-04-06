"""Trade cycle — risk checks, order building, signal execution, and exit processing."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from src.core.models import (
    Direction,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    StrategyName,
)


async def _process_exits(
    position_manager, market_lookup, scanner, settings, kalshi,
    poly_scanner, order_builder, order_router, circuit_breaker,
    fill_tracker, risk_engine, alert_manager, metrics, logger,
):
    """Process exit candidates — close positions that hit stop-loss, time limit, or lost edge."""
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

        if position_manager.has_pending_exit(position.market_id):
            logger.debug(f"Skipping exit for {position.market_id}: resting exit order already in flight")
            continue

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
                            market = pm
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
            cost=round(exit_price * position.size, 4),
            order_type=OrderType.GTC if settings.trading.prefer_maker else OrderType.FOK,
            status=OrderStatus.PENDING,
            strategy=position.strategy,
            paper=position.paper,
            created_at=datetime.now(timezone.utc),
        )

        if circuit_breaker.is_halted() and "stop_loss" not in exit_reason.lower():
            logger.warning(f"Skipping exit for {position.market_id}: circuit breaker active (non-stop-loss)")
            continue

        result = await order_router.route_order(exit_order)
        if result is not None and result.success and result.trade is None:
            fill_tracker.track(exit_order)
            position_manager.mark_pending_exit(position.market_id)
        if result is not None and result.success and result.trade:
            position_manager.clear_pending_exit(position.market_id)
            position_manager.update_from_trade(result.trade)
            risk_engine.record_exit(position.market_id, pnl=result.trade.realized_pnl, exit_reason=exit_reason)
            # Record edge-vs-return for M-2 metrics tracking
            if metrics is not None and result.trade:
                cost_basis = position.avg_entry_price * position.size
                realized_return = (
                    result.trade.realized_pnl / cost_basis if cost_basis > 0 else 0.0
                )
                days_held = (
                    (datetime.now(timezone.utc) - position.opened_at).total_seconds() / 86400
                )
                metrics.record_closed_position(
                    market_id=position.market_id,
                    predicted_edge=0.0,  # Edge stored in signals table, not position
                    realized_return=realized_return,
                    days_held=days_held,
                )
            logger.info(f"[EXIT] {position.market_id} — {exit_reason}")
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
                logger.warning(f"Failed to log exit reason for {position.market_id}: {e}")
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


async def _execute_signals(
    all_signals, ai_signals, no_signals, markets, market_lookup,
    scanner, settings, bankroll, kelly_sizer, circuit_breaker,
    order_builder, order_router, risk_engine, position_manager,
    calibration, fill_tracker, alert_manager, logger,
    metrics=None,
):
    """Execute trades for generated signals. Returns trades_executed count."""
    # ── Signal deconfliction ──
    # When multiple strategies generate signals for the same market,
    # select the highest-quality signal (by abs(edge) * confidence)
    # to prevent the "execution order lottery" (C-10).
    signals_by_market: dict[str, list] = defaultdict(list)
    for sig in all_signals:
        signals_by_market[sig.market_id].append(sig)

    deconflicted_signals: list = []
    for market_id, sigs in signals_by_market.items():
        if len(sigs) == 1:
            deconflicted_signals.append(sigs[0])
        else:
            # Check for contradictions (some BUY_YES, some BUY_NO)
            directions = {s.direction for s in sigs}
            buy_yes = {Direction.BUY_YES, Direction.SELL_NO}
            buy_no = {Direction.BUY_NO, Direction.SELL_YES}
            has_yes = bool(directions & buy_yes)
            has_no = bool(directions & buy_no)

            if has_yes and has_no:
                # Contradictory signals — skip this market entirely
                strategy_names = [s.strategy.value for s in sigs]
                logger.warning(
                    f"Signal conflict on {market_id}: strategies {strategy_names} "
                    f"disagree on direction — skipping market this cycle"
                )
                continue

            # Same direction — pick the strongest signal
            best = max(sigs, key=lambda s: abs(s.edge) * s.confidence)
            logger.info(
                f"Deconflicted {len(sigs)} signals for {market_id}: "
                f"selected {best.strategy.value} (edge={best.edge:.1%})"
            )
            deconflicted_signals.append(best)

    all_signals = deconflicted_signals

    # Separate obvious_no from other signals so they get their own trade slot
    edge_signals = [s for s in all_signals if s.strategy != StrategyName.OBVIOUS_NO]
    obvious_no_signals = [s for s in all_signals if s.strategy == StrategyName.OBVIOUS_NO]

    edge_signals.sort(key=lambda s: abs(s.edge), reverse=True)
    obvious_no_signals.sort(key=lambda s: abs(s.edge), reverse=True)

    max_trades = settings.trading.max_trades_per_cycle
    no_slots = min(2, len(obvious_no_signals)) if obvious_no_signals else 0
    max_edge_trades = max_trades - no_slots

    ordered_signals = edge_signals + obvious_no_signals

    logger.info(
        f"Processing {len(all_signals)} signals "
        f"({len(ai_signals)} AI, {len(no_signals)} NO, "
        f"{len(edge_signals)} edge, {len(obvious_no_signals)} obvious_no)"
    )

    trades_executed = 0
    edge_trades = 0
    acted_markets: set[str] = set()
    for signal in ordered_signals:
        if trades_executed >= max_trades:
            logger.info(f"Max trades per cycle ({max_trades}) reached — deferring remaining signals")
            break
        is_obvious_no = signal.strategy == StrategyName.OBVIOUS_NO
        if not is_obvious_no and edge_trades >= max_edge_trades:
            continue

        if signal.market_id in acted_markets:
            logger.debug(f"Skipping duplicate signal for {signal.market_id}")
            continue

        if scanner.db.has_recent_trade(signal.market_id):
            logger.info(f"Skipping {signal.market_id}: recent trade exists (dedup)")
            continue

        signal_id = scanner.db.log_signal(signal)
        # M-10: Track edge for all generated signals (including those that will be gated)
        if metrics is not None:
            metrics.record_signal_generated(edge=signal.edge)

        market = market_lookup.get(signal.market_id)
        if market is None:
            logger.warning(
                f"Signal for unknown market {signal.market_id} "
                f"(strategy={signal.strategy.value}) — skipped"
            )
            continue

        current_exposure = position_manager.get_total_exposure()
        strategy_name = signal.strategy.value if hasattr(signal.strategy, 'value') else str(signal.strategy)
        contracts = kelly_sizer.calculate_position_size(
            edge=signal.edge,
            probability=signal.probability_estimate,
            bankroll=bankroll,
            current_exposure=current_exposure,
            order_price=signal.market_price,
            confidence=signal.confidence,
            strategy=strategy_name,
        )

        # M-12: Log Kelly sizing details for debugging and audit
        if contracts > 0:
            cost_price = max(signal.probability_estimate - signal.edge, signal.market_price) if signal.market_price > 0 else (signal.probability_estimate - signal.edge)
            bankroll_fraction = (contracts * cost_price) / bankroll if bankroll > 0 else 0.0
            logger.debug(
                f"Kelly details for {signal.market_id}: edge={signal.edge:.3f}, "
                f"prob={signal.probability_estimate:.3f}, bankroll_frac={bankroll_fraction:.4f}, "
                f"final_contracts={contracts}, cost_price=${cost_price:.3f}, "
                f"strategy={strategy_name}"
            )

        # NOTE: Circuit breaker multiplier is already applied inside
        # kelly_sizer via set_circuit_breaker_multiplier(). Do NOT apply
        # it again here — that would double-penalize during drawdowns.

        if contracts <= 0:
            logger.debug(
                f"Kelly sized to 0 contracts for {signal.market_id} "
                f"(exposure=${current_exposure:.2f}, edge={signal.edge:.1%})"
            )
            continue

        price = signal.market_price
        if settings.trading.prefer_maker:
            order = order_builder.build_limit_order(market, signal, contracts, price)
        else:
            order = order_builder.build_market_order(market, signal, contracts)
        if order is None:
            logger.warning(f"Could not build order for {signal.market_id} — missing tokens")
            continue
        proposed_cost = order.cost

        risk_result = risk_engine.check_all(signal, market, contracts, proposed_cost)
        # H-1/H-5: Persist risk gate results for every signal
        scanner.db.update_signal_risk_result(
            signal_id, risk_result.passed,
            risk_result.failed_checks, risk_result.warnings,
        )
        if not risk_result.passed:
            # M-10: Track gated signal edges for comparison with executed edges
            if metrics is not None:
                metrics.record_signal_risk_gated(edge=signal.edge)
            continue

        result = await order_router.route_order(order)
        if result is not None and result.success and result.trade is None:
            fill_tracker.track(order)
        if result is not None and result.success and result.trade:
            position_manager.update_from_trade(result.trade, market.question)

            if signal.direction in (Direction.BUY_NO, Direction.SELL_NO):
                cal_probability = 1.0 - signal.probability_estimate
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

            scanner.db.update_signal_acted_on(signal_id, order.id)

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
            if metrics is not None:
                metrics.record_signal_executed()

    return trades_executed
