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

    def test_duplicate_registration_replaces(self, ws_client):
        cb = AsyncMock()
        ws_client.on_price_update(cb)
        ws_client.on_price_update(cb)
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
