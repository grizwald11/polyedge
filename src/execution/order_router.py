"""Order router — routes orders through paper or live execution.

Paper mode simulates fills at the order price.
Live mode submits to Kalshi API via KalshiClient.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, Trade, dollars_to_cents,
    kalshi_maker_fee, kalshi_taker_fee, OrderType,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)


class OrderResult:
    """Result of order routing."""

    def __init__(
        self,
        success: bool,
        order: Order,
        trade: Optional[Trade] = None,
        error: str = "",
    ):
        self.success = success
        self.order = order
        self.trade = trade
        self.error = error


class OrderRouter:
    """Routes orders to paper or live execution."""

    def __init__(self, settings: Settings, kalshi: KalshiClient, db: Database):
        self.settings = settings
        self.kalshi = kalshi
        self.db = db
        self._session_confirmed = False  # Gate 3: first-trade confirmation

    async def route_order(self, order: Order) -> OrderResult:
        """Route an order based on current trading mode.

        Paper mode: simulates immediate fill at order price.
        Live mode: submits to Kalshi API.
        """
        if order.paper or self.settings.trading.mode == "paper":
            return await self._paper_fill(order)
        else:
            return await self._live_fill(order)

    async def _paper_fill(self, order: Order) -> OrderResult:
        """Simulate a fill in paper trading mode."""
        now = datetime.now(timezone.utc)

        # Simulate fill at order price
        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = order.price

        # Calculate fee
        price_cents = dollars_to_cents(order.price)
        if order.order_type == OrderType.GTC:
            fee_cents = kalshi_maker_fee(int(order.size), price_cents)
        else:
            fee_cents = kalshi_taker_fee(int(order.size), price_cents)
        fee_dollars = fee_cents / 100.0

        # Create trade record
        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            fee=fee_dollars,
            realized_pnl=0.0,  # P&L calculated on position close
            strategy=order.strategy,
            paper=True,
            timestamp=now,
        )

        # Persist to database
        self._log_order(order)
        self.db.log_trade(trade)

        logger.info(
            f"[PAPER] Filled: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"(cost=${order.cost:.2f}, fee=${fee_dollars:.2f})"
        )

        return OrderResult(success=True, order=order, trade=trade)

    async def _live_fill(self, order: Order) -> OrderResult:
        """Submit order to Kalshi API for live execution."""
        # Three-gate safety check
        if not self._live_gates_passed():
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Live trading gates not passed"
            self._log_order(order)
            return OrderResult(
                success=False, order=order,
                error="Live trading gates not passed"
            )

        # Gate 3: Interactive confirmation on first live trade per session
        if not self._session_confirmed:
            confirmed = await self._request_confirmation(order)
            if not confirmed:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "User declined live trade confirmation"
                self._log_order(order)
                return OrderResult(
                    success=False, order=order,
                    error="User declined live trade confirmation"
                )
            self._session_confirmed = True

        # Determine Kalshi side and order type
        kalshi_side = "yes" if "yes" in order.token_id.lower() else "no"
        kalshi_type = "limit" if order.order_type == OrderType.GTC else "market"
        # Kalshi API always expects yes_price regardless of which side we buy.
        # For BUY_NO: order.price is the NO price, so yes_price = 1 - order.price.
        if kalshi_side == "no":
            yes_price = dollars_to_cents(1.0 - order.price)
        else:
            yes_price = dollars_to_cents(order.price)

        try:
            result = await self.kalshi.create_order(
                ticker=order.market_id,
                side=kalshi_side,
                yes_price=yes_price,
                count=int(order.size),
                order_type=kalshi_type,
                action=order.side.value.lower(),
            )

            if result is None:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "Kalshi API returned None"
                self._log_order(order)
                return OrderResult(
                    success=False, order=order, error="Kalshi API returned None"
                )

            # Poll for fill status (limit orders may rest)
            kalshi_order_id = result.get("order_id", "")
            final_status = await self._poll_order_status(kalshi_order_id, result)

            # Capture timestamp once for consistency
            now = datetime.now(timezone.utc)

            if final_status in ("executed", "filled"):
                order.status = OrderStatus.FILLED
                order.filled_at = now
                # Use actual fill price from API if available; fall back to order price
                api_fill_price = result.get("avg_price")
                if api_fill_price is not None:
                    order.fill_price = api_fill_price / 100.0  # cents to dollars
                else:
                    order.fill_price = order.price
            elif final_status == "resting":
                order.status = OrderStatus.OPEN
                self._log_order(order)
                logger.info(
                    f"[LIVE] Order resting: {order.side.value} {int(order.size)}x "
                    f"{order.token_id} @ ${order.price:.2f}"
                )
                return OrderResult(success=True, order=order, trade=None)
            elif final_status in ("canceled", "cancelled"):
                order.status = OrderStatus.CANCELLED
                order.cancelled_at = now
                self._log_order(order)
                return OrderResult(
                    success=False, order=order, error="Order was cancelled"
                )

            # Create trade record for filled orders
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
                f"[LIVE] Filled: {order.side.value} {int(order.size)}x "
                f"{order.token_id} @ ${order.price:.2f}"
            )

            return OrderResult(success=True, order=order, trade=trade)

        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = str(e)
            self._log_order(order)
            logger.error(f"Live order failed: {e}")
            return OrderResult(success=False, order=order, error=str(e))

    async def _poll_order_status(
        self, kalshi_order_id: str, initial_data: dict
    ) -> str:
        """Poll Kalshi for order fill status up to 5 times with 2s delays.

        Returns the final status string.
        """
        status = initial_data.get("status", "").lower()
        if status in ("executed", "filled", "canceled", "cancelled"):
            return status

        if not kalshi_order_id:
            return status

        max_attempts = self.settings.execution.max_poll_attempts
        poll_delay = self.settings.execution.order_poll_delay_seconds
        for attempt in range(max_attempts):
            await asyncio.sleep(poll_delay)
            try:
                order_data = await self.kalshi.get_order(kalshi_order_id)
                if order_data:
                    status = order_data.get("status", "").lower()
                    if status in ("executed", "filled", "canceled", "cancelled"):
                        return status
            except Exception as e:
                logger.warning(f"Order poll attempt {attempt + 1} failed: {e}")

        return status  # Return last known status

    async def _request_confirmation(self, order: Order) -> bool:
        """Request interactive confirmation for the first live trade of the session.

        Uses asyncio.run_in_executor to avoid blocking the event loop.
        """
        prompt = (
            f"\n{'='*60}\n"
            f"LIVE TRADE: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"(cost=${order.cost:.2f})\n"
            f"Market: {order.market_id}\n"
            f"Strategy: {order.strategy.value}\n"
            f"{'='*60}\n"
            f"Confirm first live trade of session? [y/N]: "
        )
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(None, input, prompt)
            return response.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting (open) live order.

        Args:
            order_id: The internal order ID (PE-xxx)

        Returns:
            True if successfully cancelled, False otherwise
        """
        # Look up the order in DB to get Kalshi order ID and current status
        conn = self.db._get_conn()
        row = conn.execute(
            "SELECT status, paper FROM orders WHERE id=?", (order_id,)
        ).fetchone()

        if row is None:
            logger.warning(f"Cancel failed: order {order_id} not found")
            return False

        if row["status"] != "open":
            logger.warning(
                f"Cancel failed: order {order_id} is {row['status']}, not open"
            )
            return False

        if row["paper"]:
            # Paper orders can be "cancelled" by just updating status
            conn = self.db._get_conn()
            conn.execute(
                "UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), order_id),
            )
            conn.commit()
            logger.info(f"[PAPER] Cancelled order {order_id}")
            return True

        try:
            result = await self.kalshi.cancel_order(order_id)
            if result is not None:
                conn = self.db._get_conn()
                conn.execute(
                    "UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), order_id),
                )
                conn.commit()
                logger.info(f"[LIVE] Cancelled order {order_id}")
                return True
            else:
                logger.warning(f"Cancel returned None for {order_id}")
                return False
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    async def cancel_stale_orders(self, max_age_seconds: int = 1800) -> int:
        """Cancel open orders older than max_age_seconds.

        Args:
            max_age_seconds: Max time an order can rest before auto-cancel (default 30 min)

        Returns:
            Number of orders cancelled
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT id FROM orders WHERE status='open' AND created_at < ?",
            (cutoff,),
        ).fetchall()

        cancelled = 0
        for row in rows:
            if await self.cancel_order(row["id"]):
                cancelled += 1
        return cancelled

    async def cancel_all_open(self) -> int:
        """Cancel all open (resting) orders.

        Returns:
            Number of orders successfully cancelled
        """
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT id FROM orders WHERE status='open'"
        ).fetchall()

        cancelled = 0
        for row in rows:
            if await self.cancel_order(row["id"]):
                cancelled += 1
        if cancelled:
            logger.info(f"Cancelled {cancelled} open orders")
        return cancelled

    def _live_gates_passed(self) -> bool:
        """Three-gate safety system for live trading."""
        # Gate 1: Config mode must be "live"
        if self.settings.trading.mode != "live":
            logger.warning("Gate 1 failed: trading.mode is not 'live'")
            return False
        # Gate 2: Environment variable
        if not self.settings.live_enabled:
            logger.warning("Gate 2 failed: POLYEDGE_LIVE_ENABLED is not true")
            return False
        # Gate 3 is interactive confirmation — handled externally
        return True

    def _log_order(self, order: Order):
        """Persist order to database."""
        conn = self.db._get_conn()
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
