"""Tests for order router."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.kalshi_client import KalshiClient, KalshiRateLimitError
from src.core.models import (
    Direction,
    Order,
    OrderStatus,
    OrderType,
    Side,
    StrategyName,
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
        # get_order is called during polling; must return a dict to avoid
        # unawaited coroutines from .get().lower() on an AsyncMock.
        kalshi.get_order = AsyncMock(return_value={"order_id": "recovered-kalshi-456", "status": "resting"})
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
        # get_order is called during polling; must return a dict to avoid
        # unawaited coroutines from .get().lower() on an AsyncMock.
        kalshi.get_order = AsyncMock(return_value={"order_id": "kalshi-resting", "status": "resting"})
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


# ──────────────────────────────────────────────
# New comprehensive tests
# ──────────────────────────────────────────────

class TestPaperFillSlippage:
    """Paper mode: fill simulation, fill probability, slippage modeling."""

    @pytest.mark.asyncio
    async def test_paper_sell_slippage_adverse(self, paper_settings, mock_kalshi, tmp_db):
        """SELL orders should get adverse slippage (lower price)."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        # Force a specific slippage: adverse for SELL means price goes DOWN
        router._simulate_slippage = lambda order: (True, max(0.01, order.price - 0.005))
        order = _make_order(paper=True)
        order.side = Side.SELL

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.fill_price <= order.price

    @pytest.mark.asyncio
    async def test_paper_fill_miss_returns_cancelled(self, paper_settings, mock_kalshi, tmp_db):
        """When paper fill misses (simulated no-fill), order is CANCELLED."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (False, order.price)
        order = _make_order(paper=True)

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.CANCELLED
        assert "missed fill" in result.error.lower()
        assert result.trade is None

    @pytest.mark.asyncio
    async def test_paper_fill_price_clamped_to_99(self, paper_settings, mock_kalshi, tmp_db):
        """Fill price should never exceed 0.99 even with slippage."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)
        order.price = 0.99
        # Force slippage that would push above 0.99
        router._simulate_slippage = lambda o: (True, 0.99)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.fill_price <= 0.99

    @pytest.mark.asyncio
    async def test_simulate_slippage_buy_direction(self, paper_settings, mock_kalshi, tmp_db):
        """BUY slippage should increase price (adverse for buyer)."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)
        order.side = Side.BUY
        # Run many trials — at least some should have slippage > 0
        prices = []
        for _ in range(50):
            filled, price = router._simulate_slippage(order)
            if filled:
                prices.append(price)
        # All fill prices should be >= order price (adverse slippage for BUY)
        assert all(p >= order.price for p in prices)

    @pytest.mark.asyncio
    async def test_simulate_slippage_miss_rate(self, paper_settings, mock_kalshi, tmp_db):
        """Roughly 15% of paper limit orders should miss (statistical check)."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)
        n = 1000
        misses = sum(1 for _ in range(n) if not router._simulate_slippage(order)[0])
        # Allow generous range: 5% to 30%
        miss_rate = misses / n
        assert 0.05 < miss_rate < 0.30, f"Miss rate {miss_rate:.2%} outside expected range"


class TestPaperRiskRejection:
    """Paper mode: order rejection scenarios."""

    @pytest.mark.asyncio
    async def test_sell_rejected_no_position(self, paper_settings, mock_kalshi, tmp_db):
        """SELL order rejected when no position exists and position_manager is set."""
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm.get_position.return_value = None
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db, position_manager=pm)
        order = _make_order(paper=True)
        order.side = Side.SELL

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED
        assert "no open position" in result.error.lower()

    @pytest.mark.asyncio
    async def test_sell_size_clamped_to_position(self, paper_settings, mock_kalshi, tmp_db):
        """SELL order size clamped to actual position size."""
        from unittest.mock import MagicMock

        from src.core.models import Direction, Position
        pm = MagicMock()
        pm.get_position.return_value = Position(
            market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes",
            direction=Direction.BUY_YES,
            size=5,
            avg_entry_price=0.34,
        )
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db, position_manager=pm)
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)
        order.side = Side.SELL
        order.size = 20  # Larger than position

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.size == 5  # Clamped to position size


