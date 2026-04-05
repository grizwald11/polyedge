"""Order router -- routes orders through paper or live execution.

Paper mode simulates fills at the order price.
Live mode submits to Kalshi API.

Platform-specific logic is delegated to:
  - router_paper.py     (paper trading simulation)
  - router_kalshi.py    (Kalshi live execution)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from typing import TYPE_CHECKING, Optional

from src.config import Settings
from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    Trade,
    dollars_to_cents,
    kalshi_maker_fee,
    kalshi_taker_fee,
)
from src.storage.database import Database

if TYPE_CHECKING:
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
    """Routes orders to paper or live execution.

    Platform-specific fill logic is delegated to submodules:
      - router_paper.paper_fill() for paper trading
      - router_kalshi.live_fill() for Kalshi live execution
    """

    # Paper trading simulation constants (kept for backward compat with tests
    # that reference these as class attributes)
    PAPER_LIMIT_ORDER_MISS_RATE = 0.15  # 15% of limit orders don't fill
    PAPER_MAX_SLIPPAGE = 0.01           # 0-1 cent adverse slippage
    GATE3_CONFIRMATION_TTL_SECONDS = 3600  # Gate 3 expires after 1 hour

    def __init__(self, settings: Settings, kalshi: KalshiClient, db: Database, position_manager: Optional[PositionManager] = None):
        self.settings = settings
        self.kalshi = kalshi
        self.db = db
        self.position_manager = position_manager
        self._pending_order_cost: float = 0.0  # Total cost of unfilled pending orders
        self._pending_orders: dict[str, float] = {}  # order_id -> cost
        self._pending_lock = asyncio.Lock()  # Protect pending order state
        self._session_confirmed = False  # Gate 3: first-trade confirmation
        self._session_confirm_time: float | None = None  # When gate 3 was confirmed
        self._session_confirm_ttl = self.GATE3_CONFIRMATION_TTL_SECONDS
        self.metrics = None  # Optional: set by orchestrator for fill rate tracking
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

    async def _cleanup_stale_pending_orders(self) -> int:
        """M-5: Remove pending orders older than 24 hours to prevent unbounded memory growth.

        Returns the number of stale orders removed.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        removed = 0
        # M-N3: Acquire lock before querying DB to prevent race with concurrent fill updates
        async with self._pending_lock:
            conn = self.db._get_conn()
            rows = conn.execute(
                "SELECT id FROM orders WHERE status='open' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            stale_ids = {row["id"] for row in rows}
            for order_id in list(self._pending_orders.keys()):
                if order_id in stale_ids:
                    self.db.delete_pending_order(order_id)
                    del self._pending_orders[order_id]
                    removed += 1

            if removed:
                self._pending_order_cost = round(sum(self._pending_orders.values()), 4)
                logger.info(
                    f"Cleaned up {removed} stale pending orders (>24h old), "
                    f"remaining pending cost=${self._pending_order_cost:.2f}"
                )
        return removed

    def _log_gate_status(self):
        """Log live trading gate status at startup for visibility."""
        mode = self.settings.trading.mode
        live_enabled = self.settings.live_enabled
        if mode == "live" and not live_enabled:
            logger.warning(
                "Live mode configured but POLYEDGE_LIVE_ENABLED env var is not set -- "
                "live trades will be rejected until the env var is set to 'true'"
            )
        elif mode == "live" and live_enabled:
            logger.warning("LIVE TRADING ENABLED -- all gates passed at startup")
        else:
            logger.info(f"Trading mode: {mode} (live gates not required)")

    async def route_order(self, order: Order) -> OrderResult:
        """Route an order based on current trading mode.

        Paper mode: simulates immediate fill at order price.
        Live mode: submits to Kalshi or Polymarket API.
        """
        # M-5: Clean up stale pending orders at the start of each routing cycle
        await self._cleanup_stale_pending_orders()

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
                if order.order_type == OrderType.GTC:
                    fee_dollars = kalshi_maker_fee(int(order.size), price_cents) / 100.0
                else:
                    fee_dollars = kalshi_taker_fee(int(order.size), price_cents) / 100.0
                # M-1 FIX: Use Decimal arithmetic to avoid float rounding errors
                from decimal import Decimal, ROUND_HALF_UP
                order.cost = float(
                    (Decimal(str(order.price)) * Decimal(str(order.size))
                     + Decimal(str(fee_dollars)))
                    .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                )

        if self.metrics is not None:
            self.metrics.record_order_submitted()

        # H-12: Reserve pending cost BEFORE submission to prevent race between
        # concurrent route_order calls that could over-commit capital.
        is_live = not (order.paper or self.settings.trading.mode == "paper")
        if is_live:
            await self._add_pending(order.id, order.cost)

        try:
            if order.paper or self.settings.trading.mode == "paper":
                result = await self._paper_fill(order)
            else:
                result = await self._live_fill(order)
        except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError) as exc:
            # H-12: Release reserved cost if submission itself raises
            if is_live:
                await self._remove_pending(order.id)
            raise
        except Exception as exc:
            # Catch-all for unexpected errors; still release reserved cost
            logger.error(f"Unexpected error routing order {order.id}: {exc}", exc_info=True)
            if is_live:
                await self._remove_pending(order.id)
            raise

        # H-12: Release reserved cost if order was not successfully placed
        # (rejected, failed, etc.). Resting orders keep the reservation.
        if is_live and not result.success:
            await self._remove_pending(order.id)

        if self.metrics is not None:
            if result.success:
                self.metrics.record_order_filled()
            else:
                self.metrics.record_order_rejected()

        return result

    # ------------------------------------------------------------------ #
    # Delegation to platform-specific routers                             #
    # ------------------------------------------------------------------ #

    def _simulate_slippage(self, order: Order) -> tuple[bool, float]:
        """Simulate realistic fill behavior for paper trading.

        Delegates to router_paper module.
        """
        from src.execution.router_paper import simulate_slippage
        return simulate_slippage(order)

    async def _paper_fill(self, order: Order) -> OrderResult:
        """Simulate a fill in paper trading mode."""
        from src.execution.router_paper import paper_fill
        return await paper_fill(self, order)

    async def _live_fill(self, order: Order) -> OrderResult:
        """Submit order to Kalshi API for live execution."""
        from src.execution.router_kalshi import live_fill
        return await live_fill(self, order)

    # ------------------------------------------------------------------ #
    # Gate checking (shared across platforms)                              #
    # ------------------------------------------------------------------ #

    async def _check_gate3(self, order: Order) -> Optional[OrderResult]:
        """Check Gate 3 (interactive session confirmation) with TTL expiry.

        Returns None if gate passes, or an OrderResult rejection if gate fails.
        Shared by both Kalshi and Polymarket live fill paths.
        """
        import time as _time
        if self._session_confirmed and self._session_confirm_time is not None:
            if _time.time() - self._session_confirm_time > self._session_confirm_ttl:
                logger.info("Gate 3 confirmation expired -- re-prompting")
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
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, input, prompt),
                timeout=60,
            )
            return response.strip().lower() in ("y", "yes")
        except asyncio.TimeoutError:
            logger.critical(
                "Gate 3 confirmation timed out after 60s -- live trade rejected. "
                "Bot may be running unattended without interactive confirmation."
            )
            return False
        except (EOFError, KeyboardInterrupt):
            return False

    # ------------------------------------------------------------------ #
    # Order cancellation                                                   #
    # ------------------------------------------------------------------ #

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
                f"No exchange_order_id for {order_id} -- using internal ID "
                f"(cancel may fail if Kalshi doesn't recognize it)"
            )

        try:
            result = await self.kalshi.cancel_order(exchange_id)
            if result is not None:
                await self._remove_pending(order_id)
                try:
                    conn = self.db._get_conn()
                    conn.execute(
                        "UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), order_id),
                    )
                    conn.commit()
                except (sqlite3.Error, OSError) as db_err:
                    logger.error(
                        f"Cancel succeeded on exchange but DB update failed -- "
                        f"will reconcile on next sync: {db_err}",
                        exc_info=True,
                    )
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

    async def periodic_cleanup(self) -> int:
        """H-5: Clean up stale pending orders. Call every ~1 hour from orchestrator.

        Returns number of stale orders cleaned up.
        """
        return await self.cancel_stale_orders(max_age_seconds=3600)

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
        # Gate 3 is interactive confirmation -- handled externally
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
