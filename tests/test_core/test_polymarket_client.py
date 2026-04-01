"""Tests for Polymarket CLOB client wrapper."""

from __future__ import annotations

import sys
import asyncio
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Stub out py_clob_client before importing PolymarketClient so that tests
# run even when the real package is not installed in the environment.
# ---------------------------------------------------------------------------

def _make_py_clob_stubs():
    """Build a minimal sys.modules stub tree for py_clob_client."""
    # Top-level package
    pkg = ModuleType("py_clob_client")

    # py_clob_client.client
    client_mod = ModuleType("py_clob_client.client")
    MockClobClient = MagicMock(name="ClobClient")
    client_mod.ClobClient = MockClobClient
    pkg.client = client_mod

    # py_clob_client.clob_types
    types_mod = ModuleType("py_clob_client.clob_types")

    class _OrderType:
        GTC = "GTC"
        FOK = "FOK"

    types_mod.OrderType = _OrderType

    class _OrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id = token_id
            self.price = price
            self.size = size
            self.side = side

    types_mod.OrderArgs = _OrderArgs
    pkg.clob_types = types_mod

    return {
        "py_clob_client": pkg,
        "py_clob_client.client": client_mod,
        "py_clob_client.clob_types": types_mod,
    }


# Register stubs before importing the module under test
_stubs = _make_py_clob_stubs()
for _mod_name, _mod in _stubs.items():
    sys.modules.setdefault(_mod_name, _mod)

# Now we can safely import — the real package is not required
from src.core.polymarket_client import PolymarketClient, _PY_CLOB_AVAILABLE  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """PolymarketClient with internal ClobClient pre-mocked and marked initialized."""
    c = PolymarketClient(
        host="https://clob.polymarket.com",
        private_key="0xtest_key",
        chain_id=137,
        signature_type=1,
    )
    c._client = MagicMock()
    c._initialized = True
    c._disabled = False
    return c


@pytest.fixture
def disabled_client():
    """A client whose _disabled flag is True (simulates missing package or bad init)."""
    c = PolymarketClient(private_key="0xtest_key")
    c._disabled = True
    c._initialized = False
    return c


# ---------------------------------------------------------------------------
# Module-level import guard
# ---------------------------------------------------------------------------

