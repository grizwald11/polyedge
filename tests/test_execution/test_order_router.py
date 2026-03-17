"""Tests for order router."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    client.create_order = AsyncMock(return_value={"order_id": "kalshi-123", "status": "resting"})
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
    )


class TestPaperFill:
    @pytest.mark.asyncio
    async def test_paper_fill_succeeds(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.status == OrderStatus.FILLED
        assert result.order.fill_price == 0.34
        assert result.order.filled_at is not None
        assert result.trade is not None
        assert result.trade.price == 0.34
        assert result.trade.size == 10
        assert result.trade.paper is True

    @pytest.mark.asyncio
    async def test_paper_fill_logs_trade(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=True)

        await router.route_order(order)

        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "PE-test123"
        assert trades[0]["paper"] == 1

    @pytest.mark.asyncio
    async def test_paper_fill_calculates_fee(self, paper_settings, mock_kalshi, tmp_db):
        router = OrderRouter(paper_settings, mock_kalshi, tmp_db)
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
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is True
        assert result.order.status == OrderStatus.FILLED
        mock_kalshi.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_fill_api_failure(self, live_settings, mock_kalshi, tmp_db):
        mock_kalshi.create_order = AsyncMock(return_value=None)
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert result.order.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_live_fill_api_exception(self, live_settings, mock_kalshi, tmp_db):
        mock_kalshi.create_order = AsyncMock(side_effect=Exception("API down"))
        router = OrderRouter(live_settings, mock_kalshi, tmp_db)
        order = _make_order(paper=False)

        result = await router.route_order(order)

        assert result.success is False
        assert "API down" in result.error


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
