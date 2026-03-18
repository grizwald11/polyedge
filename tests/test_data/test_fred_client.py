"""Tests for the FRED client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.fred_client import FREDClient


class TestGetSeriesLatest:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self):
        client = FREDClient(api_key=None)
        result = await client.get_series_latest("CPIAUCSL")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        client = FREDClient(api_key="test-key")
        mock_response_data = {
            "observations": [
                {"date": "2026-02-01", "value": "3.2"},
                {"date": "2026-01-01", "value": "3.5"},
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fred_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_series_latest("CPIAUCSL")

        assert result is not None
        assert result["value"] == 3.2
        assert result["date"] == "2026-02-01"
        assert result["previous_value"] == 3.5

    @pytest.mark.asyncio
    async def test_skips_missing_values(self):
        client = FREDClient(api_key="test-key")
        mock_response_data = {
            "observations": [
                {"date": "2026-03-01", "value": "."},
                {"date": "2026-02-01", "value": "4.1"},
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fred_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_series_latest("UNRATE")

        assert result is not None
        assert result["value"] == 4.1
        assert result["previous_value"] is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = FREDClient(api_key="test-key")

        with patch("src.data.fred_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_series_latest("CPIAUCSL")

        assert result is None

    @pytest.mark.asyncio
    async def test_caching(self):
        client = FREDClient(api_key="test-key")
        mock_response_data = {
            "observations": [
                {"date": "2026-02-01", "value": "3.2"},
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fred_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result1 = await client.get_series_latest("CPIAUCSL")
            result2 = await client.get_series_latest("CPIAUCSL")

        # Second call should use cache, so only one HTTP call
        assert mock_client.get.call_count == 1
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_empty_observations(self):
        client = FREDClient(api_key="test-key")
        mock_response_data = {"observations": []}

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fred_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_series_latest("CPIAUCSL")

        assert result is None


class TestGetMacroSummary:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self):
        client = FREDClient(api_key=None)
        result = await client.get_macro_summary()
        assert result == ""

    @pytest.mark.asyncio
    async def test_formats_summary(self):
        client = FREDClient(api_key="test-key")

        async def mock_latest(series_id):
            data = {
                "CPIAUCSL": {"value": 3.2, "date": "2026-02-01", "previous_value": 3.5},
                "UNRATE": {"value": 4.1, "date": "2026-02-01", "previous_value": 4.1},
                "FEDFUNDS": {"value": 4.50, "date": "2026-02-01", "previous_value": 4.50},
            }
            return data.get(series_id)

        with patch.object(client, "get_series_latest", side_effect=mock_latest):
            result = await client.get_macro_summary()

        assert "ECONOMIC DATA (FRED" in result
        assert "CPI" in result
        assert "3.2" in result
        assert "Unemployment" in result
        assert "4.10%" in result
        assert "unchanged" in result

    @pytest.mark.asyncio
    async def test_all_failures_returns_empty(self):
        client = FREDClient(api_key="test-key")

        async def mock_latest(series_id):
            return None

        with patch.object(client, "get_series_latest", side_effect=mock_latest):
            result = await client.get_macro_summary()

        assert result == ""
