"""Tests for the FedWatch client (FRED API-based)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.fedwatch import FedWatchClient


def _mock_fred_response(observations: list[dict]):
    """Create a mock httpx response with FRED JSON format."""
    resp = MagicMock()
    resp.json.return_value = {"observations": observations}
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response):
    """Patch httpx.AsyncClient to return mock_response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client), mock_client


# Standard FRED responses for each series
DFEDTARU_OBS = [{"value": "5.50", "date": "2026-04-01"}]
DFEDTARL_OBS = [{"value": "5.25", "date": "2026-04-01"}]
DFF_OBS = [{"value": "5.33", "date": "2026-04-01"}]


class TestGetRateProbabilities:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)

        responses = iter([
            _mock_fred_response(DFEDTARU_OBS),
            _mock_fred_response(DFEDTARL_OBS),
            _mock_fred_response(DFF_OBS),
        ])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_rate_probabilities()

        assert result is not None
        assert len(result) == 1
        assert result[0]["meeting"] == "Current Target"
        assert result[0]["target_upper"] == 5.50
        assert result[0]["target_lower"] == 5.25
        assert result[0]["effective_rate"] == 5.33

    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self):
        client = FedWatchClient(api_key=None, max_retries=0)
        result = await client.get_rate_probabilities()
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_observations_returns_none(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([]))
        with patcher:
            result = await client.get_rate_probabilities()
        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_caching(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)

        responses = iter([
            _mock_fred_response(DFEDTARU_OBS),
            _mock_fred_response(DFEDTARL_OBS),
            _mock_fred_response(DFF_OBS),
        ])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result1 = await client.get_rate_probabilities()
            result2 = await client.get_rate_probabilities()

        assert result1 == result2
        # Second call should use cache, so only 3 HTTP calls total (not 6)
        assert mock_client.get.call_count == 3


class TestFetchSeries:
    @pytest.mark.asyncio
    async def test_dot_value_returns_none(self):
        """FRED uses '.' for missing values."""
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": ".", "date": "2026-04-01"}]))
        with patcher:
            result = await client._fetch_series("DFF")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_value_over_max(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "25.0", "date": "2026-04-01"}]))
        with patcher:
            result = await client._fetch_series("DFF")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_negative_value(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "-1.0", "date": "2026-04-01"}]))
        with patcher:
            result = await client._fetch_series("DFF")
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_zero(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "0.0", "date": "2026-04-01"}]))
        with patcher:
            result = await client._fetch_series("DFF")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_accepts_boundary_max(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "20.0", "date": "2026-04-01"}]))
        with patcher:
            result = await client._fetch_series("DFF")
        assert result == 20.0


class TestStaleCacheFallback:
    @pytest.mark.asyncio
    async def test_serves_stale_on_http_failure(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)

        # First call succeeds
        responses = iter([
            _mock_fred_response(DFEDTARU_OBS),
            _mock_fred_response(DFEDTARL_OBS),
            _mock_fred_response(DFF_OBS),
        ])
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result1 = await client.get_rate_probabilities()
        assert result1 is not None

        # Clear TTL cache
        client._cache.clear()

        # Second call fails — should serve stale
        mock_client2 = AsyncMock()
        mock_client2.get = AsyncMock(side_effect=httpx.HTTPError("network down"))
        mock_client2.__aenter__ = AsyncMock(return_value=mock_client2)
        mock_client2.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client2):
            result2 = await client.get_rate_probabilities()

        assert result2 is not None
        assert result2 == result1

    @pytest.mark.asyncio
    async def test_no_stale_on_first_failure(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_consecutive_failure_tracking(self):
        client = FedWatchClient(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            for _ in range(3):
                await client.get_rate_probabilities()

        assert client._consecutive_failures == 3


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context(self):
        client = FedWatchClient(api_key="test-key")

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {
                    "meeting": "Current Target",
                    "target_upper": 5.50,
                    "target_lower": 5.25,
                    "effective_rate": 5.33,
                    "cut_prob": 0.0,
                    "hold_prob": 100.0,
                },
            ]
            context = await client.get_context()

        assert "FED FUNDS RATE" in context
        assert "5.25%" in context
        assert "5.50%" in context
        assert "5.33%" in context

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        client = FedWatchClient(api_key="test-key")

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context()

        assert context == ""

    @pytest.mark.asyncio
    async def test_partial_data_no_effective_rate(self):
        client = FedWatchClient(api_key="test-key")

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {
                    "meeting": "Current Target",
                    "target_upper": 5.50,
                    "target_lower": 5.25,
                    "effective_rate": None,
                    "cut_prob": 0.0,
                    "hold_prob": 100.0,
                },
            ]
            context = await client.get_context()

        assert "5.25%" in context
        assert "5.50%" in context
        # effective_rate is None so should not appear
        assert "Effective" not in context
