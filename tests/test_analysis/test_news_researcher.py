"""Tests for the news researcher module."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from src.analysis.news_researcher import NewsResearcher, NewsResult, _extract_source


class TestGenerateQueries:
    def test_basic_question(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries(
            "Will DHS funding bill become law before Apr 1, 2026?"
        )
        assert len(queries) >= 2
        assert len(queries) <= 3
        # First query should be the cleaned question
        assert "Will" not in queries[0]
        assert "DHS" in queries[0]

    def test_strips_will_prefix(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries("Will the Fed cut rates in May?")
        assert not queries[0].startswith("Will")
        assert "Fed" in queries[0]

    def test_strips_question_mark(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries("Is Bitcoin above 100k?")
        assert "?" not in queries[0]

    def test_includes_time_scoped_query(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries("Will Trump win 2028?")
        # Second query should have time scope
        assert any("2026" in q for q in queries)

    def test_extracts_entities(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries(
            "Will the European Central Bank raise rates?"
        )
        assert len(queries) >= 2


class TestSearch:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self):
        researcher = NewsResearcher(serper_api_key=None)
        results = await researcher.search("test query")
        assert results == []

    @pytest.mark.asyncio
    async def test_successful_search(self):
        researcher = NewsResearcher(serper_api_key="test-key")
        mock_response_data = {
            "organic": [
                {
                    "title": "DHS Funding Bill Stalls in Senate",
                    "snippet": "Senate leaders failed to reach agreement...",
                    "link": "https://www.reuters.com/article/dhs-funding",
                    "date": "2 days ago",
                },
                {
                    "title": "House Passes DHS Bill",
                    "snippet": "The House narrowly approved...",
                    "link": "https://apnews.com/article/dhs-bill",
                    "date": "5 days ago",
                },
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await researcher.search("DHS funding bill")

        assert len(results) == 2
        assert results[0].title == "DHS Funding Bill Stalls in Senate"
        assert results[0].source == "reuters.com"
        assert results[1].date == "5 days ago"

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self):
        researcher = NewsResearcher(serper_api_key="test-key")

        with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await researcher.search("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_empty_response(self):
        researcher = NewsResearcher(serper_api_key="test-key")

        mock_response = MagicMock()
        mock_response.json.return_value = {"organic": []}
        mock_response.raise_for_status = MagicMock()

        with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await researcher.search("nothing here")

        assert results == []


class TestGetContext:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty_string(self):
        researcher = NewsResearcher(serper_api_key=None)
        context = await researcher.get_context("Will X happen?")
        assert context == ""

    @pytest.mark.asyncio
    async def test_formats_context_block(self):
        researcher = NewsResearcher(serper_api_key="test-key")

        mock_results = [
            NewsResult(
                title="Big News",
                snippet="Something happened...",
                source="reuters.com",
                date="1 day ago",
                url="https://reuters.com/1",
            ),
            NewsResult(
                title="More News",
                snippet="Another thing...",
                source="apnews.com",
                date="3 days ago",
                url="https://apnews.com/2",
            ),
        ]

        with patch.object(researcher, "search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = mock_results
            context = await researcher.get_context("Will X happen?")

        assert "RECENT NEWS CONTEXT:" in context
        assert '[1] "Big News"' in context
        assert '[2] "More News"' in context
        assert "reuters.com" in context
        assert "1 day ago" in context

    @pytest.mark.asyncio
    async def test_deduplicates_results(self):
        researcher = NewsResearcher(serper_api_key="test-key")

        same_result = NewsResult(
            title="Same Article",
            snippet="...",
            source="cnn.com",
            date="",
            url="https://cnn.com/same",
        )

        with patch.object(researcher, "search", new_callable=AsyncMock) as mock_search:
            # All queries return the same result
            mock_search.return_value = [same_result]
            context = await researcher.get_context("Will X happen?")

        # Should only appear once despite multiple queries
        assert context.count('"Same Article"') == 1


class TestFormatContext:
    def test_basic_formatting(self):
        researcher = NewsResearcher()
        results = [
            NewsResult(
                title="Test Title",
                snippet="Test snippet text",
                source="example.com",
                date="2 days ago",
                url="https://example.com/1",
            ),
        ]
        context = researcher._format_context(results)
        assert "RECENT NEWS CONTEXT:" in context
        assert '[1] "Test Title" (example.com, 2 days ago)' in context
        assert "Test snippet text" in context

    def test_no_date(self):
        researcher = NewsResearcher()
        results = [
            NewsResult(
                title="No Date",
                snippet="...",
                source="example.com",
                date="",
                url="https://example.com/1",
            ),
        ]
        context = researcher._format_context(results)
        assert "(example.com)" in context


class TestExtractSource:
    def test_strips_www(self):
        assert _extract_source("https://www.reuters.com/article") == "reuters.com"

    def test_no_www(self):
        assert _extract_source("https://apnews.com/article/x") == "apnews.com"

    def test_empty_url(self):
        assert _extract_source("") == ""
