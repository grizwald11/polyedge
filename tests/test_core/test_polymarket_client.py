"""Tests for Polymarket CLOB client wrapper."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# py-clob-client is optional — skip all tests if not installed
pytest.importorskip("py_clob_client", reason="py-clob-client not installed")

from src.core.polymarket_client import PolymarketClient


@pytest.fixture
def client():
    """Create a PolymarketClient with mocked ClobClient."""
    c = PolymarketClient(
        host="https://clob.polymarket.com",
        private_key="0xtest_key",
        chain_id=137,
        signature_type=1,
    )
    # Mock the internal client
    c._client = MagicMock()
    c._initialized = True
    return c


class TestPolymarketClientInit:
    def test_not_initialized_by_default(self):
        c = PolymarketClient()
        assert c._initialized is False
        assert c._client is None

    @pytest.mark.asyncio
    async def test_run_raises_before_init(self):
        c = PolymarketClient()
        with pytest.raises(RuntimeError, match="not initialized"):
            await c._run(lambda: None)


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, client):
        client._client.get_ok.return_value = "OK"
        result = await client.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_unhealthy(self, client):
        client._client.get_ok.return_value = "NOT OK"
        result = await client.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self, client):
        client._client.get_ok.side_effect = ConnectionError("down")
        result = await client.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_no_client_returns_false(self):
        c = PolymarketClient()
        result = await c.health_check()
        assert result is False


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_balance_small_value(self, client):
        # Balance is in USDC atomic units (6 decimals): 500 USDC = 500_000_000
        client._client.get_balance_allowance.return_value = {"balance": 500_000_000}
        balance = await client.get_balance()
        assert balance == 500.0

    @pytest.mark.asyncio
    async def test_balance_wei_format(self, client):
        # Large values get divided by 1e6 (USDC has 6 decimals)
        client._client.get_balance_allowance.return_value = {"balance": 500_000_000}
        balance = await client.get_balance()
        assert balance == 500.0


class TestOrderBook:
    @pytest.mark.asyncio
    async def test_get_order_book(self, client):
        expected = {"bids": [{"price": "0.50"}], "asks": [{"price": "0.52"}]}
        client._client.get_order_book.return_value = expected
        result = await client.get_order_book("tok_123")
        assert result == expected
        client._client.get_order_book.assert_called_once_with("tok_123")


class TestPricing:
    @pytest.mark.asyncio
    async def test_get_midpoint_dict(self, client):
        client._client.get_midpoint.return_value = {"mid": "0.55"}
        result = await client.get_midpoint("tok_123")
        assert result == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_get_midpoint_string(self, client):
        client._client.get_midpoint.return_value = "0.60"
        result = await client.get_midpoint("tok_123")
        assert result == pytest.approx(0.60)

    @pytest.mark.asyncio
    async def test_get_price(self, client):
        client._client.get_price.return_value = {"price": "0.45"}
        result = await client.get_price("tok_123", "buy")
        assert result == pytest.approx(0.45)


class TestOrderOperations:
    @pytest.mark.asyncio
    async def test_cancel_order(self, client):
        client._client.cancel.return_value = {"success": True}
        result = await client.cancel_order("order_abc")
        assert result == {"success": True}
        client._client.cancel.assert_called_once_with("order_abc")

    @pytest.mark.asyncio
    async def test_get_order(self, client):
        client._client.get_order.return_value = {"id": "order_abc", "status": "LIVE"}
        result = await client.get_order("order_abc")
        assert result["status"] == "LIVE"

    @pytest.mark.asyncio
    async def test_get_orders_list(self, client):
        client._client.get_orders.return_value = [{"id": "o1"}, {"id": "o2"}]
        result = await client.get_orders()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_orders_dict(self, client):
        client._client.get_orders.return_value = {"orders": [{"id": "o1"}]}
        result = await client.get_orders()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_trades(self, client):
        client._client.get_trades.return_value = [{"id": "t1"}]
        result = await client.get_trades()
        assert len(result) == 1
