"""Tests for Kalshi API client — auth signing and request handling."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.kalshi_client import KalshiClient, KalshiRateLimitError, TokenBucket


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


class TestRetryAfterHeader:
    """H-16: 429 responses should respect the Retry-After header."""

    @pytest.mark.asyncio
    async def test_429_uses_retry_after_header_when_present(self):
        """When Retry-After is present, wait that many seconds instead of backoff."""
        import httpx

        from src.core.kalshi_client import KalshiRateLimitError

        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = False

        # First two calls return 429 with Retry-After=5, third succeeds
        rate_limited_response = MagicMock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "5"}
        rate_limited_response.request = MagicMock()

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"data": "ok"}
        success_response.raise_for_status = MagicMock()
        success_response.headers = {}

        mock_http.get = AsyncMock(side_effect=[rate_limited_response, rate_limited_response, success_response])
        client._client = mock_http

        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            result = await client._request("GET", "/markets")

        assert result == {"data": "ok"}
        # Both retry sleeps should use Retry-After=5 not backoff formula
        assert len(sleep_calls) == 2
        assert all(s == 5.0 for s in sleep_calls), f"Expected sleep=5.0 from Retry-After, got {sleep_calls}"

    @pytest.mark.asyncio
    async def test_429_falls_back_to_backoff_without_retry_after(self):
        """When Retry-After header is absent, use formula-based backoff."""
        import httpx

        from src.core.kalshi_client import KalshiRateLimitError

        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = False

        rate_limited_response = MagicMock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {}  # No Retry-After
        rate_limited_response.request = MagicMock()

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"ok": True}
        success_response.raise_for_status = MagicMock()
        success_response.headers = {}

        mock_http.get = AsyncMock(side_effect=[rate_limited_response, success_response])
        client._client = mock_http

        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            result = await client._request("GET", "/markets")

        assert result == {"ok": True}
        assert len(sleep_calls) == 1
        # Backoff formula: min(10, 2**(0+1)) + jitter → at least 2.0
        assert sleep_calls[0] >= 2.0

    @pytest.mark.asyncio
    async def test_429_uses_backoff_when_retry_after_invalid(self):
        """When Retry-After header is unparseable, fall back to formula backoff."""
        from src.core.kalshi_client import KalshiRateLimitError

        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = False

        rate_limited_response = MagicMock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "not-a-number"}
        rate_limited_response.request = MagicMock()

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"ok": True}
        success_response.raise_for_status = MagicMock()
        success_response.headers = {}

        mock_http.get = AsyncMock(side_effect=[rate_limited_response, success_response])
        client._client = mock_http

        sleep_calls = []
        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            result = await client._request("GET", "/markets")

        assert result == {"ok": True}
        assert len(sleep_calls) == 1
        # Should use formula backoff, not crash
        assert sleep_calls[0] >= 2.0


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Unit tests for the proactive rate limiter."""

    @pytest.mark.asyncio
    async def test_token_available_immediately(self):
        """Bucket starts full — first acquire should not sleep."""
        bucket = TokenBucket(rate=8.0, capacity=10.0)
        sleep_calls = []

        async def fake_sleep(s):
            sleep_calls.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await bucket.acquire()

        assert sleep_calls == [], "Should not sleep when tokens are available"

    @pytest.mark.asyncio
    async def test_depleted_bucket_sleeps(self):
        """After capacity is exhausted, acquire waits for refill."""
        bucket = TokenBucket(rate=8.0, capacity=2.0)
        # Drain the bucket entirely.
        bucket._tokens = 0.0

        sleep_calls = []

        async def fake_sleep(s):
            # Simulate time passing so the second acquire loop doesn't spin.
            bucket._tokens += s * bucket._rate
            sleep_calls.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await bucket.acquire()

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] > 0


# ---------------------------------------------------------------------------
# _load_private_key
# ---------------------------------------------------------------------------


