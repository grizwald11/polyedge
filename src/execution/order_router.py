"""Order router — routes orders through paper or live execution.

Paper mode simulates fills at the order price.
Live mode submits to Kalshi or Polymarket API based on order platform.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from src.config import Settings
from src.core.kalshi_client import KalshiClient, KalshiRateLimitError
from src.core.models import (
    Order, OrderStatus, Platform, Side, Trade, dollars_to_cents,
    kalshi_maker_fee, kalshi_taker_fee, polymarket_fee, OrderType,
)
from src.storage.database import Database

if TYPE_CHECKING:
    from src.core.polymarket_client import PolymarketClient
    from src.execution.position_manager import PositionManager

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

    def __init__(self, settings: Settings, kalshi: KalshiClient, db: Database, position_manager: Optional[PositionManager] = None, polymarket: Optional[PolymarketClient] = None):
        self.settings = settings
        self.kalshi = kalshi
        self.polymarket = polymarket  # Optional PolymarketClient
        self.db = db
        self.position_manager = position_manager
        self._pending_order_cost: float = 0.0  # Total cost of unfilled pending orders
        self._pending_orders: dict[str, float] = {}  # order_id -> cost
        self._pending_lock = asyncio.Lock()  # Protect pending order state
        self._session_confirmed = False  # Gate 3: first-trade confirmation
        self._session_confirm_time: float | None = None  # When gate 3 was confirmed
        self._session_confirm_ttl = self.GATE3_CONFIRMATION_TTL_SECONDS
        self._polymarket_residency_confirmed = False  # Polymarket jurisdiction gate
        self._restore_pending_orders()
        self._log_gate_status()

    @property
    def pending_order_cost(self) -> float:
        """Total cost of unfilled pending orders."""
        return self._pending_order_cost

    def _restore_pending_orders(self) -> None:
        """Restore in-memory pending order state from DB on startup (H-1).

        Called synchronously from __init__ before the event loop is running.
        Rebuilds _pending_orders and _pending_order_cost from persisted DB rows
        so resting orders survive process restarts without losing cost accounting.
        """
        pending = self.db.load_pending_orders()
        if pending:
            self._pending_orders = pending
            self._pending_order_cost = round(sum(pending.values()), 4)
            logger.info(
                f"Restored {len(pending)} pending orders from DB "
                f"(total cost=${self._pending_order_cost:.2f})"
            )

    async def _add_pending(self, order_id: str, cost: float) -> None:
        """Track a new pending (resting) order's cost and persist to DB (H-1, H-3).

        Acquires _pending_lock to guard against concurrent fill callbacks.
        Persists to DB before updating in-memory state so crashes between
        the two operations never result in lost pending order records.
        """
        async with self._pending_lock:
            self.db.save_pending_order(order_id, cost)
            self._pending_orders[order_id] = cost
            self._pending_order_cost = round(sum(self._pending_orders.values()), 4)

    async def _remove_pending(self, order_id: str) -> None:
        """Remove a pending order (filled, cancelled, or expired) and delete from DB (H-1, H-3)."""
        async with self._pending_lock:
            self.db.delete_pending_order(order_id)
            self._pending_orders.pop(order_id, None)
            self._pending_order_cost = round(sum(self._pending_orders.values()), 4)

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
                # Recalculate cost including fees (not just price * size)
                price_cents = dollars_to_cents(order.price)
                if order.platform == Platform.POLYMARKET:
                    fee_dollars = 0.0
                elif order.order_type == OrderType.GTC:
                    fee_dollars = kalshi_maker_fee(int(order.size), price_cents) / 100.0
                else:
                    fee_dollars = kalshi_taker_fee(int(order.size), price_cents) / 100.0
                order.cost = round(order.price * order.size + fee_dollars, 4)

        if order.paper or self.settings.trading.mode == "paper":
            return await self._paper_fill(order)
        elif order.platform == Platform.POLYMARKET:
            return await self._poly_live_fill(order)
        else:
            return await self._live_fill(order)

    # Paper trading simulation constants
    PAPER_LIMIT_ORDER_MISS_RATE = 0.15  # 15% of limit orders don't fill
    PAPER_MAX_SLIPPAGE = 0.01           # 0-1 cent adverse slippage
    GATE3_CONFIRMATION_TTL_SECONDS = 3600  # Gate 3 expires after 1 hour

    def _simulate_slippage(self, order: Order) -> tuple[bool, float]:
        """Simulate realistic fill behavior for paper trading.

        Returns (filled, fill_price). ~15% of limit orders miss entirely.
        Fills include 0-1 cent adverse slippage.
        Uses non-deterministic randomness for realistic variance.
        """
        import random as _random
        # Use non-deterministic randomness for realistic paper trading variance.
        # Previously used deterministic PRNG seeded from order attributes,
        # but this biased paper trading results by producing identical
        # slippage for the same order parameters across restarts.
        rng = _random.Random()

        if rng.random() < self.PAPER_LIMIT_ORDER_MISS_RATE:
            return False, order.price

        # Adverse slippage: 0 to PAPER_MAX_SLIPPAGE
        slippage = rng.random() * self.PAPER_MAX_SLIPPAGE
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

    async def _check_gate3(self, order: Order) -> Optional[OrderResult]:
        """Check Gate 3 (interactive session confirmation) with TTL expiry.

        Returns None if gate passes, or an OrderResult rejection if gate fails.
        Shared by both Kalshi and Polymarket live fill paths.
        """
        import time as _time
        if self._session_confirmed and self._session_confirm_time is not None:
            if _time.time() - self._session_confirm_time > self._session_confirm_ttl:
                logger.info("Gate 3 confirmation expired — re-prompting")
                self._session_confirmed = False

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
            self._session_confirm_time = _time.time()

        return None  # Gate passed

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

        # Gate 3: Interactive confirmation with TTL
        gate3_result = await self._check_gate3(order)
        if gate3_result is not None:
            return gate3_result

        # Determine Kalshi side and order type.
        # kalshi_side is set explicitly by order_builder from the Direction enum,
        # avoiding fragile string matching on token_id.
        kalshi_side = order.kalshi_side
        if kalshi_side is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Missing kalshi_side — order_builder must set this explicitly"
            self._log_order(order)
            logger.error(
                f"Order {order.id} missing kalshi_side — rejected to prevent side mismatch. "
                f"Ensure order_builder sets kalshi_side from Direction enum."
            )
            return OrderResult(success=False, order=order, error="Missing kalshi_side")
        kalshi_type = "limit" if order.order_type == OrderType.GTC else "market"
        # Kalshi API always expects yes_price regardless of which side we buy.
        # For BUY_NO: order.price is the NO price, so yes_price = 1 - order.price.
        if kalshi_side == "no":
            yes_price = dollars_to_cents(1.0 - order.price)
        else:
            yes_price = dollars_to_cents(order.price)

        # C-3: Validate cents are in Kalshi's valid range after conversion.
        if not (1 <= yes_price <= 99):
            order.status = OrderStatus.REJECTED
            order.rejection_reason = f"Price converts to {yes_price} cents — outside Kalshi range [1, 99]"
            self._log_order(order)
            logger.error(
                f"Order {order.id} rejected: yes_price={yes_price} cents "
                f"(from order.price=${order.price:.4f}, side={kalshi_side})"
            )
            return OrderResult(success=False, order=order, error=order.rejection_reason)

        try:
            # Hard timeout on order creation to prevent hanging indefinitely.
            # If this times out, the order may have been placed on Kalshi —
            # we reconcile by checking open orders below.
            try:
                result = await asyncio.wait_for(
                    self.kalshi.create_order(
                        ticker=order.market_id,
                        side=kalshi_side,
                        yes_price=yes_price,
                        count=int(order.size),
                        order_type=kalshi_type,
                        action=order.side.value.lower(),
                    ),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"Order creation timed out for {order.market_id} — "
                    f"order may exist on Kalshi. Checking open orders for reconciliation."
                )
                # Reconcile: check if the order was actually placed
                result = await self._reconcile_after_timeout(order)
                if result is None:
                    order.status = OrderStatus.OPEN  # Assume order may exist — safer than REJECTED
                    order.rejection_reason = "Timeout creating order — may exist on Kalshi (unconfirmed)"
                    self._log_order(order)
                    logger.critical(
                        f"ORPHANED ORDER RISK: order for {order.market_id} timed out and reconciliation "
                        f"found no match. Order may still be processing on Kalshi. "
                        f"Manual review required — check Kalshi dashboard."
                    )
                    return OrderResult(success=False, order=order, error="Timeout — order status unknown, manual review required")

            if result is None:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "Kalshi API returned None"
                self._log_order(order)
                return OrderResult(
                    success=False, order=order, error="Kalshi API returned None"
                )

            # Validate order_id exists in response — without it we can't
            # track or poll the order, risking orphaned positions on Kalshi.
            kalshi_order_id = (result.get("order_id") or "").strip()
            if not kalshi_order_id:
                # Recovery attempt: fetch open orders and match by ticker + price + side
                logger.warning(
                    f"Kalshi order response missing order_id for {order.market_id} — "
                    f"attempting recovery via get_open_orders()"
                )
                try:
                    open_orders = await asyncio.wait_for(
                        self.kalshi.get_open_orders(),
                        timeout=10.0,
                    )
                    for oo in open_orders:
                        price_match = abs(oo.get("yes_price", 0) / 100 - order.price) < 0.01
                        side_match = oo.get("side", "").lower() == (order.kalshi_side or "").lower()
                        count_match = oo.get("count", 0) == int(order.size)
                        if (
                            oo.get("ticker") == order.market_id
                            and price_match
                            and side_match
                            and count_match
                        ):
                            recovered_id = (oo.get("order_id") or "").strip()
                            if recovered_id:
                                kalshi_order_id = recovered_id
                                logger.warning(
                                    f"Orphaned order recovery succeeded: matched order_id={kalshi_order_id} "
                                    f"for {order.market_id} via open orders list"
                                )
                                result = oo
                                break
                except Exception as rec_err:
                    logger.error(f"Orphaned order recovery failed: {rec_err}", exc_info=True)

                if not kalshi_order_id:
                    order.status = OrderStatus.REJECTED
                    order.rejection_reason = "Kalshi API did not return order_id"
                    self._log_order(order)
                    logger.error(
                        f"Kalshi order response missing order_id and recovery found no match: {result}"
                    )
                    return OrderResult(
                        success=False, order=order,
                        error="Kalshi API did not return order_id",
                    )

            # Validate that response contains expected fields
            if "status" not in result:
                logger.warning(
                    f"Kalshi order response missing 'status' field: {result}"
                )

            # Store exchange order ID for later cancel/lookup operations
            order.exchange_order_id = kalshi_order_id

            final_status = await self._poll_order_status(kalshi_order_id, result)

            # Capture timestamp once for consistency
            now = datetime.now(timezone.utc)

            if final_status in ("executed",):
                order.status = OrderStatus.FILLED
                order.filled_at = now
                # Use actual fill price from API if available; fall back to order price
                api_fill_price = result.get("avg_price")
                if api_fill_price is not None:
                    # Validate avg_price is numeric and in valid Kalshi range (1-99 cents)
                    try:
                        api_fill_price = float(api_fill_price)
                        if not (0 < api_fill_price <= 100):
                            raise ValueError(f"avg_price out of range: {api_fill_price}")
                        order.fill_price = api_fill_price / 100.0
                    except (TypeError, ValueError) as e:
                        logger.error(f"Invalid avg_price from Kalshi: {result.get('avg_price')!r} — using order price")
                        order.fill_price = order.price
                else:
                    order.fill_price = order.price
            elif final_status == "resting":
                order.status = OrderStatus.OPEN
                await self._add_pending(order.id, order.cost)
                self._log_order(order)
                logger.info(
                    f"[LIVE] Order resting: {order.side.value} {int(order.size)}x "
                    f"{order.token_id} @ ${order.price:.2f} "
                    f"(pending_cost=${self._pending_order_cost:.2f})"
                )
                return OrderResult(success=True, order=order, trade=None)
            elif final_status in ("canceled", "cancelled"):
                order.status = OrderStatus.CANCELLED
                order.cancelled_at = now
                await self._remove_pending(order.id)
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

            await self._remove_pending(order.id)
            self._log_order(order)
            self.db.log_trade(trade)

            # Post-fill slippage monitoring: warn if actual fill diverges from expected
            slippage = abs(fill_price - order.price)
            if slippage > 0.01:
                logger.warning(
                    f"[LIVE] Slippage alert: expected ${order.price:.2f}, "
                    f"filled ${fill_price:.2f} (slippage=${slippage:.3f})"
                )

            logger.info(
                f"[LIVE] Filled: {order.side.value} {int(order.size)}x "
                f"{order.token_id} @ ${fill_price:.2f}"
            )

            return OrderResult(success=True, order=order, trade=trade)

        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = str(e)
            self._log_order(order)
            logger.exception(f"Live order failed: {e}")
            return OrderResult(success=False, order=order, error=str(e))

    async def _poly_live_fill(self, order: Order) -> OrderResult:
        """Submit order to Polymarket CLOB API for live execution."""
        if self.polymarket is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Polymarket client not configured"
            self._log_order(order)
            return OrderResult(success=False, order=order, error="Polymarket client not configured")

        # Polymarket residency gate: require explicit non-US confirmation.
        # Polymarket is not legal for US persons — this gate prevents accidental
        # live trades without jurisdiction acknowledgment.
        # NOTE: Intentionally uses env var (not config file) as a safety gate —
        # env vars are harder to accidentally change and require explicit action.
        if not self._polymarket_residency_confirmed:
            import os
            if os.environ.get("CONFIRM_NON_US_POLYMARKET", "").lower() != "true":
                order.status = OrderStatus.REJECTED
                order.rejection_reason = (
                    "Polymarket residency gate: set CONFIRM_NON_US_POLYMARKET=true "
                    "to confirm you are not a US resident"
                )
                self._log_order(order)
                logger.error(
                    "Polymarket live trade BLOCKED: CONFIRM_NON_US_POLYMARKET env var not set. "
                    "Polymarket is not available to US residents."
                )
                return OrderResult(success=False, order=order, error=order.rejection_reason)
            self._polymarket_residency_confirmed = True

        # Three-gate safety check (same gates for both platforms)
        if not self._live_gates_passed():
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Live trading gates not passed"
            self._log_order(order)
            return OrderResult(success=False, order=order, error="Live trading gates not passed")

        # Gate 3: Interactive confirmation with TTL (shared logic)
        gate3_result = await self._check_gate3(order)
        if gate3_result is not None:
            return gate3_result

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
            logger.error(f"Polymarket live order failed: {e}", exc_info=True)
            return OrderResult(success=False, order=order, error=str(e))

    async def _reconcile_after_timeout(self, order: Order) -> Optional[dict]:
        """After a create_order timeout, check Kalshi for matching recent orders.

        Returns the matching order dict if found, None otherwise.
        """
        try:
            open_orders = await asyncio.wait_for(
                self.kalshi.get_open_orders(),
                timeout=10.0,
            )
            for oo in open_orders:
                price_match = abs(oo.get("yes_price", 0) / 100 - order.price) < 0.01
                if (
                    oo.get("ticker") == order.market_id
                    and oo.get("count") == int(order.size)
                    and price_match
                ):
                    logger.warning(
                        f"Reconciliation found matching order on Kalshi: {oo.get('order_id')}"
                    )
                    return oo
        except Exception as e:
            logger.error(f"Reconciliation check failed: {e}", exc_info=True)
        return None

    async def _poll_order_status(
        self, kalshi_order_id: str, initial_data: dict
    ) -> str:
        """Poll Kalshi for order fill status up to 5 times with 2s delays.

        Returns the final status string.
        """
        status = initial_data.get("status", "").lower()
        if status in ("executed", "canceled", "cancelled", "expired", "rejected", "failed"):
            return status

        if not kalshi_order_id:
            return status

        max_attempts = self.settings.execution.max_poll_attempts
        poll_delay = self.settings.execution.order_poll_delay_seconds
        poll_timeout = self.settings.execution.order_poll_timeout_seconds
        for attempt in range(max_attempts):
            await asyncio.sleep(poll_delay)
            try:
                order_data = await asyncio.wait_for(
                    self.kalshi.get_order(kalshi_order_id),
                    timeout=poll_timeout,
                )
                if order_data:
                    status = order_data.get("status", "").lower()
                    if status in ("executed", "canceled", "cancelled", "expired", "rejected", "failed"):
                        return status
            except asyncio.TimeoutError:
                logger.warning(f"Order poll attempt {attempt + 1} timed out after {poll_timeout}s")
            except Exception as e:
                logger.warning(f"Order poll attempt {attempt + 1} failed: {e}")

        return status  # Return last known status

    async def _request_confirmation(self, order: Order) -> bool:
        """Request interactive confirmation for the first live trade of the session.

        Uses asyncio.run_in_executor to avoid blocking the event loop.
        Times out after 60 seconds to prevent the trading loop from hanging
        indefinitely when running unattended (e.g., under pm2).
        """
        prompt = (
            f"\n{'='*60}\n"
            f"LIVE TRADE: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"(cost=${order.cost:.2f})\n"
            f"Market: {order.market_id}\n"
            f"Strategy: {order.strategy.value}\n"
            f"{'='*60}\n"
            f"Confirm first live trade of session? [y/N] (60s timeout): "
        )
        loop = asyncio.get_event_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, input, prompt),
                timeout=60,
            )
            return response.strip().lower() in ("y", "yes")
        except asyncio.TimeoutError:
            logger.critical(
                "Gate 3 confirmation timed out after 60s — live trade rejected. "
                "Bot may be running unattended without interactive confirmation."
            )
            return False
        except (EOFError, KeyboardInterrupt):
            return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting (open) live order.

        Args:
            order_id: The internal order ID (PE-xxx)

        Returns:
            True if successfully cancelled, False otherwise
        """
        # Look up the order in DB to get exchange order ID and current status
        conn = self.db._get_conn()
        row = conn.execute(
            "SELECT status, paper, exchange_order_id FROM orders WHERE id=?", (order_id,)
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
            await self._remove_pending(order_id)
            logger.info(f"[PAPER] Cancelled order {order_id}")
            return True

        # Use the exchange (Kalshi) order ID for the cancel API call.
        # Fall back to internal ID only if exchange_order_id was never stored.
        exchange_id = row["exchange_order_id"] if row["exchange_order_id"] else order_id
        if not row["exchange_order_id"]:
            logger.warning(
                f"No exchange_order_id for {order_id} — using internal ID "
                f"(cancel may fail if Kalshi doesn't recognize it)"
            )

        try:
            result = await self.kalshi.cancel_order(exchange_id)
            if result is not None:
                await self._remove_pending(order_id)
                conn = self.db._get_conn()
                conn.execute(
                    "UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), order_id),
                )
                conn.commit()
                logger.info(f"[LIVE] Cancelled order {order_id} (exchange_id={exchange_id})")
                return True
            else:
                logger.warning(f"Cancel returned None for {order_id} (exchange_id={exchange_id})")
                return False
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}", exc_info=True)
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
                paper, created_at, filled_at, fill_price, cancelled_at, rejection_reason,
                exchange_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            getattr(order, 'exchange_order_id', None),
        ))
        conn.commit()
