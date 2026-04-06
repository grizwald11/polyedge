"""Tests for the Cleveland Fed inflation nowcast client (FRED API-based)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.cleveland_fed import ClevelandFedNowcast


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
    return patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client), mock_client


# Standard FRED observations for each CPI series
CPIAUCSL_OBS = [{"value": "3.10", "date": "2026-03-01"}]
CPILFESL_OBS = [{"value": "3.30", "date": "2026-03-01"}]
PCEPILFE_OBS = [{"value": "2.80", "date": "2026-03-01"}]


class TestGetNowcast:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)

        responses = iter([
            _mock_fred_response(CPIAUCSL_OBS),
            _mock_fred_response(CPILFESL_OBS),
            _mock_fred_response(PCEPILFE_OBS),
        ])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_nowcast()

        assert result is not None
        assert result["cpi"] == 3.10
        assert result["core_cpi"] == 3.30
        assert result["core_pce"] == 2.80
        assert result["as_of"] == "2026-03-01"

    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self):
        client = ClevelandFedNowcast(api_key=None, max_retries=0)
        result = await client.get_nowcast()
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_observations_returns_none(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([]))
        with patcher:
            result = await client.get_nowcast()
        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_nowcast()

        assert result is None

    @pytest.mark.asyncio
    async def test_caching(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)

        responses = iter([
            _mock_fred_response(CPIAUCSL_OBS),
            _mock_fred_response(CPILFESL_OBS),
            _mock_fred_response(PCEPILFE_OBS),
        ])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result1 = await client.get_nowcast()
            result2 = await client.get_nowcast()

        assert result1 == result2
        assert mock_client.get.call_count == 3  # Only 3 calls, second uses cache

    @pytest.mark.asyncio
    async def test_cpi_only_when_core_missing(self):
        """Only CPIAUCSL returns data, others return empty observations."""
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)

        responses = iter([
            _mock_fred_response(CPIAUCSL_OBS),
            _mock_fred_response([]),  # No core CPI
            _mock_fred_response([]),  # No core PCE
        ])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_nowcast()

        assert result is not None
        assert result["cpi"] == 3.10
        assert result["core_cpi"] is None


class TestFetchSeries:
    @pytest.mark.asyncio
    async def test_dot_value_returns_none(self):
        """FRED uses '.' for missing values."""
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": ".", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_value_over_max(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "55.0", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_below_min(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "-6.0", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_negative_deflation(self):
        """Deflation values within bounds should be accepted."""
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "-2.0", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result is not None
        assert result == (-2.0, "2026-03-01")

    @pytest.mark.asyncio
    async def test_accepts_zero(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "0.0", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result == (0.0, "2026-03-01")

    @pytest.mark.asyncio
    async def test_accepts_high_but_valid(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        patcher, _ = _patch_httpx(_mock_fred_response([{"value": "15.5", "date": "2026-03-01"}]))
        with patcher:
            result = await client._fetch_series("CPIAUCSL")
        assert result == (15.5, "2026-03-01")


class TestStaleCacheFallback:
    @pytest.mark.asyncio
    async def test_serves_stale_on_http_failure(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)

        # First call succeeds
        responses = iter([
            _mock_fred_response(CPIAUCSL_OBS),
            _mock_fred_response(CPILFESL_OBS),
            _mock_fred_response(PCEPILFE_OBS),
        ])
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(responses))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result1 = await client.get_nowcast()
        assert result1 is not None

        # Clear TTL cache
        client._cache.clear()

        # Second call fails — should serve stale
        mock_client2 = AsyncMock()
        mock_client2.get = AsyncMock(side_effect=httpx.HTTPError("network down"))
        mock_client2.__aenter__ = AsyncMock(return_value=mock_client2)
        mock_client2.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client2):
            result2 = await client.get_nowcast()

        assert result2 is not None
        assert result2 == result1

    @pytest.mark.asyncio
    async def test_no_stale_on_first_failure(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_nowcast()

        assert result is None

    @pytest.mark.asyncio
    async def test_consecutive_failure_tracking(self):
        client = ClevelandFedNowcast(api_key="test-key", max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.cleveland_fed.httpx.AsyncClient", return_value=mock_client):
            for _ in range(3):
                await client.get_nowcast()

        assert client._consecutive_failures == 3


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context(self):
        client = ClevelandFedNowcast(api_key="test-key")

        with patch.object(client, "get_nowcast", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "cpi": 3.1,
                "core_cpi": 3.3,
                "core_pce": 2.8,
                "as_of": "2026-03-01",
            }
            context = await client.get_context()

        assert "INFLATION DATA" in context
        assert "3.1%" in context
        assert "3.3%" in context
        assert "2.8%" in context

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        client = ClevelandFedNowcast(api_key="test-key")

        with patch.object(client, "get_nowcast", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context()

        assert context == ""

    @pytest.mark.asyncio
    async def test_partial_data_cpi_only(self):
        client = ClevelandFedNowcast(api_key="test-key")

        with patch.object(client, "get_nowcast", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "cpi": 3.1,
                "core_cpi": None,
                "as_of": "2026-03-01",
            }
            context = await client.get_context()

        assert "3.1%" in context
        assert "Core CPI" not in context
