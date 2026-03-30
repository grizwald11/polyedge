"""Tests for the CME FedWatch client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.fedwatch import FedWatchClient

SAMPLE_HTML = """
<html>
<body>
<div class="fedwatch">
May 2026 meeting: 60.5% probability of rate cut
Jun 2026 expected: 75.0% cumulative cut
</div>
</body>
</html>
"""

SAMPLE_HTML_NO_DATA = """
<html><body><p>No meeting data</p></body></html>
"""


class TestGetRateProbabilities:
    @pytest.mark.asyncio
    async def test_successful_parse(self):
        client = FedWatchClient()

        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fedwatch.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_rate_probabilities()

        assert result is not None
        assert len(result) >= 1
        assert result[0]["meeting"] == "May 2026"
        assert result[0]["cut_prob"] == 60.5

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self):
        client = FedWatchClient()

        mock_response = MagicMock()
        mock_response.text = SAMPLE_HTML_NO_DATA
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.fedwatch.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = FedWatchClient()

        with patch("src.data.fedwatch.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await client.get_rate_probabilities()

        assert result is None


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context(self):
        client = FedWatchClient()

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {"meeting": "May 2026", "cut_prob": 60.0, "hold_prob": 35.0, "hike_prob": 5.0},
                {"meeting": "Jun 2026", "cut_prob": 75.0, "hold_prob": 25.0, "hike_prob": 0.0},
            ]
            context = await client.get_context()

        assert "FED FUNDS FUTURES" in context
        assert "May 2026" in context
        assert "60% cut" in context
        assert "35% hold" in context
        assert "5% hike" in context

    @pytest.mark.asyncio
    async def test_no_hike_omitted(self):
        client = FedWatchClient()

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {"meeting": "May 2026", "cut_prob": 60.0, "hold_prob": 40.0, "hike_prob": 0.0},
            ]
            context = await client.get_context()

        assert "hike" not in context

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        client = FedWatchClient()

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context()

        assert context == ""