class TestModuleImport:
    def test_py_clob_available_flag(self):
        # With our stubs in place _PY_CLOB_AVAILABLE should be True
        assert _PY_CLOB_AVAILABLE is True


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestPolymarketClientInit:
    def test_default_constructor(self):
        c = PolymarketClient()
        assert c.host == "https://clob.polymarket.com"
        assert c._initialized is False
        assert c._client is None
        assert c._disabled is False  # stubs installed → not disabled

    def test_custom_params(self):
        c = PolymarketClient(
            host="https://custom.host",
            private_key="0xabc",
            chain_id=80001,
            signature_type=0,
        )
        assert c.host == "https://custom.host"
        assert c._chain_id == 80001
        assert c._signature_type == 0

    def test_is_available_before_init(self):
        c = PolymarketClient(private_key="0xtest")
        assert c.is_available is False

    def test_is_available_after_init(self, client):
        assert client.is_available is True

    def test_is_available_when_disabled(self, disabled_client):
        assert disabled_client.is_available is False

    @pytest.mark.asyncio
    async def test_run_raises_when_disabled(self, disabled_client):
        with pytest.raises(RuntimeError, match="disabled"):
            await disabled_client._run(lambda: None)

    @pytest.mark.asyncio
    async def test_run_raises_before_init(self):
        c = PolymarketClient(private_key="0xtest")
        c._disabled = False
        c._initialized = False
        with pytest.raises(RuntimeError, match="not initialized"):
            await c._run(lambda: None)

    @pytest.mark.asyncio
    async def test_run_executes_callable(self, client):
        result = await client._run(lambda: 42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_run_passes_args_and_kwargs(self, client):
        def add(a, b, *, multiplier=1):
            return (a + b) * multiplier

        result = await client._run(add, 3, 4, multiplier=2)
        assert result == 14


# ---------------------------------------------------------------------------
# initialize() / _do_initialize()
# ---------------------------------------------------------------------------

class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self, client):
        """Calling initialize() a second time should be a no-op."""
        assert client._initialized is True
        await client.initialize()  # second call — should not re-run
        assert client._initialized is True

    @pytest.mark.asyncio
    async def test_do_initialize_skips_when_disabled(self):
        c = PolymarketClient()
        c._disabled = True
        await c._do_initialize()
        assert c._initialized is False

    @pytest.mark.asyncio
    async def test_do_initialize_disables_on_empty_key(self):
        c = PolymarketClient(private_key="")
        c._disabled = False
        await c._do_initialize()
        assert c._disabled is True
        assert c._initialized is False

    @pytest.mark.asyncio
    async def test_do_initialize_success(self):
        """Happy path: ClobClient is constructed and creds are set."""
        c = PolymarketClient(private_key="0xvalidkey")
        c._disabled = False

        mock_clob = MagicMock()
        mock_creds = MagicMock()
        mock_clob.create_or_derive_api_creds.return_value = mock_creds

        with patch("src.core.polymarket_client.ClobClient", return_value=mock_clob):
            await c._do_initialize()

        assert c._initialized is True
        assert c._disabled is False
        mock_clob.create_or_derive_api_creds.assert_called_once()
        mock_clob.set_api_creds.assert_called_once_with(mock_creds)

    @pytest.mark.asyncio
    async def test_do_initialize_disables_on_exception(self):
        """If ClobClient constructor raises, the client should self-disable."""
        c = PolymarketClient(private_key="0xbadkey")
        c._disabled = False

        with patch("src.core.polymarket_client.ClobClient", side_effect=RuntimeError("bad key")):
            await c._do_initialize()

        assert c._disabled is True
        assert c._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_acquires_lock_once(self):
        """Concurrent calls to initialize() should only run _do_initialize once."""
        c = PolymarketClient(private_key="0xkey")
        c._disabled = False

        mock_clob = MagicMock()
        mock_clob.create_or_derive_api_creds.return_value = MagicMock()

        with patch("src.core.polymarket_client.ClobClient", return_value=mock_clob):
            await asyncio.gather(c.initialize(), c.initialize(), c.initialize())

        # _do_initialize sets _initialized=True; ClobClient should be constructed once
        assert c._initialized is True
        assert mock_clob.create_or_derive_api_creds.call_count == 1


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_true_when_ok(self, client):
        client._client.get_ok.return_value = "OK"
        assert await client.health_check() is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_ok(self, client):
        client._client.get_ok.return_value = "FAIL"
        assert await client.health_check() is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self, client):
        client._client.get_ok.side_effect = ConnectionError("timeout")
        assert await client.health_check() is False

    @pytest.mark.asyncio
    async def test_returns_false_when_disabled(self, disabled_client):
        assert await disabled_client.health_check() is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_client(self):
        c = PolymarketClient()
        c._disabled = False
        c._client = None
        assert await c.health_check() is False


# ---------------------------------------------------------------------------
# get_server_time()
# ---------------------------------------------------------------------------

class TestGetServerTime:
    @pytest.mark.asyncio
    async def test_returns_server_time(self, client):
        client._client.get_server_time.return_value = "2026-04-01T12:00:00Z"
        result = await client.get_server_time()
        assert result == "2026-04-01T12:00:00Z"


# ---------------------------------------------------------------------------
# get_balance()
# ---------------------------------------------------------------------------

class TestGetBalance:
    @pytest.mark.asyncio
    async def test_standard_usdc_balance(self, client):
        # 500 USDC = 500_000_000 atomic units (6 decimals)
        client._client.get_balance_allowance.return_value = {"balance": 500_000_000}
        assert await client.get_balance() == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_small_balance(self, client):
        # $1.50 = 1_500_000 atomic units
        client._client.get_balance_allowance.return_value = {"balance": 1_500_000}
        assert await client.get_balance() == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_zero_balance(self, client):
        client._client.get_balance_allowance.return_value = {"balance": 0}
        assert await client.get_balance() == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_missing_balance_key(self, client):
        client._client.get_balance_allowance.return_value = {}
        assert await client.get_balance() == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_negative_balance_clamped_to_zero(self, client):
        # API returning a negative value should be clamped
        client._client.get_balance_allowance.return_value = {"balance": -100}
        assert await client.get_balance() == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_large_balance(self, client):
        # $10,000 = 10_000_000_000 atomic units
        client._client.get_balance_allowance.return_value = {"balance": 10_000_000_000}
        assert await client.get_balance() == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# get_order_book()