class TestLoadPrivateKey:
    def test_returns_none_without_path(self):
        client = KalshiClient()
        result = client._load_private_key()
        assert result is None

    def test_returns_cached_key(self):
        client = KalshiClient(private_key_path="/some/path")
        fake_key = MagicMock()
        client._private_key = fake_key
        result = client._load_private_key()
        assert result is fake_key

    def test_key_load_attempted_returns_none(self):
        """If a previous load attempt failed, subsequent calls return None immediately."""
        client = KalshiClient(private_key_path="/some/path")
        client._key_load_attempted = True
        result = client._load_private_key()
        assert result is None

    def test_loads_key_via_key_loader(self):
        client = KalshiClient(private_key_path="/fake/key.pem")
        fake_key = MagicMock()

        stat_result = MagicMock()
        stat_result.st_mtime = 1234567.0

        with patch("src.core.key_loader.load_rsa_private_key", return_value=fake_key):
            with patch("os.stat", return_value=stat_result):
                result = client._load_private_key()

        assert result is fake_key
        assert client._key_load_mtime == 1234567.0


# ---------------------------------------------------------------------------
# check_key_freshness
# ---------------------------------------------------------------------------


class TestCheckKeyFreshness:
    def test_returns_true_when_no_key_loaded(self):
        client = KalshiClient()
        assert client.check_key_freshness() is True

    def test_returns_true_when_no_path(self):
        client = KalshiClient()
        client._private_key = MagicMock()
        # No private_key_path set
        assert client.check_key_freshness() is True

    def test_returns_true_when_mtime_unchanged(self):
        client = KalshiClient(private_key_path="/fake/key.pem")
        client._private_key = MagicMock()
        client._key_load_mtime = 100.0

        stat_result = MagicMock()
        stat_result.st_mtime = 100.0

        with patch("os.stat", return_value=stat_result):
            result = client.check_key_freshness()

        assert result is True

    def test_returns_false_and_clears_key_when_mtime_changed(self):
        client = KalshiClient(private_key_path="/fake/key.pem")
        client._private_key = MagicMock()
        client._key_load_mtime = 100.0

        stat_result = MagicMock()
        stat_result.st_mtime = 200.0

        with patch("os.stat", return_value=stat_result):
            result = client.check_key_freshness()

        assert result is False
        assert client._private_key is None
        assert client._key_load_attempted is False

    def test_returns_true_on_os_error(self):
        client = KalshiClient(private_key_path="/fake/key.pem")
        client._private_key = MagicMock()
        client._key_load_mtime = 100.0

        with patch("os.stat", side_effect=OSError("no such file")):
            result = client.check_key_freshness()

        assert result is True


# ---------------------------------------------------------------------------
# _request — circuit breaker
# ---------------------------------------------------------------------------


def _make_resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    if body is not None:
        resp.json.return_value = body
    if status < 400:
        resp.raise_for_status = MagicMock()
    else:
        def _raise():
            raise httpx.HTTPStatusError(
                f"HTTP {status}",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(status),
            )
        resp.raise_for_status = _raise
    return resp


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_opens_after_max_5xx(self):
        """After MAX_CONSECUTIVE_5XX server errors, circuit opens and blocks further calls."""
        from src.core.kalshi_client import MAX_CONSECUTIVE_5XX

        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        server_error = _make_resp(503)
        mock_http.get = AsyncMock(return_value=server_error)
        client._client = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Exhaust enough retries to accumulate 5 consecutive 5xx errors.
            for _ in range(MAX_CONSECUTIVE_5XX):
                try:
                    await client._request("GET", "/markets")
                except httpx.HTTPStatusError:
                    pass

        assert client._circuit_open_until > 0

    @pytest.mark.asyncio
    async def test_circuit_open_raises_immediately(self):
        """When circuit is open, _request raises without hitting the HTTP client."""
        client = KalshiClient()
        client._circuit_open_until = time.monotonic() + 3600  # open for an hour

        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(httpx.HTTPStatusError, match="Circuit breaker open"):
            await client._request("GET", "/markets")

        # HTTP client should never have been called.
        mock_http.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_recovery_after_three_successes(self):
        """Half-open: 3 consecutive successes close the circuit."""
        client = KalshiClient()
        # Simulate a previously-tripped circuit that has now expired.
        client._circuit_breaker_triggers = 1
        client._consecutive_5xx = 5

        mock_http = MagicMock()
        mock_http.is_closed = False

        success_resp = _make_resp(200, {"ok": True})
        mock_http.get = AsyncMock(return_value=success_resp)
        client._client = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            for _ in range(3):
                await client._request("GET", "/markets")

        assert client._circuit_breaker_triggers == 0
        assert client._consecutive_5xx == 0
        assert client._recovery_successes == 0


# ---------------------------------------------------------------------------
# _request — 5xx retries, request errors, 204, JSON parse failure
# ---------------------------------------------------------------------------


