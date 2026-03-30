"""Fill tracker — monitors pending orders for fills, cancellations, and expirations.

Polls Kalshi or Polymarket API for order status updates and reconciles with local state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, Platform, Trade, Side, StrategyName,
    dollars_to_cents, kalshi_maker_fee, kalshi_taker_fee, polymarket_fee, OrderType,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)

MAX_POLLS = 5
POLL_STATES_TERMINAL = {"executed", "canceled", "cancelled"}


class FillTracker:
    """Tracks pending live orders and detects fills/cancellations.

    Supports two modes:
    - REST polling: check_fills() polls Kalshi API for order status
    - WebSocket: handle_ws_fill() processes real-time fill notifications
    """

    def __init__(self, kalshi: KalshiClient, db: Database, poll_timeout: int = 10, polymarket=None):
        self.kalshi = kalshi
        self.polymarket = polymarket  # Optional PolymarketClient
        self.db = db
        self._poll_timeout = poll_timeout
        self._pending_orders: dict[str, Order] = {}  # order_id -> Order
        self._ws_fills: list[Trade] = []  # Fills received via WebSocket
        # Load previously filled order IDs from database to prevent duplicate
        # recording after restart. Without this, a restart + re-detection of
        # old fills would double-count trades and corrupt position tracking.
        self._processed_fills: set[str] = self._load_filled_order_ids()
        # Track cumulative recorded fill count per order for partial fills.
        # This allows recording the delta when subsequent partials or the
        # final execution arrive. Load from DB on restart to prevent
        # duplicate trade records after crash during partial fill.
        self._partial_recorded: dict[str, int] = self._load_partial_recorded_counts()

    def track(self, order: Order):
        """Register an order for fill tracking."""
        self._pending_orders[order.id] = order
        logger.debug(f"Tracking order {order.id} for fills")

    async def check_fills(self) -> list[Trade]:
        """Poll Kalshi for status of all pending orders concurrently.

        Returns list of newly detected trades (fills).
        """
        if not self._pending_orders:
            return []

        # Snapshot to avoid RuntimeError if handle_ws_fill() pops an entry
        # during an await inside this loop.
        order_snapshot = list(self._pending_orders.items())

        # Poll all orders concurrently instead of sequentially
        async def _poll_one(order_id: str, order: Order):
            try:
                platform = getattr(order, "platform", Platform.KALSHI)
                if platform == Platform.POLYMARKET and self.polymarket is not None:
                    status = await asyncio.wait_for(
                        self.polymarket.get_order(order_id), timeout=self._poll_timeout
                    )
                else:
                    status = await asyncio.wait_for(
                        self.kalshi.get_order(order_id), timeout=self._poll_timeout
                    )
                return (order_id, order, status)
            except asyncio.TimeoutError:
                logger.warning(f"Order poll timed out for {order_id}")
                return (order_id, order, None)
            except Exception as e:
                logger.error(f"Fill check failed for {order_id}: {e}", exc_info=True)
                return (order_id, order, None)

        # Wrap all concurrent polls with a cumulative timeout (5 minutes max).
        # Without this, a hung API connection could block the fill-check loop
        # indefinitely, causing missed fills for all other tracked orders.
        try:
            poll_results = await asyncio.wait_for(
                asyncio.gather(
                    *[_poll_one(oid, o) for oid, o in order_snapshot],
                    return_exceptions=True,
                ),
                timeout=300,  # 5-minute cumulative timeout for all polls
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Fill check cumulative timeout (300s) exceeded for "
                f"{len(order_snapshot)} orders — returning any fills found so far"
            )
            return []

        fills: list[Trade] = []
        resolved: list[str] = []

        for result in poll_results:
            if isinstance(result, Exception):
                logger.error(f"Unexpected poll error: {result}")
                continue

            order_id, order, status = result
            if status is None:
                continue

            kalshi_status = status.get("status", "").lower()

            if kalshi_status == "executed":
                trade = self._record_fill(order, status)
                if trade:
                    fills.append(trade)
                resolved.append(order_id)
            elif kalshi_status == "partial":
                filled_count = status.get("filled_count", 0)
                remaining = status.get("remaining_count", 0)
                if filled_count > 0 and order.id not in self._processed_fills:
                    partial_trade = self._record_partial_fill(order, status)
                    if partial_trade:
                        fills.append(partial_trade)
                if remaining == 0:
                    resolved.append(order_id)
            elif kalshi_status in ("canceled", "cancelled"):
                self._record_cancellation(order)
                resolved.append(order_id)
            # "resting" means still open — keep tracking

        for oid in resolved:
            self._pending_orders.pop(oid, None)

        if fills:
            logger.info(f"Fill tracker: {len(fills)} new fills detected")

        return fills

    async def handle_ws_fill(self, fill_update) -> Optional[Trade]:
        """Handle a fill notification from the WebSocket.

        Args:
            fill_update: FillUpdate from the WebSocket client

        Returns:
            Trade if the fill matches a tracked order, None otherwise
        """
        order_id = fill_update.order_id
        order = self._pending_orders.get(order_id)
        if order is None:
            logger.debug(f"WebSocket fill for untracked order {order_id}")
            return None

        # Build a minimal kalshi_data dict for _record_fill
        kalshi_data = {
            "order_id": order_id,
            "status": "executed",
        }
        trade = self._record_fill(order, kalshi_data)
        if trade:
            self._pending_orders.pop(order_id, None)
            self._ws_fills.append(trade)
            logger.info(f"WebSocket fill detected for {order_id}")
        return trade

    def drain_ws_fills(self) -> list[Trade]:
        """Return and clear any fills received via WebSocket since last drain."""
        fills = self._ws_fills[:]
        self._ws_fills.clear()
        return fills

    def _record_partial_fill(self, order: Order, kalshi_data: dict) -> Optional[Trade]:
        """Record a partial fill. Returns Trade for the NEW portion only.

        Tracks cumulative filled count per order so that subsequent partial
        fills (or the final full execution) only record the delta — contracts
        filled since the last recording.
        """
        if order.id in self._processed_fills:
            return None

        filled_count = kalshi_data.get("filled_count", 0)
        if filled_count <= 0:
            return None

        # Calculate delta: only record contracts not yet recorded.
        # Assumption: filled_count is monotonically increasing from the API.
        # If the API ever reports a lower count (fill correction), log a warning.
        already_recorded = self._partial_recorded.get(order.id, 0)
        delta = filled_count - already_recorded
        if delta < 0:
            logger.warning(
                f"Non-monotonic filled_count for {order.id}: "
                f"API={filled_count}, recorded={already_recorded} — using max defensively"
            )
            # Use max defensively: don't lose track of already-recorded fills
            self._partial_recorded[order.id] = max(already_recorded, filled_count)
            return None
        if delta == 0:
            return None

        now = datetime.now(timezone.utc)

        # Calculate fee on newly filled portion only (platform-aware)
        platform = getattr(order, "platform", Platform.KALSHI)
        if platform == Platform.POLYMARKET:
            fee_dollars = 0.0  # Polymarket event markets are fee-free
        else:
            price_cents = dollars_to_cents(order.price)
            if order.order_type == OrderType.GTC:
                fee_cents = kalshi_maker_fee(delta, price_cents)
            else:
                fee_cents = kalshi_taker_fee(delta, price_cents)
            fee_dollars = fee_cents / 100.0

        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            platform=platform,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=float(delta),
            fee=fee_dollars,
            realized_pnl=0.0,
            strategy=order.strategy,
            signal_id=order.signal_id,  # Propagate signal linkage from order to trade
            paper=False,
            timestamp=now,
        )

        # Update order status but do NOT mutate order.size — the original
        # size is needed for fee calculations and DB consistency. Track
        # remaining count via the API, not by mutating local state.
        remaining = kalshi_data.get("remaining_count", 0)
        order.status = OrderStatus.PARTIAL if remaining > 0 else OrderStatus.FILLED

        # Atomic ordering: DB write before memory update ensures crash recovery correctness.
        # Write to DB FIRST, then update in-memory tracker. If we crash
        # after DB write but before memory update, restart will re-read
        # from DB via _load_partial_recorded_counts() and be correct.
        # If we crash before DB write, the in-memory tracker won't have
        # advanced, so we'll correctly re-record on restart.
        self.db.log_trade(trade)
        self._log_order(order)
        self._partial_recorded[order.id] = filled_count

        logger.info(
            f"[PARTIAL FILL] {order.side.value} {delta}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"({remaining} remaining)"
        )
        return trade

    def _record_fill(self, order: Order, kalshi_data: dict) -> Optional[Trade]:
        """Record a detected fill. Returns None if already processed (dedup).

        If some contracts were already recorded via partial fills, only records
        the remaining delta to avoid double-counting.
        """
        if order.id in self._processed_fills:
            logger.debug(f"Fill already processed for {order.id} — skipping duplicate")
            return None
        self._processed_fills.add(order.id)
        # Clean up partial tracking now that order is fully resolved
        already_recorded = self._partial_recorded.pop(order.id, 0)
        now = datetime.now(timezone.utc)

        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = order.price

        # Only record the delta if some contracts were already logged as partial fills
        remaining_size = int(order.size) - already_recorded
        if remaining_size <= 0:
            # All contracts were already recorded via partial fills
            self._log_order(order)
            logger.debug(f"Full fill for {order.id} — all {int(order.size)} contracts already recorded via partials")
            return None

        # Calculate fee on remaining portion only (platform-aware)
        platform = getattr(order, "platform", Platform.KALSHI)
        if platform == Platform.POLYMARKET:
            fee_dollars = 0.0
        else:
            price_cents = dollars_to_cents(order.price)
            if order.order_type == OrderType.GTC:
                fee_cents = kalshi_maker_fee(remaining_size, price_cents)
            else:
                fee_cents = kalshi_taker_fee(remaining_size, price_cents)
            fee_dollars = fee_cents / 100.0

        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            platform=platform,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=float(remaining_size),
            fee=fee_dollars,
            realized_pnl=0.0,
            strategy=order.strategy,
            signal_id=order.signal_id,  # Propagate signal linkage from order to trade
            paper=False,
            timestamp=now,
        )

        self._log_order(order)
        self.db.log_trade(trade)

        logger.info(
            f"[FILL] {order.side.value} {remaining_size}x "
            f"{order.token_id} @ ${order.price:.2f}"
            f"{f' ({already_recorded} already recorded via partials)' if already_recorded else ''}"
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
        platform = getattr(order, "platform", Platform.KALSHI)
        conn.execute("""
            INSERT OR REPLACE INTO orders (
                id, market_id, platform, token_id, side, price, size, cost,
                order_type, fee_rate_bps, status, strategy, signal_id,
                paper, created_at, filled_at, fill_price, cancelled_at, rejection_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order.id,
            order.market_id,
            platform.value,
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

    def _load_filled_order_ids(self) -> set[str]:
        """Load order IDs that already have trades recorded in the database.

        This prevents duplicate trade recording after a restart.
        """
        try:
            conn = self.db._get_conn()
            rows = conn.execute(
                "SELECT DISTINCT order_id FROM trades"
            ).fetchall()
            ids = {row["order_id"] for row in rows if row["order_id"]}
            if ids:
                logger.info(f"Fill tracker: loaded {len(ids)} previously filled order IDs")
            return ids
        except Exception as e:
            logger.warning(f"Failed to load filled order IDs: {e}")
            return set()

    def _load_partial_recorded_counts(self) -> dict[str, int]:
        """Load cumulative fill counts per order from the database.

        On restart, this prevents re-recording partial fills that were already
        saved. Without this, a crash during partial fill processing would
        create duplicate trade records on the next poll.
        """
        try:
            conn = self.db._get_conn()
            rows = conn.execute(
                "SELECT order_id, CAST(SUM(size) AS INTEGER) as filled "
                "FROM trades WHERE order_id IS NOT NULL "
                "GROUP BY order_id"
            ).fetchall()
            counts = {
                row["order_id"]: row["filled"]
                for row in rows
                if row["order_id"] and row["order_id"] not in self._processed_fills
            }
            if counts:
                logger.info(f"Fill tracker: loaded {len(counts)} partial fill counts")
            return counts
        except Exception as e:
            logger.warning(f"Failed to load partial fill counts: {e}")
            return {}

    @property
    def pending_count(self) -> int:
        """Number of orders being tracked."""
        return len(self._pending_orders)