# ---------------------------------------------------------------------------

class TestGetOrderBook:
    @pytest.mark.asyncio
    async def test_returns_order_book(self, client):
        expected = {"bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": "0.52", "size": "200"}]}
        client._client.get_order_book.return_value = expected
        result = await client.get_order_book("tok_123")
        assert result == expected
        client._client.get_order_book.assert_called_once_with("tok_123")

    @pytest.mark.asyncio
    async def test_passes_token_id(self, client):
        client._client.get_order_book.return_value = {}
        await client.get_order_book("token_abc_456")
        client._client.get_order_book.assert_called_once_with("token_abc_456")


# ---------------------------------------------------------------------------
# get_midpoint()
# ---------------------------------------------------------------------------

class TestGetMidpoint:
    @pytest.mark.asyncio
    async def test_dict_response(self, client):
        client._client.get_midpoint.return_value = {"mid": "0.55"}
        assert await client.get_midpoint("tok_123") == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_string_response(self, client):
        client._client.get_midpoint.return_value = "0.60"
        assert await client.get_midpoint("tok_123") == pytest.approx(0.60)

    @pytest.mark.asyncio
    async def test_float_response(self, client):
        client._client.get_midpoint.return_value = 0.75
        assert await client.get_midpoint("tok_123") == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_type_error_returns_zero(self, client):
        client._client.get_midpoint.return_value = None
        assert await client.get_midpoint("tok_123") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_key_error_returns_zero(self, client):
        # dict without "mid" key — float(result.get("mid", 0.0)) → 0.0
        client._client.get_midpoint.return_value = {"other_key": "0.5"}
        assert await client.get_midpoint("tok_123") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# get_price()
# ---------------------------------------------------------------------------

class TestGetPrice:
    @pytest.mark.asyncio
    async def test_dict_response_buy(self, client):
        client._client.get_price.return_value = {"price": "0.45"}
        result = await client.get_price("tok_123", "buy")
        assert result == pytest.approx(0.45)
        client._client.get_price.assert_called_once_with("tok_123", "buy")

    @pytest.mark.asyncio
    async def test_dict_response_sell(self, client):
        client._client.get_price.return_value = {"price": "0.48"}
        result = await client.get_price("tok_123", "sell")
        assert result == pytest.approx(0.48)

    @pytest.mark.asyncio
    async def test_string_response(self, client):
        client._client.get_price.return_value = "0.52"
        assert await client.get_price("tok_123", "buy") == pytest.approx(0.52)

    @pytest.mark.asyncio
    async def test_default_side_is_buy(self, client):
        client._client.get_price.return_value = {"price": "0.50"}
        await client.get_price("tok_123")
        client._client.get_price.assert_called_once_with("tok_123", "buy")

    @pytest.mark.asyncio
    async def test_type_error_returns_zero(self, client):
        client._client.get_price.return_value = None
        assert await client.get_price("tok_123") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_missing_price_key_returns_zero(self, client):
        client._client.get_price.return_value = {}
        assert await client.get_price("tok_123") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# create_and_post_order()
# ---------------------------------------------------------------------------