class TestRequestRetries:
    @pytest.mark.asyncio
    async def test_5xx_retried_then_succeeds(self):
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        error_resp = _make_resp(503)
        ok_resp = _make_resp(200, {"result": "good"})

        mock_http.get = AsyncMock(side_effect=[error_resp, ok_resp])
        client._client = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/markets")

        assert result == {"result": "good"}

    @pytest.mark.asyncio
    async def test_204_returns_empty_dict(self):
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        no_content = MagicMock()
        no_content.status_code = 204
        no_content.headers = {}
        no_content.raise_for_status = MagicMock()

        mock_http.delete = AsyncMock(return_value=no_content)
        client._client = mock_http

        result = await client._request("DELETE", "/portfolio/orders/abc")
        assert result == {}

    @pytest.mark.asyncio
    async def test_json_parse_failure_retried(self):
        """Bad JSON on first attempt should be retried."""
        import json

        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        bad_json_resp = MagicMock()
        bad_json_resp.status_code = 200
        bad_json_resp.headers = {}
        bad_json_resp.raise_for_status = MagicMock()
        bad_json_resp.json.side_effect = json.JSONDecodeError("bad", "", 0)

        ok_resp = _make_resp(200, {"data": 42})
        mock_http.get = AsyncMock(side_effect=[bad_json_resp, ok_resp])
        client._client = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/markets")

        assert result == {"data": 42}

    @pytest.mark.asyncio
    async def test_request_error_resets_client_after_3_consecutive(self):
        """After 3 consecutive request errors, the HTTP client pool is reset."""
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.aclose = AsyncMock()

        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        client._client = mock_http

        with patch("asyncio.sleep", new_callable=AsyncMock):
            try:
                # Run enough separate requests to accumulate 3 timeouts within one _request.
                # Each _request retries internally, so drive _consecutive_timeouts up via
                # multiple _request calls.
                for _ in range(3):
                    client._consecutive_timeouts = 2  # pre-prime so third triggers reset
                    try:
                        await client._request("GET", "/markets")
                    except httpx.ConnectError:
                        pass
            except Exception:
                pass

        # After hitting the threshold, the client should have been reset.
        mock_http.aclose.assert_called()

    @pytest.mark.asyncio
    async def test_post_method_routes_correctly(self):
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        ok_resp = _make_resp(200, {"order": {"id": "123"}})
        mock_http.post = AsyncMock(return_value=ok_resp)
        client._client = mock_http

        result = await client._request("POST", "/portfolio/orders", json_body={"ticker": "X"})
        assert result == {"order": {"id": "123"}}
        mock_http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_method_raises_value_error(self):
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(ValueError, match="Unsupported method"):
            await client._request("PATCH", "/markets")

    @pytest.mark.asyncio
    async def test_auth_retry_on_401(self):
        """401 should trigger a single retry with 2s sleep before re-raising."""
        client = KalshiClient()
        mock_http = MagicMock()
        mock_http.is_closed = False

        auth_err_resp = _make_resp(401)
        ok_resp = _make_resp(200, {"ok": True})

        mock_http.get = AsyncMock(side_effect=[auth_err_resp, ok_resp])
        client._client = mock_http

        sleep_calls = []

        async def fake_sleep(s):
            sleep_calls.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            result = await client._request("GET", "/markets")

        assert result == {"ok": True}
        assert any(s == 2.0 for s in sleep_calls), "Should sleep 2s after 401"

    @pytest.mark.asyncio
    async def test_metrics_latency_recorded(self):
        """When metrics are provided, API latency should be recorded."""
        metrics = MagicMock()
        client = KalshiClient(metrics=metrics)

        mock_http = MagicMock()
        mock_http.is_closed = False

        ok_resp = _make_resp(200, {"ok": True})
        mock_http.get = AsyncMock(return_value=ok_resp)
        client._client = mock_http

        await client._request("GET", "/markets")

        metrics.record_api_latency.assert_called_once()
        args = metrics.record_api_latency.call_args[0]
        assert args[0] == "/markets"
        assert args[1] > 0  # latency in ms


# ---------------------------------------------------------------------------
# Public API methods
# ---------------------------------------------------------------------------


