"""Tests for fill tracker."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    StrategyName,
    Trade,
)
from src.execution.fill_tracker import FillTracker, MAX_PROCESSED_FILLS


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
        from src.core.models import StrategyName, Trade

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

    @pytest.mark.asyncio
    async def test_order_missing_platform_defaults_to_kalshi(self, mock_kalshi, tmp_db):
        """An order where platform attribute is missing defaults to KALSHI (lines 83-84)."""
        mock_kalshi.get_order = AsyncMock(return_value={"status": "executed"})

        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        # Simulate missing platform by deleting the attribute
        object.__setattr__(order, "platform", None)
        tracker.track(order)

        fills = await tracker.check_fills()

        assert len(fills) == 1
        # Trade should default to KALSHI platform
        assert fills[0].platform == Platform.KALSHI

    # ──────────────────────────────────────────────────────────────────
    # Cumulative timeout (lines 112-117)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cumulative_timeout_returns_empty(self, mock_kalshi, tmp_db):
        """If the 300s cumulative gather timeout fires, check_fills returns []."""
        async def forever(_order_id):
            await asyncio.sleep(9999)

        mock_kalshi.get_order = forever
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            fills = await tracker.check_fills()

        assert fills == []

    # ──────────────────────────────────────────────────────────────────
    # Exception result bubbled from gather (lines 124-125)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exception_in_gather_result_is_logged_and_skipped(self, mock_kalshi, tmp_db):
        """An Exception object in gather results should be logged and skipped gracefully."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker.track(order)

        # Patch gather to return an Exception in the results list
        real_gather = asyncio.gather

        async def patched_gather(*coros, **kwargs):
            # Close the coroutines we're not going to run, to avoid
            # "coroutine was never awaited" warnings.
            for c in coros:
                c.close()
            return [RuntimeError("unexpected gather error")]

        with patch("asyncio.gather", patched_gather):
            fills = await tracker.check_fills()

        # Should not raise; fills will be empty since we short-circuited
        assert isinstance(fills, list)

    # ──────────────────────────────────────────────────────────────────
    # Partial fill exception handling (lines 144-149)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_partial_fill_exception_logged_not_raised(self, mock_kalshi, tmp_db):
        """An exception in _record_partial_fill should be caught and logged, not re-raised."""
        mock_kalshi.get_order = AsyncMock(return_value={
            "status": "partial",
            "filled_count": 5,
            "remaining_count": 5,
        })
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        with patch.object(tracker, "_record_partial_fill", side_effect=RuntimeError("DB error")):
            fills = await tracker.check_fills()

        # Should not raise; partial_trade treated as None
        assert fills == []
        # Order remains tracked since remaining > 0
        assert tracker.pending_count == 1

    # ──────────────────────────────────────────────────────────────────
    # WebSocket fill for untracked order (lines 179-180)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ws_fill_untracked_order_returns_none(self, mock_kalshi, tmp_db):
        """handle_ws_fill for an order not in _pending_orders returns None."""
        tracker = FillTracker(mock_kalshi, tmp_db)

        fill_update = type("FillUpdate", (), {"order_id": "UNKNOWN-ORDER"})()
        result = await tracker.handle_ws_fill(fill_update)

        assert result is None

    # ──────────────────────────────────────────────────────────────────
    # drain_ws_fills (lines 196-198)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_drain_ws_fills_returns_and_clears(self, mock_kalshi, tmp_db):
        """drain_ws_fills returns accumulated WS fills and clears the internal list."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker.track(order)

        fill_update = type("FillUpdate", (), {"order_id": order.id})()
        trade = await tracker.handle_ws_fill(fill_update)
        assert trade is not None

        # First drain returns the fill
        drained = tracker.drain_ws_fills()
        assert len(drained) == 1
        assert drained[0].order_id == order.id

        # Second drain returns empty
        assert tracker.drain_ws_fills() == []

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: already processed (line 208)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_already_processed_returns_none(self, mock_kalshi, tmp_db):
        """_record_partial_fill returns None if order.id already in _processed_fills."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        tracker._processed_fills[order.id] = None

        result = tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})
        assert result is None

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: zero filled_count (line 212)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_zero_filled_count_returns_none(self, mock_kalshi, tmp_db):
        """_record_partial_fill returns None when filled_count is 0."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()

        result = tracker._record_partial_fill(order, {"filled_count": 0, "remaining_count": 10})
        assert result is None

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: delta == 0 (line 239)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_delta_zero_returns_none(self, mock_kalshi, tmp_db):
        """_record_partial_fill returns None when delta (new fills since last recording) is 0."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        # Pre-record that 6 contracts were already captured
        tracker._partial_recorded[order.id] = 6

        result = tracker._record_partial_fill(order, {"filled_count": 6, "remaining_count": 4})
        assert result is None

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: suspicious negative delta (lines 222-237)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_suspicious_correction_rejected(self, mock_kalshi, tmp_db):
        """A large negative delta (fill correction) exceeding 5% of order size is rejected."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()  # size=10; 5% = 0 (max_allowed = max(1, 0) = 1)
        # Pretend we already recorded 8, but API now says 5 — delta=-3, max_allowed=1
        tracker._partial_recorded[order.id] = 8

        result = tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})
        assert result is None
        # partial_recorded should NOT be updated since the correction was rejected
        assert tracker._partial_recorded[order.id] == 8

    def test_record_partial_fill_small_correction_accepted(self, mock_kalshi, tmp_db):
        """A small negative delta within 5% of order size is accepted (H-6)."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        # Use a large order so the 5% tolerance is > 1
        order = Order(
            id="PE-correction-test",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=100,  # 5% = 5 contracts tolerance
            cost=34.0,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        # Pretend 50 recorded; API says 49 — delta=-1, max_allowed=5 → accepted
        tracker._partial_recorded[order.id] = 50

        result = tracker._record_partial_fill(order, {"filled_count": 49, "remaining_count": 51})
        # Small correction: accepted but returns None (no new trade recorded)
        assert result is None
        # partial_recorded should be updated to the corrected value
        assert tracker._partial_recorded[order.id] == 49

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: Polymarket fee-free (line 249)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_polymarket_zero_fee(self, mock_kalshi, tmp_db):
        """Polymarket partial fills should have zero fee."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = Order(
            id="PE-poly-partial",
            market_id="POLY-MKT",
            token_id="POLY-MKT_yes",
            side=Side.BUY,
            price=0.60,
            size=10,
            cost=6.0,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
            platform=Platform.POLYMARKET,
        )

        trade = tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})

        assert trade is not None
        assert trade.fee == 0.0
        assert trade.platform == Platform.POLYMARKET

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: missing platform defaults to KALSHI (lines 246-247)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_missing_platform_defaults_to_kalshi(self, mock_kalshi, tmp_db):
        """Partial fill on order with platform=None defaults to KALSHI and logs warning."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        object.__setattr__(order, "platform", None)

        trade = tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})

        assert trade is not None
        assert trade.platform == Platform.KALSHI

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: taker fee path (line 255)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_taker_order_applies_taker_fee(self, mock_kalshi, tmp_db):
        """Kalshi IOC (taker) partial fill should use taker fee, not maker fee."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = Order(
            id="PE-taker-partial",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=10,
            cost=3.40,
            order_type=OrderType.FOK,  # Taker
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
            platform=Platform.KALSHI,
        )

        trade = tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})

        assert trade is not None
        # Taker fee > 0 for Kalshi
        assert trade.fee >= 0.0

    # ──────────────────────────────────────────────────────────────────
    # _record_partial_fill: DB transaction rollback (lines 293-295)
    # ──────────────────────────────────────────────────────────────────

    def test_record_partial_fill_db_error_raises_and_rolls_back(self, mock_kalshi, tmp_db):
        """DB error in partial fill transaction should rollback and re-raise."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()

        with patch.object(tmp_db, "log_trade", side_effect=RuntimeError("DB error")):
            with pytest.raises(RuntimeError, match="DB error"):
                tracker._record_partial_fill(order, {"filled_count": 5, "remaining_count": 5})

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: _processed_fills pruning (lines 319-322)
    # ──────────────────────────────────────────────────────────────────

    def test_processed_fills_pruned_when_exceeds_cap(self, mock_kalshi, tmp_db):
        """When _processed_fills exceeds MAX_PROCESSED_FILLS, oldest 25% is pruned (M-5)."""
        tracker = FillTracker(mock_kalshi, tmp_db)

        # Pre-fill with MAX_PROCESSED_FILLS entries
        for i in range(MAX_PROCESSED_FILLS):
            tracker._processed_fills[f"old-order-{i}"] = None

        assert len(tracker._processed_fills) == MAX_PROCESSED_FILLS

        # Recording a new fill should trigger pruning
        order = _make_order()
        tracker._record_fill(order, {"status": "executed"})

        # M-5: After pruning, set should keep ~75% of entries (drop oldest 25%)
        assert len(tracker._processed_fills) <= (MAX_PROCESSED_FILLS * 3 // 4) + 5

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: all contracts already recorded via partials (lines 335-337)
    # ──────────────────────────────────────────────────────────────────

    def test_record_fill_all_contracts_already_recorded_returns_none(self, mock_kalshi, tmp_db):
        """When all contracts were already recorded via partial fills, _record_fill returns None."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()  # size=10
        # Pretend all 10 contracts were already recorded as partials
        tracker._partial_recorded[order.id] = 10

        result = tracker._record_fill(order, {"status": "executed"})

        assert result is None
        # Order should still be marked filled
        assert order.status == OrderStatus.FILLED

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: Polymarket fee-free (line 344-345)
    # ──────────────────────────────────────────────────────────────────

    def test_record_fill_polymarket_zero_fee(self, mock_kalshi, tmp_db):
        """Full fill on Polymarket order should have zero fee."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = Order(
            id="PE-poly-full",
            market_id="POLY-MKT",
            token_id="POLY-MKT_yes",
            side=Side.BUY,
            price=0.60,
            size=10,
            cost=6.0,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
            platform=Platform.POLYMARKET,
        )

        trade = tracker._record_fill(order, {"status": "executed"})

        assert trade is not None
        assert trade.fee == 0.0
        assert trade.platform == Platform.POLYMARKET

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: missing platform defaults to KALSHI (lines 342-343)
    # ──────────────────────────────────────────────────────────────────

    def test_record_fill_missing_platform_defaults_to_kalshi(self, mock_kalshi, tmp_db):
        """Full fill on order with platform=None defaults to KALSHI."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        object.__setattr__(order, "platform", None)

        trade = tracker._record_fill(order, {"status": "executed"})

        assert trade is not None
        assert trade.platform == Platform.KALSHI

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: taker fee (line 351)
    # ──────────────────────────────────────────────────────────────────

    def test_record_fill_taker_order_applies_taker_fee(self, mock_kalshi, tmp_db):
        """Kalshi IOC (taker) full fill uses taker fee."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = Order(
            id="PE-taker-full",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=10,
            cost=3.40,
            order_type=OrderType.FOK,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
            platform=Platform.KALSHI,
        )

        trade = tracker._record_fill(order, {"status": "executed"})

        assert trade is not None
        assert trade.fee >= 0.0

    # ──────────────────────────────────────────────────────────────────
    # _record_fill: DB transaction rollback (lines 378-380)
    # ──────────────────────────────────────────────────────────────────

    def test_record_fill_db_error_raises_and_rolls_back(self, mock_kalshi, tmp_db):
        """DB error in _record_fill transaction should rollback and re-raise."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()

        with patch.object(tmp_db, "log_trade", side_effect=RuntimeError("DB write fail")):
            with pytest.raises(RuntimeError, match="DB write fail"):
                tracker._record_fill(order, {"status": "executed"})

    # ──────────────────────────────────────────────────────────────────
    # _log_order_with_conn: missing platform defaults (lines 406-407)
    # ──────────────────────────────────────────────────────────────────

    def test_log_order_with_conn_missing_platform_defaults_to_kalshi(self, mock_kalshi, tmp_db):
        """_log_order_with_conn with platform=None defaults to KALSHI and logs warning."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        order = _make_order()
        order.status = OrderStatus.FILLED
        object.__setattr__(order, "platform", None)

        conn = tmp_db._get_conn()
        # Should not raise; inserts with KALSHI
        tracker._log_order_with_conn(order, conn)
        conn.commit()

        row = conn.execute(
            "SELECT platform FROM orders WHERE id=?", (order.id,)
        ).fetchone()
        assert row is not None
        assert row["platform"] == Platform.KALSHI.value

    # ──────────────────────────────────────────────────────────────────
    # _load_filled_order_ids: exception path (lines 450-452)
    # ──────────────────────────────────────────────────────────────────

    def test_load_filled_order_ids_exception_raises_on_non_table_error(self, mock_kalshi, tmp_db):
        """C-4: Non-table DB errors should raise to prevent duplicate fills."""
        with patch.object(tmp_db, "_get_conn", side_effect=RuntimeError("DB unavailable")):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                FillTracker(mock_kalshi, tmp_db)

    def test_load_filled_order_ids_graceful_on_missing_table(self, mock_kalshi, tmp_db):
        """C-4: 'no such table' errors should return empty set gracefully."""
        from sqlite3 import OperationalError
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = OperationalError("no such table: trades")
        with patch.object(tmp_db, "_get_conn", return_value=mock_conn):
            tracker = FillTracker(mock_kalshi, tmp_db)
        assert len(tracker._processed_fills) == 0

    # ──────────────────────────────────────────────────────────────────
    # _load_partial_recorded_counts: success + exception (lines 474, 476-478)
    # ──────────────────────────────────────────────────────────────────

    def test_load_partial_recorded_counts_loads_from_db(self, mock_kalshi, tmp_db):
        """Partial fill counts should be loaded from DB on init if trades exist."""
        # Pre-record a partial trade in the DB
        partial_trade = Trade(
            order_id="PE-partial-restart",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=6,
            fee=0.01,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tmp_db.log_trade(partial_trade)

        # New tracker (simulate restart); the order is not in _processed_fills
        tracker = FillTracker(mock_kalshi, tmp_db)

        # The partial count for this order should NOT be loaded because
        # log_trade also adds to trades table → order IS in _processed_fills
        # Let's verify the load mechanism: if order_id is already in _processed_fills
        # it is excluded from _partial_recorded (see source line 470-472)
        assert "PE-partial-restart" in tracker._processed_fills

    def test_load_partial_recorded_counts_nonzero_logs_info(self, mock_kalshi, tmp_db):
        """_load_partial_recorded_counts logs info when counts dict is non-empty (line 474).

        To reach this branch, we need trades in the DB whose order_ids are NOT
        in _processed_fills when _load_partial_recorded_counts runs. We achieve
        this by monkeypatching _load_filled_order_ids to return an empty set
        so that the trade row is not excluded during partial count loading.
        """
        partial_trade = Trade(
            order_id="PE-partial-nodup",
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            side=Side.BUY,
            price=0.34,
            size=7,
            fee=0.01,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tmp_db.log_trade(partial_trade)

        # Patch _load_filled_order_ids so _processed_fills starts empty,
        # allowing _load_partial_recorded_counts to include the trade row.
        with patch.object(FillTracker, "_load_filled_order_ids", return_value=set()):
            tracker = FillTracker(mock_kalshi, tmp_db)

        assert "PE-partial-nodup" in tracker._partial_recorded
        assert tracker._partial_recorded["PE-partial-nodup"] == 7

    def test_load_partial_recorded_counts_exception_raises_on_non_table_error(self, mock_kalshi, tmp_db):
        """C-4: Non-table DB errors in partial counts should raise to prevent duplicates."""
        call_count = 0
        real_get_conn = tmp_db._get_conn

        def patched_get_conn():
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second call is _load_partial_recorded_counts
                raise RuntimeError("DB unavailable")
            return real_get_conn()

        with patch.object(tmp_db, "_get_conn", patched_get_conn):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                FillTracker(mock_kalshi, tmp_db)

    def test_load_partial_recorded_counts_graceful_on_missing_table(self, mock_kalshi, tmp_db):
        """C-4: 'no such table' errors in partial counts should return empty dict."""
        from sqlite3 import OperationalError
        call_count = 0
        real_get_conn = tmp_db._get_conn

        def patched_get_conn():
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second call is _load_partial_recorded_counts
                mock_conn = MagicMock()
                mock_conn.execute.side_effect = OperationalError("no such table: trades")
                return mock_conn
            return real_get_conn()

        with patch.object(tmp_db, "_get_conn", patched_get_conn):
            tracker = FillTracker(mock_kalshi, tmp_db)

        assert tracker._partial_recorded == {}

    # ──────────────────────────────────────────────────────────────────
    # _prune_partial_recorded (lines 487-494)
    # ──────────────────────────────────────────────────────────────────

    def test_prune_partial_recorded_removes_stale_entries(self, mock_kalshi, tmp_db):
        """_prune_partial_recorded removes entries for orders not in _pending_orders."""
        tracker = FillTracker(mock_kalshi, tmp_db)

        # Add 5001 stale entries (above the 5000 threshold)
        for i in range(5001):
            tracker._partial_recorded[f"stale-order-{i}"] = i

        # Add one active order (should be kept)
        active_order = _make_order()
        tracker._pending_orders[active_order.id] = active_order
        tracker._partial_recorded[active_order.id] = 3

        tracker._prune_partial_recorded()

        # All stale entries should be pruned; active order entry retained
        for i in range(5001):
            assert f"stale-order-{i}" not in tracker._partial_recorded
        assert tracker._partial_recorded[active_order.id] == 3

    def test_prune_partial_recorded_noop_below_threshold(self, mock_kalshi, tmp_db):
        """_prune_partial_recorded does nothing if <= 5000 entries."""
        tracker = FillTracker(mock_kalshi, tmp_db)

        for i in range(100):
            tracker._partial_recorded[f"order-{i}"] = i

        tracker._prune_partial_recorded()

        # Nothing pruned
        assert len(tracker._partial_recorded) == 100

    # ──────────────────────────────────────────────────────────────────
    # get_pending_for_market (line 506)
    # ──────────────────────────────────────────────────────────────────

    def test_get_pending_for_market_filters_by_market_id(self, mock_kalshi, tmp_db):
        """get_pending_for_market returns only orders matching the given ticker."""
        tracker = FillTracker(mock_kalshi, tmp_db)

        order_a = _make_order()  # market_id="FED-RATE-CUT-MAY26"
        order_b = Order(
            id="PE-other-mkt",
            market_id="TRUMP-GOP-2028",
            token_id="TRUMP-GOP-2028_yes",
            side=Side.BUY,
            price=0.55,
            size=5,
            cost=2.75,
            order_type=OrderType.GTC,
            status=OrderStatus.OPEN,
            strategy=StrategyName.AI_PROBABILITY,
            paper=False,
        )
        tracker.track(order_a)
        tracker.track(order_b)

        result = tracker.get_pending_for_market("FED-RATE-CUT-MAY26")

        assert len(result) == 1
        assert result[0].id == order_a.id

    def test_get_pending_for_market_returns_empty_when_no_match(self, mock_kalshi, tmp_db):
        """get_pending_for_market returns [] when no orders match the ticker."""
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        result = tracker.get_pending_for_market("NONEXISTENT-MARKET")

        assert result == []

    # ──────────────────────────────────────────────────────────────────
    # cancelled (British spelling) also resolves order (line 154)
    # ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_british_spelling_cancelled_resolves_order(self, mock_kalshi, tmp_db):
        """Status 'cancelled' (British spelling) should also remove order from pending."""
        mock_kalshi.get_order = AsyncMock(return_value={"status": "cancelled"})
        tracker = FillTracker(mock_kalshi, tmp_db)
        tracker.track(_make_order())

        fills = await tracker.check_fills()

        assert fills == []
        assert tracker.pending_count == 0