class TestCreateAndPostOrder:
    @pytest.mark.asyncio
    async def test_gtc_order_success(self, client):
        signed = MagicMock(name="signed_order")
        client._client.create_order.return_value = signed
        client._client.post_order.return_value = {"id": "order_001", "status": "LIVE"}

        result = await client.create_and_post_order(
            token_id="tok_abc",
            side="BUY",
            price=0.65,
            size=10.0,
            order_type="GTC",
        )

        assert result["id"] == "order_001"
        client._client.post_order.assert_called_once_with(signed)

    @pytest.mark.asyncio
    async def test_fok_order_uses_fok_type(self, client):
        from src.core.polymarket_client import PolyOrderType

        signed = MagicMock(name="signed_order")
        client._client.create_order.return_value = signed
        client._client.post_order.return_value = {"id": "order_002", "status": "MATCHED"}

        result = await client.create_and_post_order(
            token_id="tok_xyz",
            side="SELL",
            price=0.30,
            size=5.0,
            order_type="FOK",
        )

        assert result["status"] == "MATCHED"

    @pytest.mark.asyncio
    async def test_order_args_constructed_correctly(self, client):
        """Verify OrderArgs receives the right values."""
        from src.core.polymarket_client import OrderArgs

        captured_args = []

        def capture_create(order_args, order_type):
            captured_args.append(order_args)
            return MagicMock()

        client._client.create_order.side_effect = capture_create
        client._client.post_order.return_value = {}

        await client.create_and_post_order(
            token_id="tok_test",
            side="BUY",
            price=0.72,
            size=15.0,
        )

        assert len(captured_args) == 1
        args = captured_args[0]
        assert args.token_id == "tok_test"
        assert args.price == 0.72
        assert args.size == 15.0
        assert args.side == "BUY"

    @pytest.mark.asyncio
    async def test_default_order_type_is_gtc(self, client):
        """When order_type is not provided it should default to GTC."""
        from src.core.polymarket_client import PolyOrderType

        called_with_type = []

        def capture(order_args, order_type):
            called_with_type.append(order_type)
            return MagicMock()

        client._client.create_order.side_effect = capture
        client._client.post_order.return_value = {}

        await client.create_and_post_order(token_id="tok_t", side="BUY", price=0.5, size=1.0)

        assert called_with_type[0] == PolyOrderType.GTC


# ---------------------------------------------------------------------------
# cancel_order()
# ---------------------------------------------------------------------------

class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_success(self, client):
        client._client.cancel.return_value = {"success": True}
        result = await client.cancel_order("order_abc")
        assert result == {"success": True}
        client._client.cancel.assert_called_once_with("order_abc")

    @pytest.mark.asyncio
    async def test_cancel_passes_order_id(self, client):
        client._client.cancel.return_value = {}
        await client.cancel_order("ORDER-XYZ-999")
        client._client.cancel.assert_called_once_with("ORDER-XYZ-999")


# ---------------------------------------------------------------------------
# get_order()
# ---------------------------------------------------------------------------

class TestGetOrder:
    @pytest.mark.asyncio
    async def test_returns_order_dict(self, client):
        client._client.get_order.return_value = {"id": "order_abc", "status": "LIVE"}
        result = await client.get_order("order_abc")
        assert result["id"] == "order_abc"
        assert result["status"] == "LIVE"

    @pytest.mark.asyncio
    async def test_passes_order_id(self, client):
        client._client.get_order.return_value = {}
        await client.get_order("ORDER-123")
        client._client.get_order.assert_called_once_with("ORDER-123")


# ---------------------------------------------------------------------------
# get_orders()
# ---------------------------------------------------------------------------

class TestGetOrders:
    @pytest.mark.asyncio
    async def test_returns_list_directly(self, client):
        client._client.get_orders.return_value = [{"id": "o1"}, {"id": "o2"}]
        result = await client.get_orders()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_list_from_dict(self, client):
        client._client.get_orders.return_value = {"orders": [{"id": "o1"}]}
        result = await client.get_orders()
        assert len(result) == 1
        assert result[0]["id"] == "o1"

    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        client._client.get_orders.return_value = []
        result = await client.get_orders()
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_dict_fallback(self, client):
        client._client.get_orders.return_value = {}
        result = await client.get_orders()
        assert result == []


# ---------------------------------------------------------------------------
# get_trades()
# ---------------------------------------------------------------------------

class TestGetTrades:
    @pytest.mark.asyncio
    async def test_returns_list_directly(self, client):
        client._client.get_trades.return_value = [{"id": "t1"}, {"id": "t2"}]
        result = await client.get_trades()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_trades_from_dict(self, client):
        client._client.get_trades.return_value = {"trades": [{"id": "t1"}]}
        result = await client.get_trades()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        client._client.get_trades.return_value = []
        result = await client.get_trades()
        assert result == []


# ---------------------------------------------------------------------------
# is_available property
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_true_when_initialized_and_not_disabled(self, client):
        assert client.is_available is True

    def test_false_when_disabled(self, disabled_client):
        assert disabled_client.is_available is False

    def test_false_when_initialized_but_disabled(self):
        c = PolymarketClient(private_key="0xkey")
        c._initialized = True
        c._disabled = True
        assert c.is_available is False

    def test_false_when_not_initialized_and_not_disabled(self):
        c = PolymarketClient(private_key="0xkey")
        c._initialized = False
        c._disabled = False
        assert c.is_available is False
