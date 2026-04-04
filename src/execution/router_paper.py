"""Paper trading router -- simulates order fills for paper trading mode.

Extracted from order_router.py (M-11) to isolate paper trading simulation
logic from live platform-specific routing.
"""

from __future__ import annotations

import logging
import random as _random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.core.models import (
    OrderStatus,
    OrderType,
    Platform,
    Side,
    Trade,
    dollars_to_cents,
    kalshi_maker_fee,
    kalshi_taker_fee,
)

if TYPE_CHECKING:
    from src.core.models import Order
    from src.execution.order_router import OrderResult, OrderRouter

logger = logging.getLogger(__name__)

# Paper trading simulation constants
PAPER_LIMIT_ORDER_MISS_RATE = 0.15  # 15% of limit orders don't fill
PAPER_MAX_SLIPPAGE = 0.01           # 0-1 cent adverse slippage


def simulate_slippage(order: Order) -> tuple[bool, float]:
    """Simulate realistic fill behavior for paper trading.

    Returns (filled, fill_price). ~15% of limit orders miss entirely.
    Fills include 0-1 cent adverse slippage.
    Uses non-deterministic randomness for realistic variance.
    """
    # Use non-deterministic randomness for realistic paper trading variance.
    rng = _random.Random()

    if rng.random() < PAPER_LIMIT_ORDER_MISS_RATE:
        return False, order.price

    # Adverse slippage: 0 to PAPER_MAX_SLIPPAGE
    slippage = rng.random() * PAPER_MAX_SLIPPAGE
    if order.side == Side.BUY:
        fill_price = order.price + slippage
    else:
        fill_price = order.price - slippage

    # L-3: Clamp fill price to valid range [0.01, 0.99]
    fill_price = max(0.01, min(0.99, fill_price))

    return True, round(fill_price, 2)


async def paper_fill(router: OrderRouter, order: Order) -> OrderResult:
    """Simulate a fill in paper trading mode.

    Args:
        router: The parent OrderRouter instance (for DB access and shared state).
        order: The order to simulate filling.
    """
    from src.execution.order_router import OrderResult

    # L-4: Apply Polymarket jurisdiction gate even in paper mode,
    # so paper trading results are realistic about which markets are tradeable.
    if order.platform == Platform.POLYMARKET and not router._polymarket_residency_confirmed:
        import os
        if os.environ.get("CONFIRM_NON_US_POLYMARKET", "").lower() != "true":
            order.status = OrderStatus.REJECTED
            order.rejection_reason = (
                "Polymarket residency gate: set CONFIRM_NON_US_POLYMARKET=true "
                "to confirm you are not a US resident (applies to paper trading too -- L-4)"
            )
            router._log_order(order)
            return OrderResult(success=False, order=order, error=order.rejection_reason)
        router._polymarket_residency_confirmed = True

    now = datetime.now(timezone.utc)

    # Simulate realistic fill with possible slippage/miss
    # Use router._simulate_slippage to allow test monkey-patching
    filled, fill_price = router._simulate_slippage(order)
    if not filled:
        order.status = OrderStatus.CANCELLED
        logger.info(
            f"[PAPER] Missed fill: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} (simulated no-fill)"
        )
        router._log_order(order)
        return OrderResult(success=False, order=order, error="Paper order missed fill")

    order.status = OrderStatus.FILLED
    order.filled_at = now
    order.fill_price = fill_price

    # Calculate fee using fill_price (not order.price) for accuracy
    # when slippage causes the fill to differ from the submitted price
    if order.platform == Platform.POLYMARKET:
        fee_dollars = 0.0  # Event markets are fee-free
    else:
        fill_price_cents = dollars_to_cents(fill_price)
        if order.order_type == OrderType.GTC:
            fee_cents = kalshi_maker_fee(int(order.size), fill_price_cents)
        else:
            fee_cents = kalshi_taker_fee(int(order.size), fill_price_cents)
        fee_dollars = round(fee_cents / 100.0, 4)

    # Create trade record (use fill_price for accurate P&L)
    trade = Trade(
        order_id=order.id,
        market_id=order.market_id,
        platform=order.platform,
        token_id=order.token_id,
        side=order.side,
        price=fill_price,
        size=order.size,
        fee=fee_dollars,
        realized_pnl=0.0,  # P&L calculated on position close
        strategy=order.strategy,
        paper=True,
        timestamp=now,
    )

    # Persist to database
    router._log_order(order)
    router.db.log_trade(trade)

    logger.info(
        f"[PAPER] Filled: {order.side.value} {int(order.size)}x "
        f"{order.token_id} @ ${order.price:.2f} "
        f"(cost=${order.cost:.2f}, fee=${fee_dollars:.2f})"
    )

    return OrderResult(success=True, order=order, trade=trade)
