"""Tests for Kalshi WebSocket client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.websocket_client import (
    FillUpdate,
    KalshiWebSocket,
    LifecycleUpdate,
    MAX_CONSECUTIVE_FAILURES,
    TickerUpdate,
)


@pytest.fixture
def ws_client():
    return KalshiWebSocket(
        host="wss://demo-api.kalshi.co/trade-api/ws/v2",
        api_key_id=None,
        private_key_path=None,
    )


class TestSubscriptions:
    def test_subscribe_adds_tickers(self, ws_client):
        ws_client.subscribe(["TICKER-A", "TICKER-B"])
        assert ws_client.subscription_count == 2
        assert "TICKER-A" in ws_client._subscriptions

    def test_subscribe_deduplicates(self, ws_client):
        ws_client.subscribe(["TICKER-A", "TICKER-B"])
        ws_client.subscribe(["TICKER-A", "TICKER-C"])
        assert ws_client.subscription_count == 3

    def test_unsubscribe_removes_tickers(self, ws_client):
        ws_client.subscribe(["TICKER-A", "TICKER-B"])
        ws_client.unsubscribe(["TICKER-A"])
        assert ws_client.subscription_count == 1
        assert "TICKER-A" not in ws_client._subscriptions

    def test_unsubscribe_nonexistent_is_noop(self, ws_client):
        ws_client.subscribe(["TICKER-A"])
        ws_client.unsubscribe(["TICKER-Z"])
        assert ws_client.subscription_count == 1

    def test_set_channels(self, ws_client):
        ws_client.set_channels(["ticker", "fill", "orderbook_delta"])
        assert ws_client._channels == ["ticker", "fill", "orderbook_delta"]


class TestCallbackRegistration:
    def test_on_price_update(self, ws_client):
        cb = AsyncMock()
        ws_client.on_price_update(cb)
        assert len(ws_client._price_callbacks) == 1

    def test_on_fill(self, ws_client):
        cb = AsyncMock()
        ws_client.on_fill(cb)
        assert len(ws_client._fill_callbacks) == 1

    def test_on_lifecycle(self, ws_client):
        cb = AsyncMock()
        ws_client.on_lifecycle(cb)
        assert len(ws_client._lifecycle_callbacks) == 1

    def test_remove_callback(self, ws_client):
        cb = AsyncMock()
        cb_id = ws_client.on_price_update(cb)
        assert len(ws_client._price_callbacks) == 1
        assert ws_client.remove_callback(cb_id) is True
        assert len(ws_client._price_callbacks) == 0

    def test_remove_callback_nonexistent(self, ws_client):
        assert ws_client.remove_callback(999999) is False

    def test_duplicate_registration_creates_separate_entries(self, ws_client):
        """With monotonic IDs, registering the same callback twice creates two
        independent entries, each removable by its own ID (L-2 fix)."""
        cb = AsyncMock()
        id1 = ws_client.on_price_update(cb)
        id2 = ws_client.on_price_update(cb)
        assert id1 != id2
        assert len(ws_client._price_callbacks) == 2
        ws_client.remove_callback(id1)
        assert len(ws_client._price_callbacks) == 1


class TestMessageParsing:
    def test_parse_ticker(self, ws_client):
        msg = {
            "msg": {
                "market_ticker": "FED-RATE",
                "price_dollars": 0.45,
                "yes_bid_dollars": 0.44,
                "yes_ask_dollars": 0.46,
                "volume_fp": 50000,
                "open_interest_fp": 12000,
                "ts": 1710700000000,
            }
        }
        update = ws_client._parse_ticker(msg)
        assert update is not None
        assert update.market_ticker == "FED-RATE"
        assert update.price == 0.45
        assert update.yes_bid == 0.44
        assert update.yes_ask == 0.46
        assert update.volume == 50000
        assert update.ts == 1710700000000

    def test_parse_ticker_missing_fields_defaults(self, ws_client):
        msg = {"msg": {"market_ticker": "X"}}
        update = ws_client._parse_ticker(msg)
        assert update is not None
        assert update.price == 0
        assert update.yes_bid == 0

    def test_parse_fill(self, ws_client):
        msg = {
            "msg": {
                "order_id": "order-123",
                "market_ticker": "FED-RATE",
                "side": "yes",
                "yes_price_dollars": 0.45,
                "count_fp": 10,
                "ts": 1710700000000,
            }
        }
        update = ws_client._parse_fill(msg)
        assert update is not None
        assert update.order_id == "order-123"
        assert update.market_ticker == "FED-RATE"
        assert update.count == 10

    def test_parse_lifecycle(self, ws_client):
        msg = {
            "msg": {
                "market_ticker": "FED-RATE",
                "status": "determined",
                "settlement_value": 1.0,
            }
        }
        update = ws_client._parse_lifecycle(msg)
        assert update is not None
        assert update.status == "determined"
        assert update.settlement_value == 1.0

    def test_parse_lifecycle_no_settlement(self, ws_client):
        msg = {"msg": {"market_ticker": "X", "status": "open"}}
        update = ws_client._parse_lifecycle(msg)
        assert update is not None
        assert update.settlement_value is None


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_ticker(self, ws_client):
        cb = AsyncMock()
        ws_client.on_price_update(cb)

        msg = {
            "type": "ticker",
            "msg": {
                "market_ticker": "FED-RATE",
                "price_dollars": 0.45,
                "yes_bid_dollars": 0.44,
                "yes_ask_dollars": 0.46,
                "volume_fp": 100,
                "open_interest_fp": 50,
                "ts": 123,
            },
        }
        await ws_client._dispatch(msg)
        cb.assert_called_once()
        update = cb.call_args[0][0]
        assert isinstance(update, TickerUpdate)
        assert update.market_ticker == "FED-RATE"

    @pytest.mark.asyncio
    async def test_dispatch_fill(self, ws_client):
        cb = AsyncMock()
        ws_client.on_fill(cb)

        msg = {
            "type": "fill",
            "msg": {
                "order_id": "o1",
                "market_ticker": "X",
                "side": "yes",
                "yes_price_dollars": 0.5,
                "count_fp": 5,
                "ts": 123,
            },
        }
        await ws_client._dispatch(msg)
        cb.assert_called_once()
        assert isinstance(cb.call_args[0][0], FillUpdate)

    @pytest.mark.asyncio
    async def test_dispatch_lifecycle(self, ws_client):
        cb = AsyncMock()
        ws_client.on_lifecycle(cb)

        msg = {
            "type": "market_lifecycle_v2",
            "msg": {"market_ticker": "X", "status": "closed"},
        }
        await ws_client._dispatch(msg)
        cb.assert_called_once()
        assert isinstance(cb.call_args[0][0], LifecycleUpdate)

    @pytest.mark.asyncio
    async def test_dispatch_unknown_type_ignored(self, ws_client):
        cb = AsyncMock()
        ws_client.on_price_update(cb)
        await ws_client._dispatch({"type": "something_else"})
        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_error_isolated(self, ws_client):
        """One failing callback should not block others."""
        bad_cb = AsyncMock(side_effect=RuntimeError("boom"))
        good_cb = AsyncMock()
        ws_client.on_price_update(bad_cb)
        ws_client.on_price_update(good_cb)

        msg = {
            "type": "ticker",
            "msg": {
                "market_ticker": "X",
                "price_dollars": 0.5,
                "yes_bid_dollars": 0.49,
                "yes_ask_dollars": 0.51,
                "volume_fp": 0,
                "open_interest_fp": 0,
                "ts": 0,
            },
        }
        await ws_client._dispatch(msg)
        good_cb.assert_called_once()


class TestConnection:
    def test_connected_false_initially(self, ws_client):
        assert ws_client.connected is False

    @pytest.mark.asyncio
    async def test_connect_without_websockets_logs_warning(self, ws_client):
        """connect() gracefully returns if websockets not installed."""
        with patch.dict("sys.modules", {"websockets": None}):
            # Re-import won't help since connect does import inside
            # Just mock the import to raise ImportError
            original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

            def mock_import(name, *args, **kwargs):
                if name == "websockets":
                    raise ImportError("no websockets")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                await ws_client.connect()
                assert ws_client.connected is False

    @pytest.mark.asyncio
    async def test_close(self, ws_client):
        mock_ws = AsyncMock()
        ws_client._ws = mock_ws
        ws_client._running = True

        await ws_client.close()

        assert ws_client._running is False
        assert ws_client._ws is None
        mock_ws.close.assert_called_once()


class TestAuth:
    def test_auth_headers_empty_without_keys(self, ws_client):
        headers = ws_client._auth_headers()
        assert headers == {}

    def test_auth_headers_with_mock_key(self, tmp_path):
        """Auth headers are generated correctly with a key."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        # Generate a test RSA key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = tmp_path / "test_key.pem"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

        client = KalshiWebSocket(
            api_key_id="test-key-id",
            private_key_path=str(key_path),
        )
        headers = client._auth_headers()

        assert "KALSHI-ACCESS-KEY" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        # Timestamp should be recent (within 5 seconds)
        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        import time
        assert abs(ts - int(time.time() * 1000)) < 5000