class TestGetMarkets:
    @pytest.mark.asyncio
    async def test_get_markets_returns_data(self):
        client = KalshiClient()
        expected = {"markets": [{"ticker": "KXBTC-25DEC"}], "cursor": None}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=expected):
            result = await client.get_markets()
        assert result == expected

    @pytest.mark.asyncio
    async def test_get_markets_returns_empty_on_none(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=None):
            result = await client.get_markets()
        assert result == {"markets": [], "cursor": None}

    @pytest.mark.asyncio
    async def test_get_markets_clamps_limit(self):
        """Limit is capped at 200."""
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}) as mock_req:
            await client.get_markets(limit=999)
        params = mock_req.call_args[1]["params"]
        assert params["limit"] == 200

    @pytest.mark.asyncio
    async def test_get_markets_passes_cursor_and_event_ticker(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}) as mock_req:
            await client.get_markets(cursor="abc", event_ticker="KXELEC")
        params = mock_req.call_args[1]["params"]
        assert params["cursor"] == "abc"
        assert params["event_ticker"] == "KXELEC"


class TestGetMarket:
    @pytest.mark.asyncio
    async def test_get_market_unwraps_market_key(self):
        client = KalshiClient()
        market_data = {"ticker": "KXBTC", "status": "open"}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"market": market_data}):
            result = await client.get_market("KXBTC")
        assert result == market_data

    @pytest.mark.asyncio
    async def test_get_market_returns_raw_when_no_market_key(self):
        client = KalshiClient()
        market_data = {"ticker": "KXBTC", "status": "open"}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=market_data):
            result = await client.get_market("KXBTC")
        assert result == market_data

    @pytest.mark.asyncio
    async def test_get_market_warns_on_missing_fields(self, caplog):
        import logging
        client = KalshiClient()
        # Market is missing 'status' field
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"ticker": "X"}):
            with caplog.at_level(logging.WARNING):
                result = await client.get_market("X")
        assert result == {"ticker": "X"}
        assert "missing expected fields" in caplog.text

    @pytest.mark.asyncio
    async def test_get_market_returns_none_on_exception(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("fail")):
            result = await client.get_market("X")
        assert result is None


class TestGetEvents:
    @pytest.mark.asyncio
    async def test_get_events_returns_data(self):
        client = KalshiClient()
        expected = {"events": [{"event_ticker": "KXELEC"}], "cursor": None}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=expected):
            result = await client.get_events()
        assert result == expected

    @pytest.mark.asyncio
    async def test_get_events_returns_empty_on_none(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=None):
            result = await client.get_events()
        assert result == {"events": [], "cursor": None}

    @pytest.mark.asyncio
    async def test_get_events_passes_status_and_cursor(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}) as mock_req:
            await client.get_events(status="open", cursor="tok")
        params = mock_req.call_args[1]["params"]
        assert params["status"] == "open"
        assert params["cursor"] == "tok"


class TestGetOrderbook:
    @pytest.mark.asyncio
    async def test_get_orderbook_unwraps_key(self):
        client = KalshiClient()
        ob = {"yes": [[50, 100]], "no": [[50, 100]]}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"orderbook": ob}):
            result = await client.get_orderbook("KXBTC")
        assert result == ob

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_raw_when_no_key(self):
        client = KalshiClient()
        raw = {"yes": [], "no": []}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=raw):
            result = await client.get_orderbook("KXBTC")
        assert result == raw

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_none_on_exception(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("err")):
            result = await client.get_orderbook("KXBTC")
        assert result is None


