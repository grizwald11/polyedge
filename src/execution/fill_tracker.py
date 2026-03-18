"""Fill tracker — monitors pending orders for fills, cancellations, and expirations.

Polls Kalshi API for order status updates and reconciles with local state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, Trade, Side, StrategyName,
    dollars_to_cents, kalshi_maker_fee, kalshi_taker_fee, OrderType,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)

MAX_POLLS = 5
POLL_STATES_TERMINAL = {"executed", "canceled", "pending"}


class FillTracker:
    """Tracks pending live orders and detects fills/cancellations."""

    def __init__(self, kalshi: KalshiClient, db: Database):
        self.kalshi = kalshi
        self.db = db
        self._pending_orders: dict[str, Order] = {}  # order_id -> Order

    def track(self, order: Order):
        """Register an order for fill tracking."""
        self._pending_orders[order.id] = order
        logger.debug(f"Tracking order {order.id} for fills")

    async def check_fills(self) -> list[Trade]:
        """Poll Kalshi for status of all pending orders.

        Returns list of newly detected trades (fills).
        """
        if not self._pending_orders:
            return []

        fills: list[Trade] = []
        resolved: list[str] = []

        for order_id, order in self._pending_orders.items():
            try:
                status = await self.kalshi.get_order(order_id)
                if status is None:
                    continue

                kalshi_status = status.get("status", "").lower()

                if kalshi_status == "executed":
                    trade = self._record_fill(order, status)
                    if trade:
                        fills.append(trade)
                    resolved.append(order_id)
                elif kalshi_status in ("canceled", "cancelled"):
                    self._record_cancellation(order)
                    resolved.append(order_id)
                # "resting" means still open — keep tracking

            except Exception as e:
                logger.error(f"Fill check failed for {order_id}: {e}")

        for oid in resolved:
            self._pending_orders.pop(oid, None)

        if fills:
            logger.info(f"Fill tracker: {len(fills)} new fills detected")

        return fills

    def _record_fill(self, order: Order, kalshi_data: dict) -> Optional[Trade]:
        """Record a detected fill."""
        now = datetime.now(timezone.utc)

        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = order.price

        # Calculate fee
        price_cents = dollars_to_cents(order.price)
        if order.order_type == OrderType.GTC:
            fee_cents = kalshi_maker_fee(int(order.size), price_cents)
        else:
            fee_cents = kalshi_taker_fee(int(order.size), price_cents)

        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            fee=fee_cents / 100.0,
            realized_pnl=0.0,
            strategy=order.strategy,
            paper=False,
            timestamp=now,
        )

        self._log_order(order)
        self.db.log_trade(trade)

        logger.info(
            f"[FILL] {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f}"
        )
        return trade

    def _record_cancellation(self, order: Order):
        """Record a detected cancellation."""
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(timezone.utc)
        self._log_order(order)
        logger.info(f"[CANCELLED] Order {order.id} on {order.market_id}")

    def _log_order(self, order: Order):
        """Persist order status to database."""
        conn = self.db._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO orders (
                    id, market_id, token_id, side, price, size, cost,
                    order_type, fee_rate_bps, status, strategy, signal_id,
                    paper, created_at, filled_at, fill_price, cancelled_at, rejection_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.id,
                order.market_id,
                order.token_id,
                order.side.value,
                order.price,
                order.size,
                order.cost,
                order.order_type.value,
                order.fee_rate_bps,
                order.status.value,
                order.strategy.value,
                order.signal_id,
                int(order.paper),
                order.created_at.isoformat(),
                order.filled_at.isoformat() if order.filled_at else None,
                order.fill_price,
                order.cancelled_at.isoformat() if order.cancelled_at else None,
                order.rejection_reason,
            ))
            conn.commit()
        finally:
            conn.close()

    @property
    def pending_count(self) -> int:
        """Number of orders being tracked."""
        return len(self._pending_orders)
