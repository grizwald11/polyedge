"""Tests for fill tracker."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, OrderType, Side, StrategyName,
)
from src.execution.fill_tracker import FillTracker


def _make_order() -> Order:
    return Order(
        id="PE-fill-test",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-RATE-CUT-MAY26_yes",
        side=Side.BUY,
        price=0.34,
        size=10,
        cost=3.40,
        order_type=OrderType.GTC,
        status=OrderStatus.OPEN,
        strategy=StrategyName.AI_PROBABILITY,
        paper=False,
    )


@pytest.fixture
def mock_kalshi():
    client = AsyncMock(spec=KalshiClient)
    return client


class TestFillTracker:
    @pytest.mark.asyncio
    async def test_no_pending_returns_empty(self, mock_kalshi, tmp_db):
        tracker = FillTracker(mock_kalshi, tmp_db)
        fills = await tracker.check_fills()
        assert fills == []

    @pytest.mark.asyncio
    async def test_detects_fill(self, mock_kalshi, tmp_db):
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker.track(order)

        fills = await tracker.check_fills()

        assert len(fills) == 1
        assert fills[0].market_id == "FED-RATE-CUT-MAY26"
        assert fills[0].price == 0.34
        assert fills[0].size == 10
        assert fills[0].paper is False
        assert tracker.pending_count == 0

    @pytest.mark.asyncio
    async def test_detects_cancellation(self, mock_kalshi, tmp_db):
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "canceled",
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker.track(order)

        fills = await tracker.check_fills()

        assert len(fills) == 0
        assert tracker.pending_count == 0

    @pytest.mark.asyncio
    async def test_resting_order_stays_tracked(self, mock_kalshi, tmp_db):
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "resting",
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        fills = await tracker.check_fills()

        assert len(fills) == 0
        assert tracker.pending_count == 1

    @pytest.mark.asyncio
    async def test_api_error_keeps_tracking(self, mock_kalshi, tmp_db):
        mock_kalshi.get_order = AsyncMock(side_effect=Exception("API error"))
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        fills = await tracker.check_fills()

        assert len(fills) == 0
        assert tracker.pending_count == 1  # Still tracked

    @pytest.mark.asyncio
    async def test_fill_logged_to_db(self, mock_kalshi, tmp_db):
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        await tracker.check_fills()

        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "PE-fill-test"

    @pytest.mark.asyncio
    async def test_multiple_orders_tracked(self, mock_kalshi, tmp_db):
        call_count = 0

        async def mock_get_order(order_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"order_id": order_id, "status": "executed"}
            return {"order_id": order_id, "status": "resting"}

        mock_kalshi.get_order = mock_get_order
        tracker = FillTracker(mock_kalshi, tmp_db)

        order1 = _make_order()
        order2 = Order(
            id="PE-fill-test2",
            market_id="MKT-B",
            token_id="MKT-B_yes",
            side=Side.BUY,
            price=0.50,
            size=5,
            cost=2.50,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tracker.track(order1)
        tracker.track(order2)

        fills = await tracker.check_fills()

        assert len(fills) == 1
        assert tracker.pending_count == 1

    @pytest.mark.asyncio
    async def test_fills_loaded_from_db_on_init(self, mock_kalshi, tmp_db):
        """Regression: previously filled order IDs should be loaded from DB on init,
        preventing duplicate trade recording after restart."""
        from src.core.models import Trade, StrategyName

        # Simulate a pre-existing trade in the DB
        trade = Trade(
            order_id="PE-already-filled",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=10,
            fee=0.01,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tmp_db.log_trade(trade)

        # Create a new tracker (simulates restart)
        tracker = FillTracker(mock_kalshi, tmp_db)
        assert "PE-already-filled" in tracker._processed_fills

        # Now if the same order is detected as filled, it should be skipped
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        order = Order(
            id="PE-already-filled",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=10,
            cost=3.40,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tracker.track(order)
        fills = await tracker.check_fills()
        assert len(fills) == 0  # Should be deduplicated

    @pytest.mark.asyncio
    async def test_partial_fill_recorded(self, mock_kalshi, tmp_db):
        """Partial fills should record trade for filled portion and keep tracking."""
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "partial",
            "filled_count": 6,
            "remaining_count": 4,
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()  # size=10
        tracker.track(order)

        fills = await tracker.check_fills()

        assert len(fills) == 1
        assert fills[0].size == 6  # Only the filled portion
        assert tracker.pending_count == 1  # Still tracking remainder

    @pytest.mark.asyncio
    async def test_partial_fill_fully_done_resolves(self, mock_kalshi, tmp_db):
        """Partial fill with remaining_count=0 should resolve the order."""
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "partial",
            "filled_count": 10,
            "remaining_count": 0,
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        fills = await tracker.check_fills()

        assert len(fills) == 1
        assert tracker.pending_count == 0  # Resolved since remaining=0

    @pytest.mark.asyncio
    async def test_ws_fill_during_poll_no_crash(self, mock_kalshi, tmp_db):
        """handle_ws_fill() popping an order during check_fills() iteration must not crash."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order1 = _make_order()
        order2 = Order(
            id="PE-fill-ws",
            market_id="MKT-B",
            token_id="MKT-B_yes",
            side=Side.BUY,
            price=0.50,
            size=5,
            cost=2.50,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tracker.track(order1)
        tracker.track(order2)

        # Simulate a WS fill arriving for order2 during the API poll for order1.
        # In asyncio this happens at the await in check_fills.
        ws_fill_processed = False

        async def mock_get_order(order_id):
            nonlocal ws_fill_processed
            if not ws_fill_processed:
                ws_fill_processed = True
                # Simulate WS fill popping order2 while we're mid-iteration
                fill_update = type("FillUpdate", (), {"order_id": "PE-fill-ws"})()
                await tracker.handle_ws_fill(fill_update)
            return {"order_id": order_id, "status": "resting"}

        mock_kalshi.get_order = mock_get_order

        # Should not raise RuntimeError: dictionary changed size during iteration
        fills = await tracker.check_fills()
        assert isinstance(fills, list)

    @pytest.mark.asyncio
    async def test_duplicate_fill_deduplicated(self, mock_kalshi, tmp_db):
        """If the same order fill is detected twice (e.g. REST + WebSocket),
        it should only be recorded once."""
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker.track(order)

        # First fill via REST polling
        fills1 = await tracker.check_fills()
        assert len(fills1) == 1

        # Re-track the same order (simulate re-add after reconnect)
        tracker._pending_orders[order.id] = order

        # Second poll — should be deduplicated
        fills2 = await tracker.check_fills()
        assert len(fills2) == 0

    @pytest.mark.asyncio
    async def test_partial_then_full_fill_records_both(self, mock_kalshi, tmp_db):
        """Regression: partial fill followed by full execution must record all contracts.

        Previously, _record_partial_fill added order.id to _processed_fills,
        causing _record_fill to skip the remaining contracts when the order
        transitioned from 'partial' to 'executed'.
        """
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()  # size=10
        tracker.track(order)

        # First poll: partial fill — 6 of 10 contracts
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "partial",
            "filled_count": 6,
            "remaining_count": 4,
        })
        fills1 = await tracker.check_fills()
        assert len(fills1) == 1
        assert fills1[0].size == 6

        # Second poll: fully executed — all 10 contracts filled
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        fills2 = await tracker.check_fills()
        assert len(fills2) == 1
        assert fills2[0].size == 4  # Only the remaining 4

        # Total recorded = 6 + 4 = 10 (full order)
        assert fills1[0].size + fills2[0].size == order.size

    @pytest.mark.asyncio
    async def test_multiple_partial_fills_record_deltas(self, mock_kalshi, tmp_db):
        """Multiple partial fills should each record only the delta."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()  # size=10
        tracker.track(order)

        # First partial: 3 filled
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "partial",
            "filled_count": 3,
            "remaining_count": 7,
        })
        fills1 = await tracker.check_fills()
        assert len(fills1) == 1
        assert fills1[0].size == 3

        # Second partial: 7 filled (delta = 4)
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "partial",
            "filled_count": 7,
            "remaining_count": 3,
        })
        fills2 = await tracker.check_fills()
        assert len(fills2) == 1
        assert fills2[0].size == 4  # delta: 7 - 3

        # Final execution: all 10 done (delta = 3)
        mock_kalshi.get_order = AsyncMock(return_value={
            "order_id": "kalshi-123",
            "status": "executed",
        })
        fills3 = await tracker.check_fills()
        assert len(fills3) == 1
        assert fills3[0].size == 3  # delta: 10 - 7

        assert fills1[0].size + fills2[0].size + fills3[0].size == order.size

    @pytest.mark.asyncio
    async def test_concurrent_polling(self, mock_kalshi, tmp_db):
        """Multiple orders should be polled concurrently, not sequentially."""
        import time

        call_times = []

        async def slow_get_order(order_id):
            call_times.append(time.monotonic())
            await asyncio.sleep(0.05)  # 50ms per call
            return {"order_id": order_id, "status": "resting"}

        mock_kalshi.get_order = slow_get_order
        tracker = FillTracker(mock_kalshi, tmp_db)

        # Track 5 orders
        for i in range(5):
            order = Order(
                id=f"PE-concurrent-{i}",
                market_id=f"MKT-{i}",
                token_id=f"MKT-{i}_yes",
                side=Side.BUY,
                price=0.50,
                size=5,
                cost=2.50,
                order_type=OrderType.GTC,
                status=OrderStatus.OPEN,
                strategy=StrategyName.AI_PROBABILITY,
                paper=False,
            )
            tracker.track(order)

        start = time.monotonic()
        await tracker.check_fills()
        elapsed = time.monotonic() - start

        # If sequential: ~250ms (5 × 50ms). If concurrent: ~50ms.
        # Allow generous margin but should be well under sequential time.
        assert elapsed < 0.2, f"Polling took {elapsed:.3f}s — not concurrent?"
        assert len(call_times) == 5

    @pytest.mark.asyncio
    async def test_timeout_doesnt_block_others(self, mock_kalshi, tmp_db):
        """A timeout on one order shouldn't prevent other orders from being polled."""
        import asyncio as aio

        call_count = 0

        async def mixed_get_order(order_id):
            nonlocal call_count
            call_count += 1
            if "slow" in order_id:
                await aio.sleep(20)  # Will timeout
            return {"order_id": order_id, "status": "executed"}

        mock_kalshi.get_order = mixed_get_order
        tracker = FillTracker(mock_kalshi, tmp_db, poll_timeout=1)

        slow_order = Order(
            id="PE-slow-order",
            market_id="MKT-SLOW",
            token_id="MKT-SLOW_yes",
            side=Side.BUY, price=0.50, size=5, cost=2.50,
            order_type=OrderType.GTC, status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY, paper=False,
        )
        fast_order = Order(
            id="PE-fast-order",
            market_id="MKT-FAST",
            token_id="MKT-FAST_yes",
            side=Side.BUY, price=0.50, size=5, cost=2.50,
            order_type=OrderType.GTC, status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY, paper=False,
        )
        tracker.track(slow_order)
        tracker.track(fast_order)

        fills = await tracker.check_fills()

        # Fast order should have been filled despite slow order timing out
        assert len(fills) == 1
        assert fills[0].market_id == "MKT-FAST"
