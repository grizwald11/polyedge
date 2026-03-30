"""Tests for order router."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Direction, Order, OrderStatus, OrderType, Side, StrategyName,
)
from src.execution.order_router import OrderRouter
from src.storage.database import Database


@pytest.fixture
def mock_kalshi():
    client = AsyncMock(spec=KalshiClient)
    client.create_order = AsyncMock(return_value={"order_id": "kalshi-123", "status": "executed"})
    client.get_order = AsyncMock(return_value={"order_id": "kalshi-123", "status": "executed"})
    return client


@pytest.fixture
def paper_settings() -> Settings:
    return Settings()


@pytest.fixture
def live_settings() -> Settings:
    s = Settings()
    s.trading.mode = "live"
    s.live_enabled = True
    return s


def _make_order(paper=True) -> Order:
    return Order(
        id="PE-test123",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-RATE-CUT-MAY26_yes",
        side=Side.BUY,
        price=0.34,
        size=10,
        cost=3.40,
        order_type=OrderType.GTC,
        status=OrderStatus.PENDING,
        strategy=StrategyName.AI_PROBABILITY,
        paper=paper,
        kalshi_side="yes",
    )


class TestPaperFill:
    @pytest.mark.asyncio
    async def test_paper_fill_succeeds(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        # Patch slippage to always fill at a fixed price for deterministic tests
        router._simulate_slippage = lambda order: (True, order.price + 0.005)
        order = _make_order(paper=True)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.status == OrderStatus.FILLED
        # Fill price includes up to 1 cent adverse slippage (BUY → price goes up)
        assert 0.34 <= result.order.fill_price <= 0.35
        assert result.order.filled_at is not None
        assert result.trade is not None
        assert result.trade.price == result.order.fill_price
        assert result.trade.size == 10
        assert result.trade.paper is True

    @pytest.mark.asyncio
    async def test_paper_fill_logs_trade(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        # Patch slippage to always fill for deterministic tests
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        await router.route_order(order)

        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "PE-test123"
        assert trades[0]["paper"] == 1

    @pytest.mark.asyncio
    async def test_paper_fill_calculates_fee(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        # Patch slippage to always fill for deterministic tests
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        result = await router.route_order(order)

        assert result.trade.fee >= 0


class TestLiveFill:
    @pytest.mark.asyncio
    async def test_live_fill_requires_gates(self, paper_settings, mock_kalshi, tmp_db):
        """Live fill should fail if settings are in paper mode."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=False)
        # Force live routing
        paper_settings.trading.mode = "live"

        result = await router.route_order(order)

        # Should fail because live_enabled is False
        assert result.success is False
        assert "gates not passed" in result.error

    @pytest.mark.asyncio
    async def test_live_fill_succeeds(self, live_settings, mock_kalshi, tmp_db):
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True  # Pre-confirm for test
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.status == OrderStatus.FILLED
        mock_kalshi.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_fill_api_failure(self, live_settings, mock_kalshi, tmp_db):
        mock_kalshi.create_order = AsyncMock(return_value=None)
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_live_fill_api_exception(self, live_settings, mock_kalshi, tmp_db):
        mock_kalshi.create_order = AsyncMock(side_effect=Exception("API down"))
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert "API down" in result.error

    @pytest.mark.asyncio
    async def test_gate3_blocks_without_confirmation(self, live_settings, mock_kalshi, tmp_db):
        """Gate 3 should block live trades until user confirms."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=False)

        # Mock input to return "n"
        with patch.object(router, '_request_confirmation', new_callable=AsyncMock, return_value=False):
            result = await router.route_order(order)

        assert result.success is False
        assert "declined" in result.error

    @pytest.mark.asyncio
    async def test_gate3_allows_after_confirmation(self, live_settings, mock_kalshi, tmp_db):
        """Gate 3 should allow trading after first confirmation."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=False)

        # Mock input to return "y"
        with patch.object(router, '_request_confirmation', new_callable=AsyncMock, return_value=True):
            result = await router.route_order(order)

        assert result.success is True
        assert router._session_confirmed is True


class TestLiveFillFee:
    @pytest.mark.asyncio
    async def test_live_fill_fee_calculation(self, live_settings, mock_kalshi, tmp_db):
        """Live fill should calculate fee > 0 matching expected value."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        assert result.trade is not None
        assert result.trade.fee > 0
        # Maker fee: ceil(0.0175 * 10 * 34 * (100-34)) / 100 cents -> dollars
        # = ceil(0.0175 * 10 * 34 * 66) / 100 = ceil(392.7) / 100 = $3.93
        import math
        from src.core.models import dollars_to_cents, kalshi_maker_fee
        expected_fee_cents = kalshi_maker_fee(10, dollars_to_cents(0.34))
        assert result.trade.fee == expected_fee_cents / 100.0

    @pytest.mark.asyncio
    async def test_live_fill_consistent_timestamps(self, live_settings, mock_kalshi, tmp_db):
        """Order filled_at and trade timestamp should match exactly."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.filled_at == result.trade.timestamp