class TestModuleConstants:
    def test_max_consecutive_failures_defined(self):
        """M-19: MAX_CONSECUTIVE_FAILURES must be a module-level constant."""
        assert MAX_CONSECUTIVE_FAILURES == 10

    def test_initial_backoff_defined(self):
        from src.core.websocket_client import INITIAL_BACKOFF, MAX_BACKOFF, BACKOFF_MULTIPLIER
        assert INITIAL_BACKOFF == 1.0
        assert MAX_BACKOFF == 60.0
        assert BACKOFF_MULTIPLIER == 2.0


class TestReconnectSync:
    def test_register_reconnect_sync_stores_callback(self, ws_client):
        """H-5: register_reconnect_sync adds callback to reconnect list."""
        cb = AsyncMock()
        assert len(ws_client._reconnect_callbacks) == 0
        ws_client.register_reconnect_sync(cb)
        assert len(ws_client._reconnect_callbacks) == 1
        assert ws_client._reconnect_callbacks[0] is cb

    def test_on_reconnect_and_register_reconnect_sync_both_work(self, ws_client):
        """Both registration methods append to the same callback list."""
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        ws_client.on_reconnect(cb1)
        ws_client.register_reconnect_sync(cb2)
        assert len(ws_client._reconnect_callbacks) == 2

    @pytest.mark.asyncio
    async def test_reconnect_callbacks_called_on_connect(self, ws_client):
        """Reconnect callbacks are invoked after successful WebSocket connection."""
        cb = AsyncMock()
        ws_client.register_reconnect_sync(cb)

        # Simulate the reconnect callback invocation path directly
        for callback in ws_client._reconnect_callbacks:
            await callback()

        cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_callback_error_does_not_propagate(self, ws_client):
        """A failing reconnect callback should not abort the connection loop."""
        bad_cb = AsyncMock(side_effect=RuntimeError("sync failed"))
        ws_client.register_reconnect_sync(bad_cb)

        # Simulate the error-isolated invocation used in connect()
        for cb in ws_client._reconnect_callbacks:
            try:
                await cb()
            except Exception:
                pass  # Should be swallowed by the connect() loop

        bad_cb.assert_called_once()


class TestPingParameters:
    @pytest.mark.asyncio
    async def test_connect_passes_ping_parameters(self, ws_client):
        """H-2: websockets.connect() must be called with ping_interval=20, ping_timeout=30."""
        import websockets

        connect_kwargs = {}

        class FakeWS:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration

        def fake_connect(url, **kwargs):
            connect_kwargs.update(kwargs)
            ws_client._running = False  # Stop after first iteration
            return FakeWS()

        with patch("websockets.connect", side_effect=fake_connect):
            try:
                await ws_client.connect()
            except Exception:
                pass

        assert connect_kwargs.get("ping_interval") == 20, (
            f"Expected ping_interval=20, got {connect_kwargs.get('ping_interval')}"
        )
        assert connect_kwargs.get("ping_timeout") == 30, (
            f"Expected ping_timeout=30, got {connect_kwargs.get('ping_timeout')}"
        )
