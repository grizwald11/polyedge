"""Polymarket live order router -- handles live order submission to Polymarket CLOB API.

Extracted from order_router.py (M-11) to isolate Polymarket-specific execution
logic (residency gate, CLOB order creation, retry logic).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.core.models import (
    OrderStatus,
    OrderType,
    Platform,
    Trade,
)
from src.core.retry_helper import retry_with_backoff

if TYPE_CHECKING:
    from src.core.models import Order
    from src.execution.order_router import OrderResult, OrderRouter

logger = logging.getLogger(__name__)


async def poly_live_fill(router: OrderRouter, order: Order) -> OrderResult:
    """Submit order to Polymarket CLOB API for live execution.

    Args:
        router: The parent OrderRouter instance (for shared state, DB, settings).
        order: The order to execute on Polymarket.
    """
    from src.execution.order_router import OrderResult

    # C-3: Reject orders on closed/settled markets.
    market_row = router.db.get_market(order.market_id)
    if market_row is not None:
        mkt_closed = market_row.get("closed") or market_row.get("active") == 0
        mkt_status = str(market_row.get("status", "")).lower()
        if mkt_closed or mkt_status in ("closed", "settled", "halted", "determined"):
            order.status = OrderStatus.REJECTED
            order.rejection_reason = f"Market is {mkt_status or 'closed'} -- cannot trade"
            router._log_order(order)
            logger.warning(f"Order {order.id} rejected: {order.rejection_reason}")
            return OrderResult(success=False, order=order, error=order.rejection_reason)

    if router.polymarket is None:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = "Polymarket client not configured"
        router._log_order(order)
        return OrderResult(success=False, order=order, error="Polymarket client not configured")

    # Polymarket residency gate: require explicit non-US confirmation.
    # Polymarket is not legal for US persons -- this gate prevents accidental
    # live trades without jurisdiction acknowledgment.
    # NOTE: Intentionally uses env var (not config file) as a safety gate --
    # env vars are harder to accidentally change and require explicit action.
    if not router._polymarket_residency_confirmed:
        if os.environ.get("CONFIRM_NON_US_POLYMARKET", "").lower() != "true":
            order.status = OrderStatus.REJECTED
            order.rejection_reason = (
                "Polymarket residency gate: set CONFIRM_NON_US_POLYMARKET=true "
                "to confirm you are not a US resident"
            )
            router._log_order(order)
            logger.error(
                "Polymarket live trade BLOCKED: CONFIRM_NON_US_POLYMARKET env var not set. "
                "Polymarket is not available to US residents."
            )
            return OrderResult(success=False, order=order, error=order.rejection_reason)
        router._polymarket_residency_confirmed = True

    # Three-gate safety check (same gates for both platforms)
    if not router._live_gates_passed():
        order.status = OrderStatus.REJECTED
        order.rejection_reason = "Live trading gates not passed"
        router._log_order(order)
        return OrderResult(success=False, order=order, error="Live trading gates not passed")

    # Gate 3: Interactive confirmation with TTL (shared logic)
    gate3_result = await router._check_gate3(order)
    if gate3_result is not None:
        return gate3_result

    try:
        poly_side = order.side.value  # "BUY" or "SELL"
        poly_order_type = "GTC" if order.order_type == OrderType.GTC else "FOK"

        # Retry with backoff on transient API failures (network errors, 5xx)
        result = await retry_with_backoff(
            router.polymarket.create_and_post_order,
            token_id=order.token_id,
            side=poly_side,
            price=order.price,
            size=order.size,
            order_type=poly_order_type,
            max_retries=1,
            base_delay=1.0,
            on_retry=lambda attempt, err: logger.warning(
                f"Polymarket API call failed (attempt {attempt + 1}/2): {err} -- retrying"
            ),
        )

        now = datetime.now(timezone.utc)

        if result is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Polymarket API returned None"
            router._log_order(order)
            return OrderResult(success=False, order=order, error="Polymarket API returned None")

        # Check fill status
        status = result.get("status", "").lower()
        if status in ("matched", "filled"):
            order.status = OrderStatus.FILLED
            order.filled_at = now
            # C-2: Read actual fill price from API response, fall back to order price.
            try:
                raw_price = result.get("price") or result.get("avg_price")
                api_price = float(raw_price) if raw_price is not None else None
                order.fill_price = (api_price if api_price is not None and api_price > 0
                                    else order.price)
            except (TypeError, ValueError) as e:
                logger.warning(
                    "M-N1: Polymarket fill price parse failed for %s: %s -- "
                    "using order price $%.2f as fallback",
                    order.id, e, order.price,
                )
                order.fill_price = order.price
        elif status in ("live", "resting"):
            order.status = OrderStatus.OPEN
            router._log_order(order)
            logger.info(
                f"[POLY LIVE] Order resting: {order.side.value} {int(order.size)}x "
                f"{order.token_id} @ ${order.price:.2f}"
            )
            return OrderResult(success=True, order=order, trade=None)
        else:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = f"Unexpected status: {status}"
            router._log_order(order)
            return OrderResult(success=False, order=order, error=f"Unexpected status: {status}")

        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            platform=Platform.POLYMARKET,
            token_id=order.token_id,
            side=order.side,
            price=order.fill_price if order.fill_price is not None else order.price,
            size=order.size,
            fee=0.0,  # Event markets are fee-free
            realized_pnl=0.0,
            strategy=order.strategy,
            paper=False,
            timestamp=now,
        )

        router._log_order(order)
        router.db.log_trade(trade)

        fill_logged_price = order.fill_price if order.fill_price is not None else order.price
        logger.info(
            f"[POLY LIVE] Filled: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${fill_logged_price:.2f}"
        )

        return OrderResult(success=True, order=order, trade=trade)

    except Exception as e:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = str(e)
        router._log_order(order)
        logger.error(f"Polymarket live order failed: {e}", exc_info=True)
        return OrderResult(success=False, order=order, error=str(e))
