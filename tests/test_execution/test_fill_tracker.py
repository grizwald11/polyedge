"""Tests for fill tracker."""

from __future__ import annotations

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
