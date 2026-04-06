"""Position guard — inter-cycle stop-loss monitoring.

Runs every 30s between the main 300s scan cycles.  Fetches current prices
for open positions and triggers exits when stop-loss thresholds are breached.

This closes the gap where the main cycle's 5-minute interval lets positions
lose 60-90% before the stop-loss fires.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from decimal import ROUND_HALF_UP, Decimal

from src.core.models import (
    Direction,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    dollars_to_cents,
    kalshi_maker_fee,
    kalshi_taker_fee,
)
from src.execution.order_builder import OrderBuilder
from src.execution.order_router import OrderRouter
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.risk_engine import RiskEngine

logger = logging.getLogger(__name__)

# How often to check positions (seconds)
CHECK_INTERVAL = 30


async def run_position_guard(
    position_manager: PositionManager,
    kalshi,
    order_builder: OrderBuilder,
    order_router: OrderRouter,
    circuit_breaker: CircuitBreaker,
    risk_engine: RiskEngine,
    settings,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Lightweight loop: check open positions every CHECK_INTERVAL seconds.

    Only processes stop-loss exits — other exit types (take-profit, edge-gone,
    time-based) are handled by the main scan cycle.
    """
    prefer_maker = getattr(settings.trading, "prefer_maker", True)

    while not (shutdown_event and shutdown_event.is_set()):
        try:
            positions = position_manager.get_all_positions()
            if not positions:
                await _sleep_or_shutdown(shutdown_event)
                continue

            for position in positions:
                if position_manager.has_pending_exit(position.market_id):
                    continue

                # Fetch latest price from Kalshi
                try:
                    raw = await kalshi.get_market(position.market_id)
                    if not raw:
                        continue
                    yes_price = float(raw.get("yes_bid_dollars") or raw.get("yes_bid") or 0)
                    no_price = float(raw.get("no_bid_dollars") or raw.get("no_bid") or 0)
                    if yes_price <= 0 and no_price <= 0:
                        continue
                    if no_price <= 0:
                        no_price = 1.0 - yes_price
                    if yes_price <= 0:
                        yes_price = 1.0 - no_price

                    position_manager.update_price(
                        position.market_id, yes_price, no_price,
                    )
                except Exception as e:
                    logger.debug(f"Position guard: price fetch failed for {position.market_id}: {e}")
                    continue

                # Check stop-loss only (the main cycle handles other exits)
                should_exit, reason = position_manager.should_exit(position)
                if not should_exit or "stop_loss" not in reason:
                    continue

                # Execute exit
                if position.direction in (Direction.BUY_YES, Direction.SELL_NO):
                    exit_price = yes_price
                else:
                    exit_price = no_price

                if exit_price <= 0:
                    continue

                logger.warning(
                    f"Position guard EXIT: {position.market_id} — {reason} "
                    f"(entry={position.avg_entry_price:.2f}, now={exit_price:.2f})"
                )

                exit_platform = getattr(position, "platform", Platform.KALSHI)
                if exit_platform == Platform.POLYMARKET:
                    fee_dollars = 0.0
                else:
                    price_cents = dollars_to_cents(exit_price)
                    if prefer_maker:
                        fee_dollars = kalshi_maker_fee(position.size, price_cents) / 100.0
                    else:
                        fee_dollars = kalshi_taker_fee(position.size, price_cents) / 100.0
                exit_cost = float(
                    (Decimal(str(exit_price)) * Decimal(str(position.size))
                     + Decimal(str(fee_dollars)))
                    .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                )

                exit_order = Order(
                    id=order_builder._generate_order_id(),
                    market_id=position.market_id,
                    platform=exit_platform,
                    token_id=position.token_id,
                    side=Side.SELL,
                    price=exit_price,
                    size=position.size,
                    cost=exit_cost,
                    order_type=OrderType.GTC if prefer_maker else OrderType.FOK,
                    status=OrderStatus.PENDING,
                    strategy=position.strategy,
                    paper=position.paper,
                    created_at=datetime.now(timezone.utc),
                )

                result = await order_router.route_order(exit_order)
                if result is not None and result.success and result.trade:
                    position_manager.update_from_trade(result.trade)
                    risk_engine.record_exit(
                        position.market_id, pnl=result.trade.realized_pnl
                    )
                elif result is not None and result.success:
                    position_manager.mark_pending_exit(position.market_id)

        except Exception as e:
            logger.error(f"Position guard cycle failed: {e}", exc_info=True)

        await _sleep_or_shutdown(shutdown_event)


async def _sleep_or_shutdown(
    shutdown_event: asyncio.Event | None,
) -> None:
    """Sleep for CHECK_INTERVAL or until shutdown is requested."""
    try:
        if shutdown_event:
            await asyncio.wait_for(shutdown_event.wait(), timeout=CHECK_INTERVAL)
        else:
            await asyncio.sleep(CHECK_INTERVAL)
    except asyncio.TimeoutError:
        pass
