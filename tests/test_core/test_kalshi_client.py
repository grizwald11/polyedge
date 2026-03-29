"""Tests for Kalshi API client — auth signing and request handling."""

import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.core.kalshi_client import KalshiClient


class TestKalshiAuth:
    """Test RSA-PSS authentication signing."""

    def test_full_path_default_host(self):
        client = KalshiClient(host="https://demo-api.kalshi.co/trade-api/v2")
        assert client._full_path("/portfolio/balance") == "/trade-api/v2/portfolio/balance"
        assert client._full_path("/markets") == "/trade-api/v2/markets"

    def test_full_path_trailing_slash(self):
        client = KalshiClient(host="https://demo-api.kalshi.co/trade-api/v2/")
        assert client._full_path("/portfolio/balance") == "/trade-api/v2/portfolio/balance"

    def test_full_path_prod_host(self):
        client = KalshiClient(host="https://trading-api.kalshi.com/trade-api/v2")
        assert client._full_path("/portfolio/balance") == "/trade-api/v2/portfolio/balance"

    def test_timestamp_is_milliseconds(self):
        client = KalshiClient(
            api_key_id="test-key",
            private_key_path="/fake/path",
        )
        # Mock the signing so we can inspect the timestamp
        with patch.object(client, "_sign_request", return_value="fake-sig") as mock_sign:
            with patch.object(client, "_load_private_key", return_value=MagicMock()):
                headers = client._auth_headers("GET", "/portfolio/balance")

        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        now_ms = int(time.time() * 1000)
        # Timestamp should be in milliseconds (13 digits), not seconds (10 digits)
        assert ts > 1_000_000_000_000, "Timestamp should be in milliseconds"
        assert abs(ts - now_ms) < 5000, "Timestamp should be close to current time"

    def test_sign_uses_full_path(self):
        client = KalshiClient(
            host="https://demo-api.kalshi.co/trade-api/v2",
            api_key_id="test-key",
            private_key_path="/fake/path",
        )
        with patch.object(client, "_sign_request", return_value="fake-sig") as mock_sign:
            with patch.object(client, "_load_private_key", return_value=MagicMock()):
                client._auth_headers("GET", "/portfolio/balance")

        # Verify _sign_request was called with the full path
        call_args = mock_sign.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "/trade-api/v2/portfolio/balance"

    def test_auth_headers_structure(self):
        client = KalshiClient(
            api_key_id="my-key-id",
            private_key_path="/fake/path",
        )
        with patch.object(client, "_sign_request", return_value="base64sig"):
            with patch.object(client, "_load_private_key", return_value=MagicMock()):
                headers = client._auth_headers("GET", "/portfolio/balance")

        assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
        assert headers["KALSHI-ACCESS-SIGNATURE"] == "base64sig"
        assert "KALSHI-ACCESS-TIMESTAMP" in headers

    def test_no_auth_without_credentials(self):
        client = KalshiClient()
        headers = client._auth_headers("GET", "/markets")
        assert headers == {}


class TestKalshiRequests:
    """Test request handling and retries."""

    @pytest.mark.asyncio
    async def test_health_check(self):
        client = KalshiClient()
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ok"}
        mock_response.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        client._client = mock_http

        result = await client.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_balance_parses_cents(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"balance": 5000}
            balance = await client.get_balance()
        assert balance == 50.0

    @pytest.mark.asyncio
    async def test_429_exhausts_retries_raises(self):
        """Regression: 429 on all attempts should raise KalshiRateLimitError."""
        import httpx
        from src.core.kalshi_client import KalshiRateLimitError
        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.request = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        client._client = mock_http

        with pytest.raises(KalshiRateLimitError):
            await client._request("GET", "/markets")
        # Should have been called max_retries times (3)
        assert mock_http.get.call_count == 3

    @pytest.mark.asyncio
    async def test_close(self):
        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        await client.close()
        mock_http.aclose.assert_called_once()