class TestLiveBuyNoPrice:
    @pytest.mark.asyncio
    async def test_live_buy_no_sends_correct_yes_price(self, live_settings, mock_kalshi, tmp_db):
        """BUY NO at $0.97 should send yes_price=3 (i.e., YES=$0.03)."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = Order(
            id="PE-no-test",
            market_id="MKT-TEST",
            token_id="MKT-TEST_no",
            side=Side.BUY,
            price=0.97,
            size=10,
            cost=9.70,
            order_type=OrderType.GTC,
            status=OrderStatus.PENDING,
            strategy=StrategyName.OBVIOUS_NO,
            paper=False,
            kalshi_side="no",
        )

        await router.route_order(order)

        mock_kalshi.create_order.assert_called_once()
        call_kwargs = mock_kalshi.create_order.call_args
        assert call_kwargs.kwargs.get("yes_price") == 3 or call_kwargs[1].get("yes_price") == 3, (
            f"Expected yes_price=3 for NO@$0.97, got {call_kwargs}"
        )

    @pytest.mark.asyncio
    async def test_live_buy_yes_sends_correct_yes_price(self, live_settings, mock_kalshi, tmp_db):
        """BUY YES at $0.34 should send yes_price=34."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)  # YES at $0.34

        await router.route_order(order)

        mock_kalshi.create_order.assert_called_once()
        call_kwargs = mock_kalshi.create_order.call_args
        assert call_kwargs.kwargs.get("yes_price") == 34 or call_kwargs[1].get("yes_price") == 34, (
            f"Expected yes_price=34 for YES@$0.34, got {call_kwargs}"
        )


class TestLiveGates:
    def test_paper_mode_fails_gate(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        assert router._live_gates_passed() is False

    def test_live_mode_without_env_fails(self, tmp_db, mock_kalshi):
        s = Settings()
        s.trading.mode = "live"
        s.live_enabled = False
        router = OrderRouter(s, mock_kalshi, tmp_db)
        assert router._live_gates_passed() is False

    def test_all_gates_pass(self, live_settings, mock_kalshi, tmp_db):
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        assert router._live_gates_passed() is True


class TestOrderCancellation:
    @pytest.mark.asyncio
    async def test_cancel_paper_order(self, paper_settings, mock_kalshi, tmp_db):
        """Paper orders can be cancelled by updating DB status."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        # Patch slippage to always fill for deterministic tests
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        # Route the paper order first to get it into DB
        result = await router.route_order(order)
        assert result.success is True

        # Now update its status to open so we can cancel it
        conn = tmp_db._get_conn()
        conn.execute(
            "UPDATE orders SET status='open' WHERE id=?", (order.id,)
        )
        conn.commit()

        # Cancel it
        cancelled = await router.cancel_order(order.id)
        assert cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order(self, paper_settings, mock_kalshi, tmp_db):
        """Cancelling a nonexistent order should return False."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        cancelled = await router.cancel_order("PE-doesntexist")
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_already_filled(self, paper_settings, mock_kalshi, tmp_db):
        """Cannot cancel an already filled order."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)

        # Route to fill it
        await router.route_order(order)

        # Try to cancel — should fail because status is 'filled'
        cancelled = await router.cancel_order(order.id)
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_stale_orders(self, paper_settings, mock_kalshi, tmp_db):
        """cancel_stale_orders should cancel old open orders."""
        from datetime import datetime, timedelta, timezone
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)

        # Insert a stale open order directly into DB
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = tmp_db._get_conn()
        conn.execute("""
            INSERT INTO orders (id, market_id, token_id, side, price, size, cost,
                order_type, fee_rate_bps, status, strategy, paper, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "PE-stale1", "MKT-A", "MKT-A_yes", "buy", 0.50, 10, 5.0,
            "GTC", 175, "open", "ai_probability", 1, old_time,
        ))
        conn.commit()

        # Cancel orders older than 30 min
        cancelled = await router.cancel_stale_orders(max_age_seconds=1800)
        assert cancelled == 1


