"""Order router — routes orders through paper or live execution.

Paper mode simulates fills at the order price.
Live mode submits to Kalshi or Polymarket API based on order platform.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, Platform, Side, Trade, dollars_to_cents,
    kalshi_maker_fee, kalshi_taker_fee, polymarket_fee, OrderType,
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

    def __init__(self, settings: Settings, kalshi: KalshiClient, db: Database, position_manager=None, polymarket=None):
        self.settings = settings
        self.kalshi = kalshi
        self.polymarket = polymarket  # Optional PolymarketClient
        self.db = db
        self.position_manager = position_manager
        self._session_confirmed = False  # Gate 3: first-trade confirmation
        self._log_gate_status()

    def _log_gate_status(self):
        """Log live trading gate status at startup for visibility."""
        mode = self.settings.trading.mode
        live_enabled = self.settings.live_enabled
        if mode == "live" and not live_enabled:
            logger.warning(
                "Live mode configured but POLYEDGE_LIVE_ENABLED env var is not set — "
                "live trades will be rejected until the env var is set to 'true'"
            )
        elif mode == "live" and live_enabled:
            logger.warning("LIVE TRADING ENABLED — all gates passed at startup")
        else:
            logger.info(f"Trading mode: {mode} (live gates not required)")

    async def route_order(self, order: Order) -> OrderResult:
        """Route an order based on current trading mode.

        Paper mode: simulates immediate fill at order price.
        Live mode: submits to Kalshi API.
        """
        # Validate sell orders don't exceed position size
        if order.side == Side.SELL and self.position_manager is not None:
            position = self.position_manager.get_position(order.market_id)
            if position is None:
                logger.warning(
                    f"Rejected SELL: no open position for {order.market_id}"
                )
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "No open position to sell"
                self._log_order(order)
                return OrderResult(success=False, order=order, error="No open position to sell")
            if order.size > position.size:
                logger.warning(
                    f"Clamping sell size from {order.size:.0f} to {position.size:.0f} "
                    f"for {order.market_id}"
                )
                order.size = position.size
                order.cost = order.price * order.size

        if order.paper or self.settings.trading.mode == "paper":
            return await self._paper_fill(order)
        elif order.platform == Platform.POLYMARKET:
            return await self._poly_live_fill(order)
        else:
            return await self._live_fill(order)

    def _simulate_slippage(self, order: Order) -> tuple[bool, float]:
        """Simulate realistic fill behavior for paper trading.

        Returns (filled, fill_price). ~15% of limit orders miss entirely.
        Fills include 0-1 cent adverse slippage.
        Uses deterministic hash for reproducibility.
        """
        import hashlib
        # Deterministic pseudo-random based on order details
        seed = hashlib.md5(
            f"{order.market_id}:{order.price}:{order.size}:{order.side.value}".encode()
        ).hexdigest()
        rand_val = int(seed[:8], 16) / 0xFFFFFFFF  # 0.0 to 1.0

        # 15% chance limit order doesn't fill
        if rand_val < 0.15:
            return False, order.price

        # Adverse slippage: 0-1 cent based on order characteristics
        slippage_rand = int(seed[8:16], 16) / 0xFFFFFFFF
        slippage = slippage_rand * 0.01  # 0 to 1 cent
        if order.side == Side.BUY:
            fill_price = min(0.99, order.price + slippage)
        else:
            fill_price = max(0.01, order.price - slippage)

        return True, round(fill_price, 2)

    async def _paper_fill(self, order: Order) -> OrderResult:
        """Simulate a fill in paper trading mode."""
        now = datetime.now(timezone.utc)

        # Simulate realistic fill with possible slippage/miss
        filled, fill_price = self._simulate_slippage(order)
        if not filled:
            order.status = OrderStatus.CANCELLED
            logger.info(
                f"[PAPER] Missed fill: {order.side.value} {int(order.size)}x "
                f"{order.token_id} @ ${order.price:.2f} (simulated no-fill)"
            )
            self._log_order(order)
            return OrderResult(success=False, order=order, error="Paper order missed fill")

        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = fill_price

        # Calculate fee (platform-aware)
        if order.platform == Platform.POLYMARKET:
            fee_dollars = 0.0  # Event markets are fee-free
        else:
            price_cents = dollars_to_cents(order.price)
            if order.order_type == OrderType.GTC:
                fee_cents = kalshi_maker_fee(int(order.size), price_cents)
            else:
                fee_cents = kalshi_taker_fee(int(order.size), price_cents)
            fee_dollars = fee_cents / 100.0

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

            # Create trade record for filled orders — use actual fill price
            fill_price = order.fill_price if order.fill_price is not None else order.price
            price_cents = dollars_to_cents(fill_price)
            if order.order_type == OrderType.GTC:
                fee_cents = kalshi_maker_fee(int(order.size), price_cents)
            else:
                fee_cents = kalshi_taker_fee(int(order.size), price_cents)

            trade = Trade(
                order_id=order.id,
                market_id=order.market_id,
                platform=order.platform,
                token_id=order.token_id,
                side=order.side,
                price=fill_price,
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

    async def _poly_live_fill(self, order: Order) -> OrderResult:
        """Submit order to Polymarket CLOB API for live execution."""
        if self.polymarket is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Polymarket client not configured"
            self._log_order(order)
            return OrderResult(success=False, order=order, error="Polymarket client not configured")

        # Three-gate safety check (same gates for both platforms)
        if not self._live_gates_passed():
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Live trading gates not passed"
            self._log_order(order)
            return OrderResult(success=False, order=order, error="Live trading gates not passed")

        if not self._session_confirmed:
            confirmed = await self._request_confirmation(order)
            if not confirmed:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "User declined live trade confirmation"
                self._log_order(order)
                return OrderResult(success=False, order=order, error="User declined")
            self._session_confirmed = True

        try:
            poly_side = order.side.value  # "BUY" or "SELL"
            poly_order_type = "GTC" if order.order_type == OrderType.GTC else "FOK"

            result = await self.polymarket.create_and_post_order(
                token_id=order.token_id,
                side=poly_side,
                price=order.price,
                size=order.size,
                order_type=poly_order_type,
            )

            now = datetime.now(timezone.utc)

            if result is None:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "Polymarket API returned None"
                self._log_order(order)
                return OrderResult(success=False, order=order, error="Polymarket API returned None")

            # Check fill status
            status = result.get("status", "").lower()
            if status in ("matched", "filled"):
                order.status = OrderStatus.FILLED
                order.filled_at = now
                order.fill_price = order.price
            elif status in ("live", "resting"):
                order.status = OrderStatus.OPEN
                self._log_order(order)
                logger.info(
                    f"[POLY LIVE] Order resting: {order.side.value} {int(order.size)}x "
                    f"{order.token_id[:16]}... @ ${order.price:.2f}"
                )
                return OrderResult(success=True, order=order, trade=None)
            else:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = f"Unexpected status: {status}"
                self._log_order(order)
                return OrderResult(success=False, order=order, error=f"Unexpected status: {status}")

            trade = Trade(
                order_id=order.id,
                market_id=order.market_id,
                platform=Platform.POLYMARKET,
                token_id=order.token_id,
                side=order.side,
                price=order.price,
                size=order.size,
                fee=0.0,  # Event markets are fee-free
                realized_pnl=0.0,
                strategy=order.strategy,
                paper=False,
                timestamp=now,
            )

            self._log_order(order)
            self.db.log_trade(trade)

            logger.info(
                f"[POLY LIVE] Filled: {order.side.value} {int(order.size)}x "
                f"{order.token_id[:16]}... @ ${order.price:.2f}"
            )

            return OrderResult(success=True, order=order, trade=trade)

        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = str(e)
            self._log_order(order)
            logger.error(f"Polymarket live order failed: {e}")
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
        platform = order.platform.value if hasattr(order.platform, 'value') else str(order.platform)
        conn = self.db._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO orders (
                id, market_id, platform, token_id, side, price, size, cost,
                order_type, fee_rate_bps, status, strategy, signal_id,
                paper, created_at, filled_at, fill_price, cancelled_at, rejection_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order.id,
            order.market_id,
            platform,
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
