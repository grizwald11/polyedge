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

SAMPLE_HTML_MULTI_MEETINGS = """
<html><body>
Jan 2026 FOMC: 25.3% probability
Mar 2026 FOMC: 45.8% probability
May 2026 FOMC: 60.5% probability
Jun 2026 FOMC: 75.0% probability
Jul 2026 FOMC: 82.1% probability
Sep 2026 FOMC: 90.0% probability
</body></html>
"""


def _mock_response(html: str):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response):
    """Patch httpx.AsyncClient to return mock_response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client), mock_client


class TestGetRateProbabilities:
    @pytest.mark.asyncio
    async def test_successful_parse(self):
        client = FedWatchClient(max_retries=0)
        patcher, _ = _patch_httpx(_mock_response(SAMPLE_HTML))
        with patcher:
            result = await client.get_rate_probabilities()

        assert result is not None
        assert len(result) >= 1
        assert result[0]["meeting"] == "May 2026"
        assert result[0]["cut_prob"] == 60.5

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self):
        client = FedWatchClient(max_retries=0)
        patcher, _ = _patch_httpx(_mock_response(SAMPLE_HTML_NO_DATA))
        with patcher:
            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        client = FedWatchClient(max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_caching(self):
        client = FedWatchClient(max_retries=0)
        patcher, mock_client = _patch_httpx(_mock_response(SAMPLE_HTML))
        with patcher:
            result1 = await client.get_rate_probabilities()
            result2 = await client.get_rate_probabilities()

        assert mock_client.get.call_count == 1
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_multiple_meetings_parsed(self):
        client = FedWatchClient(max_retries=0)
        patcher, _ = _patch_httpx(_mock_response(SAMPLE_HTML_MULTI_MEETINGS))
        with patcher:
            result = await client.get_rate_probabilities()

        assert result is not None
        assert len(result) == 6
        assert result[0]["meeting"] == "Jan 2026"
        assert result[-1]["meeting"] == "Sep 2026"

    @pytest.mark.asyncio
    async def test_deduplicates_meetings(self):
        html = """
        <html><body>
        May 2026 FOMC: 60.5% probability
        May 2026 update: 62.0% probability
        Jun 2026 FOMC: 75.0% probability
        </body></html>
        """
        client = FedWatchClient(max_retries=0)
        patcher, _ = _patch_httpx(_mock_response(html))
        with patcher:
            result = await client.get_rate_probabilities()

        assert result is not None
        meeting_names = [m["meeting"] for m in result]
        assert meeting_names.count("May 2026") == 1


class TestParseProbabilities:
    def test_hold_prob_complement(self):
        client = FedWatchClient()
        result = client._parse_probabilities(SAMPLE_HTML)
        assert result is not None
        for m in result:
            assert abs(m["cut_prob"] + m["hold_prob"] - 100.0) < 0.1

    def test_rejects_probability_over_100(self):
        html = """<html><body>May 2026 meeting: 150.0% rate</body></html>"""
        client = FedWatchClient()
        result = client._parse_probabilities(html)
        assert result is None  # No valid meetings after validation

    def test_negative_sign_ignored_by_regex(self):
        # Regex matches the digits after the minus sign (5.0%), not -5.0
        html = """<html><body>May 2026 meeting: -5.0% rate</body></html>"""
        client = FedWatchClient()
        result = client._parse_probabilities(html)
        assert result is not None
        assert result[0]["cut_prob"] == 5.0

    def test_zero_probability_accepted(self):
        html = """<html><body>May 2026 meeting: 0% cut probability</body></html>"""
        client = FedWatchClient()
        result = client._parse_probabilities(html)
        assert result is not None
        assert result[0]["cut_prob"] == 0.0
        assert result[0]["hold_prob"] == 100.0

    def test_100_probability_accepted(self):
        html = """<html><body>May 2026 meeting: 100% cut probability</body></html>"""
        client = FedWatchClient()
        result = client._parse_probabilities(html)
        assert result is not None
        assert result[0]["cut_prob"] == 100.0
        assert result[0]["hold_prob"] == 0.0

    def test_empty_html(self):
        client = FedWatchClient()
        result = client._parse_probabilities("")
        assert result is None

    def test_malformed_html_no_crash(self):
        client = FedWatchClient()
        result = client._parse_probabilities("<html><body>!@#$%^&*()</body></html>")
        assert result is None

    def test_partial_meeting_data(self):
        # Meeting date without percentage — should not match
        html = """<html><body>May 2026 meeting scheduled</body></html>"""
        client = FedWatchClient()
        result = client._parse_probabilities(html)
        assert result is None


class TestStaleCacheFallback:
    @pytest.mark.asyncio
    async def test_serves_stale_on_http_failure(self):
        client = FedWatchClient(max_retries=0)

        # First call succeeds
        patcher, _ = _patch_httpx(_mock_response(SAMPLE_HTML))
        with patcher:
            result1 = await client.get_rate_probabilities()
        assert result1 is not None

        # Clear TTL cache to force re-fetch
        client._cache.clear()

        # Second call fails — should serve stale
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("network down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result2 = await client.get_rate_probabilities()

        assert result2 is not None
        assert result2 == result1

    @pytest.mark.asyncio
    async def test_no_stale_on_first_failure(self):
        """First-ever fetch failure with no stale cache returns None."""
        client = FedWatchClient(max_retries=0)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            result = await client.get_rate_probabilities()

        assert result is None

    @pytest.mark.asyncio
    async def test_serves_stale_on_parse_failure(self):
        client = FedWatchClient(max_retries=0)

        # First call succeeds
        patcher, _ = _patch_httpx(_mock_response(SAMPLE_HTML))
        with patcher:
            result1 = await client.get_rate_probabilities()
        assert result1 is not None

        client._cache.clear()

        # Second call gets unparseable HTML
        patcher2, _ = _patch_httpx(_mock_response(SAMPLE_HTML_NO_DATA))
        with patcher2:
            result2 = await client.get_rate_probabilities()

        assert result2 is not None
        assert result2 == result1


class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        client = FedWatchClient(max_retries=2)
        good_response = _mock_response(SAMPLE_HTML)

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.HTTPError("transient")
            return good_response

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.fedwatch.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client.get_rate_probabilities()

        assert result is not None
        assert call_count == 3  # 2 failures + 1 success


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

    @pytest.mark.asyncio
    async def test_limits_to_3_meetings(self):
        client = FedWatchClient()

        with patch.object(client, "get_rate_probabilities", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {"meeting": f"M{i} 2026", "cut_prob": 50.0, "hold_prob": 50.0}
                for i in range(6)
            ]
            context = await client.get_context()

        # Should only show first 3 meetings
        assert "M0 2026" in context
        assert "M2 2026" in context
        assert "M3 2026" not in context