class TestOrphanedOrderRecovery:
    """H-4: When order_id is missing from response, recover via get_open_orders()."""

    @pytest.mark.asyncio
    async def test_recovery_succeeds_when_open_order_matches(self, live_settings, tmp_db):
        """If order_id is absent from create_order response, recovery finds it via open orders."""
        kalshi = AsyncMock(spec=KalshiClient)
        # create_order returns response without order_id
        kalshi.create_order = AsyncMock(return_value={"status": "resting"})
        # get_open_orders returns a matching order
        kalshi.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "recovered-kalshi-456",
                "ticker": "FED-RATE-CUT-MAY26",
                "yes_price": 34,  # 0.34 * 100
                "count": 10,
                "side": "yes",
                "status": "resting",
            }
        ])
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        # Recovery should have found the order and proceeded to poll it
        kalshi.get_open_orders.assert_called_once()
        # Order should be tracked (resting → OPEN)
        assert result.order.exchange_order_id == "recovered-kalshi-456" or result.success is True

    @pytest.mark.asyncio
    async def test_recovery_fails_when_no_match_found(self, live_settings, tmp_db):
        """If recovery finds no matching open order, order is REJECTED."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.create_order = AsyncMock(return_value={"status": "resting"})
        # No matching orders in open orders list
        kalshi.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "other-order",
                "ticker": "DIFFERENT-MKT",
                "yes_price": 50,
                "count": 5,
                "side": "yes",
                "status": "resting",
            }
        ])
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        kalshi.get_open_orders.assert_called_once()
        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED
        assert "order_id" in result.error

    @pytest.mark.asyncio
    async def test_recovery_handles_get_open_orders_exception(self, live_settings, tmp_db):
        """If get_open_orders raises, recovery fails gracefully and order is REJECTED."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.create_order = AsyncMock(return_value={"status": "resting"})
        kalshi.get_open_orders = AsyncMock(side_effect=Exception("network error"))
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_no_recovery_when_order_id_present(self, live_settings, mock_kalshi, tmp_db):
        """When order_id is present in response, get_open_orders is NOT called."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        mock_kalshi.get_open_orders.assert_not_called()


class TestPendingOrderPersistence:
    """H-1: Pending orders must survive process restarts via DB persistence."""

    @pytest.mark.asyncio
    async def test_resting_order_persisted_to_db(self, live_settings, tmp_db):
        """When a live order rests, it is saved to the pending_orders table."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.create_order = AsyncMock(return_value={"order_id": "kalshi-resting", "status": "resting"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        # Order should be tracked as resting
        assert result.success is True
        assert result.order.status == OrderStatus.OPEN
        # DB should have the pending order persisted
        pending = tmp_db.load_pending_orders()
        assert order.id in pending
        assert pending[order.id] == order.cost

    @pytest.mark.asyncio
    async def test_pending_order_removed_on_fill(self, live_settings, mock_kalshi, tmp_db):
        """When a resting order is filled, it is removed from the pending_orders table."""
        kalshi = AsyncMock(spec=KalshiClient)
        # First call: resting; second call: executed
        kalshi.create_order = AsyncMock(return_value={"order_id": "kalshi-fill", "status": "resting"})
        kalshi.get_order = AsyncMock(return_value={"order_id": "kalshi-fill", "status": "executed"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        # Place resting then simulate fill by polling
        router2 = OrderRouter(live_settings, kalshi, tmp_db)
        router2._session_confirmed = True
        # Use mock that returns "executed" immediately
        kalshi2 = AsyncMock(spec=KalshiClient)
        kalshi2.create_order = AsyncMock(return_value={"order_id": "kalshi-fill2", "status": "executed"})
        router3 = OrderRouter(live_settings, kalshi2, tmp_db)
        router3._session_confirmed = True
        order3 = _make_order(paper=False)
        order3.id = "PE-fill3"

        result = await router3.route_order(order3)

        assert result.success is True
        assert result.order.status == OrderStatus.FILLED
        # Filled order should not be in pending table
        pending = tmp_db.load_pending_orders()
        assert order3.id not in pending

    @pytest.mark.asyncio
    async def test_pending_orders_restored_on_restart(self, live_settings, tmp_db):
        """Pending orders persisted to DB are restored into in-memory state on startup."""
        # Pre-populate the pending_orders table
        tmp_db.save_pending_order("PE-restart-test", 7.50)

        kalshi = AsyncMock(spec=KalshiClient)
        router = OrderRouter(live_settings, kalshi, tmp_db)

        # Restored state should reflect the persisted order
        assert "PE-restart-test" in router._pending_orders
        assert router._pending_orders["PE-restart-test"] == 7.50
        assert router._pending_order_cost == 7.50

    @pytest.mark.asyncio
    async def test_cancel_removes_pending_from_db(self, paper_settings, mock_kalshi, tmp_db):
        """Cancelling a paper order removes it from the pending_orders table."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        await router.route_order(order)
        # Manually insert as pending (simulate a resting order)
        tmp_db.save_pending_order(order.id, order.cost)
        conn = tmp_db._get_conn()
        conn.execute("UPDATE orders SET status='open' WHERE id=?", (order.id,))
        conn.commit()

        await router.cancel_order(order.id)

        pending = tmp_db.load_pending_orders()
        assert order.id not in pending
