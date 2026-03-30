"""Tests for the Cleveland Fed inflation nowcast client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.cleveland_fed import ClevelandFedNowcast

SAMPLE_HTML = """
<html>
<body>
<h1>Inflation Nowcasting</h1>
<p>CPI Nowcast: 3.1% as of March 15, 2026</p>
<p>Core CPI Nowcast: 3.3%</p>
</body>
</html>
"""

SAMPLE_HTML_NO_DATA = """
<html>
<body>
<h1>Inflation Nowcasting</h1>
<p>Data currently unavailable.</p>
</body>
</html>
"""


class TestGetNowcast:
    @pytest.mark.asyncio
    async def test_successful_parse(self):
        client = ClevelandFedNowcast()

        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.cleveland_fed.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_nowcast()

        assert result is not None
        assert result["cpi"] == 3.1
        assert result["core_cpi"] == 3.3

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self):
        client = ClevelandFedNowcast()

        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML_NO_DATA
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.cleveland_fed.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_nowcast()

        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = ClevelandFedNowcast()

        with patch("src.data.cleveland_fed.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_nowcast()

        assert result is None

    @pytest.mark.asyncio
    async def test_caching(self):
        client = ClevelandFedNowcast()

        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.cleveland_fed.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result1 = await client.get_nowcast()
            result2 = await client.get_nowcast()

        assert mock_client.get.call_count == 1
        assert result1 == result2


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context(self):
        client = ClevelandFedNowcast()

        with patch.object(client, "get_nowcast", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "cpi": 3.1,
                "core_cpi": 3.3,
                "as_of": "March 15, 2026",
            }
            context = await client.get_context()

        assert "CLEVELAND FED INFLATION NOWCAST" in context
        assert "3.1%" in context
        assert "3.3%" in context
        assert "BLS release" in context

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        client = ClevelandFedNowcast()

        with patch.object(client, "get_nowcast", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context()

        assert context == ""