class TestLiveKalshiSubmission:
    """Live mode: Kalshi order submission (mock the kalshi client)."""

    @pytest.mark.asyncio
    async def test_live_kalshi_order_params(self, live_settings, mock_kalshi, tmp_db):
        """Verify correct parameters are passed to Kalshi create_order."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        await router.route_order(order)

        mock_kalshi.create_order.assert_called_once_with(
            ticker="FED-RATE-CUT-MAY26",
            side="yes",
            yes_price=34,
            count=10,
            order_type="limit",
            action="buy",
        )

    @pytest.mark.asyncio
    async def test_live_fok_order_type(self, live_settings, mock_kalshi, tmp_db):
        """FOK orders should send order_type='market' to Kalshi."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)
        order.order_type = OrderType.FOK

        await router.route_order(order)

        call_kwargs = mock_kalshi.create_order.call_args
        assert call_kwargs.kwargs.get("order_type") == "market" or call_kwargs[1].get("order_type") == "market"

    @pytest.mark.asyncio
    async def test_live_rate_limit_error(self, live_settings, tmp_db):
        """KalshiRateLimitError should be caught gracefully."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.create_order = AsyncMock(side_effect=KalshiRateLimitError("rate limited"))
        kalshi.get_balance = AsyncMock(return_value=1000.0)
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED


class TestBalancePreFlight:
    """Live mode: balance pre-flight check rejection."""

    @pytest.mark.asyncio
    async def test_insufficient_balance_rejects(self, live_settings, tmp_db):
        """Order rejected when balance is insufficient."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(return_value=1.00)  # Only $1 available
        kalshi.create_order = AsyncMock(return_value={"order_id": "k-1", "status": "executed"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)
        order.cost = 10.00  # $10 cost > $1 balance

        result = await router.route_order(order)

        assert result.success is False
        assert "insufficient balance" in result.error.lower()
        kalshi.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_balance_check_timeout_proceeds(self, live_settings, tmp_db):
        """If balance check times out, order proceeds (non-blocking)."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(side_effect=asyncio.TimeoutError())
        kalshi.create_order = AsyncMock(return_value={"order_id": "k-1", "status": "executed"})
        kalshi.get_order = AsyncMock(return_value={"order_id": "k-1", "status": "executed"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        kalshi.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_balance_none_proceeds(self, live_settings, tmp_db):
        """If balance returns None (API quirk), order proceeds."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(return_value=None)
        kalshi.create_order = AsyncMock(return_value={"order_id": "k-2", "status": "executed"})
        kalshi.get_order = AsyncMock(return_value={"order_id": "k-2", "status": "executed"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True


class TestTimeoutReconciliation:
    """Live mode: timeout reconciliation after order creation timeout."""

    @pytest.mark.asyncio
    async def test_timeout_triggers_reconciliation(self, live_settings, tmp_db):
        """When create_order times out, reconciliation checks open orders."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(return_value=1000.0)
        kalshi.create_order = AsyncMock(side_effect=asyncio.TimeoutError())
        kalshi.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "recovered-timeout",
                "ticker": "FED-RATE-CUT-MAY26",
                "yes_price": 34,
                "count": 10,
                "side": "yes",
                "status": "resting",
            }
        ])
        # get_order is called during polling after reconciliation; must return
        # a dict to avoid unawaited coroutines from .get().lower() on AsyncMock.
        kalshi.get_order = AsyncMock(return_value={"order_id": "recovered-timeout", "status": "resting"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        kalshi.get_open_orders.assert_called()

    @pytest.mark.asyncio
    async def test_timeout_no_match_returns_unknown(self, live_settings, tmp_db):
        """When timeout reconciliation finds no match, order status is unknown."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(return_value=1000.0)
        kalshi.create_order = AsyncMock(side_effect=asyncio.TimeoutError())
        kalshi.get_open_orders = AsyncMock(return_value=[])
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert "timeout" in result.error.lower() or "unknown" in result.error.lower()


class TestPlatformRouting:
    """Platform routing: correct routing to Kalshi."""

    @pytest.mark.asyncio
    async def test_kalshi_order_routes_to_live_fill(self, live_settings, mock_kalshi, tmp_db):
        """Kalshi platform orders go through _live_fill."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)  # Default platform is KALSHI

        result = await router.route_order(order)

        assert result.success is True
        mock_kalshi.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_paper_mode_routes_to_paper(self, paper_settings, mock_kalshi, tmp_db):
        """Paper mode routes Kalshi orders to paper fill."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (True, order.price)

        kalshi_order = _make_order(paper=True)
        result = await router.route_order(kalshi_order)
        assert result.success is True
        assert result.trade.paper is True

        # Should not hit live APIs
        mock_kalshi.create_order.assert_not_called()


class TestOrderLogging:
    """Order logging: verify orders are logged to database."""

    @pytest.mark.asyncio
    async def test_filled_order_persisted(self, paper_settings, mock_kalshi, tmp_db):
        """Filled paper order is saved to orders table."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        await router.route_order(order)

        conn = tmp_db._get_conn()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order.id,)).fetchone()
        assert row is not None
        assert row["status"] == "FILLED"
        assert row["market_id"] == "FED-RATE-CUT-MAY26"

    @pytest.mark.asyncio
    async def test_rejected_order_persisted(self, paper_settings, mock_kalshi, tmp_db):
        """Rejected orders are also persisted to database with reason."""
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm.get_position.return_value = None
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db, position_manager=pm)
        order = _make_order(paper=True)
        order.side = Side.SELL

        await router.route_order(order)

        conn = tmp_db._get_conn()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order.id,)).fetchone()
        assert row is not None
        assert row["status"] == "REJECTED"
        assert row["rejection_reason"] is not None

    @pytest.mark.asyncio
    async def test_cancelled_paper_fill_persisted(self, paper_settings, mock_kalshi, tmp_db):
        """Cancelled (missed fill) paper order is saved to database."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (False, order.price)
        order = _make_order(paper=True)

        await router.route_order(order)

        conn = tmp_db._get_conn()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order.id,)).fetchone()
        assert row is not None
        assert row["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_trade_record_logged(self, paper_settings, mock_kalshi, tmp_db):
        """Trade record is created on successful fill."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        router._simulate_slippage = lambda order: (True, order.price)
        order = _make_order(paper=True)

        await router.route_order(order)

        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        trade = trades[0]
        assert trade["order_id"] == order.id
        assert trade["market_id"] == "FED-RATE-CUT-MAY26"
        assert trade["side"] == "BUY"
        assert trade["size"] == 10


class TestThreeGateSafety:
    """Three-gate safety system: live trading gates."""

    @pytest.mark.asyncio
    async def test_gate1_paper_mode_blocks(self, tmp_db, mock_kalshi):
        """Gate 1: paper mode blocks live trading."""
        s = Settings()
        s.trading.mode = "paper"
        s.live_enabled = True
        router = OrderRouter(s, mock_kalshi, tmp_db)
        assert router._live_gates_passed() is False

    @pytest.mark.asyncio
    async def test_gate2_env_var_blocks(self, tmp_db, mock_kalshi):
        """Gate 2: live_enabled=False blocks live trading."""
        s = Settings()
        s.trading.mode = "live"
        s.live_enabled = False
        router = OrderRouter(s, mock_kalshi, tmp_db)
        assert router._live_gates_passed() is False

    @pytest.mark.asyncio
    async def test_gate3_ttl_expiry(self, live_settings, mock_kalshi, tmp_db):
        """Gate 3 confirmation expires after TTL."""
        import time
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        router._session_confirm_time = time.time() - 7200  # 2 hours ago
        router._session_confirm_ttl = 3600  # 1 hour TTL
        order = _make_order(paper=False)

        with patch.object(router, '_request_confirmation', new_callable=AsyncMock, return_value=False):
            result = await router.route_order(order)

        assert result.success is False
        assert "declined" in result.error

    @pytest.mark.asyncio
    async def test_gate3_no_expiry_within_ttl(self, live_settings, mock_kalshi, tmp_db):
        """Gate 3 stays confirmed within TTL window."""
        import time
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        router._session_confirm_time = time.time() - 100  # 100s ago
        router._session_confirm_ttl = 3600  # 1 hour TTL
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        # Should not have requested confirmation
        mock_kalshi.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_gates_required_for_live(self, tmp_db, mock_kalshi):
        """Gate 2 blocks when live_enabled is False even with mode='live'."""
        s = Settings()
        s.trading.mode = "live"
        s.live_enabled = False
        router = OrderRouter(s, mock_kalshi, tmp_db)
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert "gates not passed" in result.error


class TestStaleOrderCancellation:
    """Stale order cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_stale_respects_age(self, paper_settings, mock_kalshi, tmp_db):
        """Only orders older than max_age_seconds are cancelled."""
        from datetime import timedelta
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        now = datetime.now(timezone.utc)
        conn = tmp_db._get_conn()

        # Insert a recent open order (should NOT be cancelled)
        recent_time = (now - timedelta(seconds=60)).isoformat()
        conn.execute("""
            INSERT INTO orders (id, market_id, token_id, side, price, size, cost,
                order_type, fee_rate_bps, status, strategy, paper, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "PE-recent", "MKT-A", "MKT-A_yes", "buy", 0.50, 10, 5.0,
            "GTC", 175, "open", "ai_probability", 1, recent_time,
        ))
        # Insert an old open order (SHOULD be cancelled)
        old_time = (now - timedelta(hours=1)).isoformat()
        conn.execute("""
            INSERT INTO orders (id, market_id, token_id, side, price, size, cost,
                order_type, fee_rate_bps, status, strategy, paper, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "PE-old", "MKT-B", "MKT-B_yes", "buy", 0.50, 10, 5.0,
            "GTC", 175, "open", "ai_probability", 1, old_time,
        ))
        conn.commit()

        cancelled = await router.cancel_stale_orders(max_age_seconds=1800)
        assert cancelled == 1

        # Verify the right one was cancelled
        row_recent = conn.execute("SELECT status FROM orders WHERE id='PE-recent'").fetchone()
        row_old = conn.execute("SELECT status FROM orders WHERE id='PE-old'").fetchone()
        assert row_recent["status"] == "open"
        assert row_old["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_all_open(self, paper_settings, mock_kalshi, tmp_db):
        """cancel_all_open cancels all resting orders."""
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            conn.execute("""
                INSERT INTO orders (id, market_id, token_id, side, price, size, cost,
                    order_type, fee_rate_bps, status, strategy, paper, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"PE-open{i}", "MKT-A", "MKT-A_yes", "buy", 0.50, 10, 5.0,
                "GTC", 175, "open", "ai_probability", 1, now,
            ))
        conn.commit()

        cancelled = await router.cancel_all_open()
        assert cancelled == 3


class TestFillRecording:
    """Fill recording and position manager updates."""

    @pytest.mark.asyncio
    async def test_live_fill_creates_trade_record(self, live_settings, mock_kalshi, tmp_db):
        """Live fill creates a Trade with correct fields."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.trade is not None
        assert result.trade.paper is False
        assert result.trade.order_id == order.id
        assert result.trade.market_id == order.market_id
        assert result.trade.side == Side.BUY
        assert result.trade.size == 10

    @pytest.mark.asyncio
    async def test_live_fill_trade_has_fee(self, live_settings, mock_kalshi, tmp_db):
        """Live Kalshi fills should have a non-zero maker fee."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.trade.fee > 0

    @pytest.mark.asyncio
    async def test_live_resting_order_no_trade(self, live_settings, tmp_db):
        """Resting (unfilled) live order should return no Trade object."""
        kalshi = AsyncMock(spec=KalshiClient)
        kalshi.get_balance = AsyncMock(return_value=1000.0)
        kalshi.create_order = AsyncMock(return_value={"order_id": "k-rest", "status": "resting"})
        # get_order is polled during status check; must return a dict (not AsyncMock)
        # so that .get("status", "").lower() doesn't produce unawaited coroutines.
        kalshi.get_order = AsyncMock(return_value={"order_id": "k-rest", "status": "resting"})
        router = OrderRouter(live_settings, kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        assert result.trade is None
        assert result.order.status == OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_exchange_order_id_stored(self, live_settings, mock_kalshi, tmp_db):
        """Exchange order ID from Kalshi is stored on the order."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.order.exchange_order_id == "kalshi-123"


class TestMissingKalshiSide:
    """Edge case: kalshi_side is None."""

    @pytest.mark.asyncio
    async def test_missing_kalshi_side_rejected(self, live_settings, mock_kalshi, tmp_db):
        """Order with kalshi_side=None is rejected."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)
        order.kalshi_side = None

        result = await router.route_order(order)

        assert result.success is False
        assert "kalshi_side" in result.error.lower()
        mock_kalshi.create_order.assert_not_called()


class TestPriceValidation:
    """Edge case: price converts to invalid Kalshi cents."""

    @pytest.mark.asyncio
    async def test_price_out_of_kalshi_range_rejected(self, live_settings, mock_kalshi, tmp_db):
        """BUY NO at $0.99 creates yes_price=1 cent, which should be valid;
        but $1.00 - $0.99 = $0.01 = 1 cent, which IS valid. Test edge case."""
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        router._session_confirmed = True
        order = _make_order(paper=False)
        order.kalshi_side = "no"
        order.price = 0.99  # yes_price = 100 - 99 = 1 cent

        result = await router.route_order(order)

        # 1 cent is in valid range [1, 99], so should succeed
        assert result.success is True