class TestGetMarketHistory:
    @pytest.mark.asyncio
    async def test_get_market_history_paginates(self):
        """Should accumulate results across paginated responses."""
        client = KalshiClient()
        page1 = {"trades": [{"id": "1"}, {"id": "2"}], "cursor": "page2"}
        page2 = {"trades": [{"id": "3"}], "cursor": None}

        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=[page1, page2]):
            result = await client.get_market_history("KXBTC", limit=100)

        assert len(result) == 3
        assert result[0]["id"] == "1"
        assert result[2]["id"] == "3"

    @pytest.mark.asyncio
    async def test_get_market_history_stops_at_limit(self):
        """Should stop fetching when limit is reached."""
        client = KalshiClient()
        page = {"trades": [{"id": str(i)} for i in range(50)], "cursor": "next"}

        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=[page, page]):
            result = await client.get_market_history("KXBTC", limit=50)

        assert len(result) == 50

    @pytest.mark.asyncio
    async def test_get_market_history_returns_empty_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("err")):
            result = await client.get_market_history("KXBTC")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_market_history_stops_when_no_trades(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"trades": []}):
            result = await client.get_market_history("KXBTC")
        assert result == []


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_get_balance_converts_cents_to_dollars(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"balance": 12345}):
            result = await client.get_balance()
        assert result == 123.45

    @pytest.mark.asyncio
    async def test_get_balance_returns_none_on_missing_key(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            result = await client.get_balance()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_balance_returns_none_on_invalid_value(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"balance": None}):
            result = await client.get_balance()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_balance_clamps_negative_to_zero(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"balance": -100}):
            result = await client.get_balance()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_balance_returns_none_on_http_error(self):
        client = KalshiClient()
        err = httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "http://test"), response=httpx.Response(403)
        )
        with patch.object(client, "_request", side_effect=err):
            result = await client.get_balance()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_balance_returns_none_on_unexpected_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("boom")):
            result = await client.get_balance()
        assert result is None


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_get_positions_returns_list(self):
        client = KalshiClient()
        positions = [{"market": "KXBTC", "side": "yes"}]
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"market_positions": positions}):
            result = await client.get_positions()
        assert result == positions

    @pytest.mark.asyncio
    async def test_get_positions_returns_empty_on_missing_key(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            result = await client.get_positions()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_positions_returns_empty_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=KalshiRateLimitError("rate limit")):
            result = await client.get_positions()
        assert result == []


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_create_order_returns_order_dict(self):
        client = KalshiClient()
        order = {"id": "ord-1", "status": "resting"}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"order": order}):
            result = await client.create_order("KXBTC", "yes", 55, 10)
        assert result == order

    @pytest.mark.asyncio
    async def test_create_order_returns_raw_when_no_order_key(self):
        client = KalshiClient()
        raw = {"id": "ord-1"}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=raw):
            result = await client.create_order("KXBTC", "yes", 55, 10)
        assert result == raw

    @pytest.mark.asyncio
    async def test_create_order_returns_none_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=httpx.RequestError("fail")):
            result = await client.create_order("KXBTC", "yes", 55, 10)
        assert result is None

    @pytest.mark.asyncio
    async def test_create_order_sends_correct_body(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"order": {}}) as mock_req:
            await client.create_order("KXBTC", "no", 45, 5, action="sell")
        body = mock_req.call_args[1]["json_body"]
        assert body["ticker"] == "KXBTC"
        assert body["side"] == "no"
        assert body["yes_price"] == 45
        assert body["count"] == 5
        assert body["action"] == "sell"


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_order_returns_data(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"status": "cancelled"}):
            result = await client.cancel_order("ord-1")
        assert result == {"status": "cancelled"}

    @pytest.mark.asyncio
    async def test_cancel_order_returns_none_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=KalshiRateLimitError("rate")):
            result = await client.cancel_order("ord-1")
        assert result is None


class TestGetOrder:
    @pytest.mark.asyncio
    async def test_get_order_unwraps_key(self):
        client = KalshiClient()
        order = {"id": "ord-1", "status": "resting"}
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"order": order}):
            result = await client.get_order("ord-1")
        assert result == order

    @pytest.mark.asyncio
    async def test_get_order_returns_none_when_no_data(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            result = await client.get_order("ord-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_order_returns_none_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://t"), response=httpx.Response(404)
        )):
            result = await client.get_order("missing")
        assert result is None


class TestGetOpenOrders:
    @pytest.mark.asyncio
    async def test_get_open_orders_returns_list(self):
        client = KalshiClient()
        orders = [{"id": "ord-1"}, {"id": "ord-2"}]
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"orders": orders}):
            result = await client.get_open_orders()
        assert result == orders

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_empty_on_missing_key(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}):
            result = await client.get_open_orders()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_empty_on_error(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("fail")):
            result = await client.get_open_orders()
        assert result == []


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_exception(self):
        client = KalshiClient()
        with patch.object(client, "_request", side_effect=RuntimeError("down")):
            result = await client.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_none(self):
        client = KalshiClient()
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=None):
            result = await client.health_check()
        assert result is False


class TestClose:
    @pytest.mark.asyncio
    async def test_close_skips_when_already_closed(self):
        client = KalshiClient()
        mock_http = AsyncMock()
        mock_http.is_closed = True
        client._client = mock_http

        await client.close()
        mock_http.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_skips_when_no_client(self):
        client = KalshiClient()
        # _client is None by default — should not raise
        await client.close()
