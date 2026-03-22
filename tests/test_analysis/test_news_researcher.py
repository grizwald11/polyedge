"""Tests for the news researcher module."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from src.analysis.news_researcher import (
    NewsResearcher, NewsResult, _extract_source,
)


class TestGenerateQueries:
    def test_basic_question(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries(
            "Will DHS funding bill become law before Apr 1, 2026?"
        )
        assert len(queries) >= 2
        assert len(queries) <= 3
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
        from datetime import datetime, timezone
        researcher = NewsResearcher()
        queries = researcher.generate_queries("Will Trump win 2028?")
        current_year = str(datetime.now(timezone.utc).year)
        assert any(current_year in q for q in queries)

    def test_extracts_entities(self):
        researcher = NewsResearcher()
        queries = researcher.generate_queries(
            "Will the European Central Bank raise rates?"
        )
        assert len(queries) >= 2


class TestSearch:
    @pytest.mark.asyncio
    async def test_ddg_results_returned(self):
        """DDG results are returned when available."""
        researcher = NewsResearcher()
        ddg_result = NewsResult(
            title="DDG Result", snippet="From DDG", source="cnn.com",
            date="2026-03-19", url="https://cnn.com/1",
        )

        with patch.object(researcher, "_search_ddg", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = [ddg_result]
            results = await researcher.search("test query")

        assert len(results) == 1
        assert results[0].title == "DDG Result"

    @pytest.mark.asyncio
    async def test_serper_fallback_when_ddg_fails(self):
        """When DDG returns nothing, Serper fallback is used."""
        researcher = NewsResearcher(serper_api_key="test-key")

        serper_data = {
            "organic": [
                {
                    "title": "Serper Result",
                    "snippet": "From Serper...",
                    "link": "https://reuters.com/serper",
                    "date": "1 day ago",
                },
            ]
        }

        mock_serper_response = MagicMock()
        mock_serper_response.json.return_value = serper_data
        mock_serper_response.raise_for_status = MagicMock()

        with patch.object(researcher, "_search_ddg", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = []

            with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_serper_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                results = await researcher.search("test query")

        assert len(results) == 1
        assert results[0].title == "Serper Result"

    @pytest.mark.asyncio
    async def test_serper_not_tried_when_ddg_succeeds(self):
        """When DDG returns results, Serper is not called."""
        researcher = NewsResearcher(serper_api_key="test-key")

        ddg_result = NewsResult(
            title="DDG Result", snippet="From DDG", source="cnn.com",
            date="", url="https://cnn.com/1",
        )

        with patch.object(researcher, "_search_ddg", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = [ddg_result]

            with patch.object(researcher, "_search_serper", new_callable=AsyncMock) as mock_serper:
                results = await researcher.search("test query")
                mock_serper.assert_not_called()

        assert len(results) == 1
        assert results[0].title == "DDG Result"

    @pytest.mark.asyncio
    async def test_no_serper_key_no_fallback(self):
        """Without Serper key, DDG failure returns empty."""
        researcher = NewsResearcher(serper_api_key=None)

        with patch.object(researcher, "_search_ddg", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = []
            results = await researcher.search("test query")

        assert results == []

    @pytest.mark.asyncio
    async def test_ddg_unavailable_uses_serper(self):
        """When DDG_AVAILABLE is False, Serper is used directly."""
        researcher = NewsResearcher(serper_api_key="test-key")

        serper_data = {
            "organic": [
                {
                    "title": "Serper Only",
                    "snippet": "...",
                    "link": "https://example.com/1",
                    "date": "",
                },
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = serper_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.analysis.news_researcher.DDG_AVAILABLE", False):
            with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                results = await researcher.search("test query")

        assert len(results) == 1
        assert results[0].title == "Serper Only"

    @pytest.mark.asyncio
    async def test_serper_http_error_returns_empty(self):
        researcher = NewsResearcher(serper_api_key="test-key")

        with patch("src.analysis.news_researcher.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await researcher._search_serper("test")

        assert results == []


class TestDDGSearch:
    @pytest.mark.asyncio
    async def test_ddg_news_search(self):
        """Test DDG news search via duckduckgo-search library."""
        researcher = NewsResearcher()

        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.news.return_value = [
            {
                "title": "Breaking News",
                "body": "Something important happened...",
                "source": "Reuters",
                "date": "2026-03-19T10:00:00",
                "url": "https://reuters.com/article/1",
            },
        ]

        with patch("src.analysis.news_researcher.DDG_AVAILABLE", True):
            with patch("src.analysis.news_researcher.DDGS", return_value=mock_ddgs):
                results = await researcher._search_ddg("test query")

        assert len(results) == 1
        assert results[0].title == "Breaking News"
        assert results[0].source == "Reuters"

    @pytest.mark.asyncio
    async def test_ddg_falls_back_to_text_search(self):
        """When DDG news returns nothing, falls back to text search."""
        researcher = NewsResearcher()

        mock_ddgs_news = MagicMock()
        mock_ddgs_news.__enter__ = MagicMock(return_value=mock_ddgs_news)
        mock_ddgs_news.__exit__ = MagicMock(return_value=False)
        mock_ddgs_news.news.return_value = []  # No news results

        mock_ddgs_text = MagicMock()
        mock_ddgs_text.__enter__ = MagicMock(return_value=mock_ddgs_text)
        mock_ddgs_text.__exit__ = MagicMock(return_value=False)
        mock_ddgs_text.text.return_value = [
            {
                "title": "Text Result",
                "body": "From text search...",
                "href": "https://example.com/1",
            },
        ]

        call_count = 0

        def mock_ddgs_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_ddgs_news
            return mock_ddgs_text

        with patch("src.analysis.news_researcher.DDG_AVAILABLE", True):
            with patch("src.analysis.news_researcher.DDGS", side_effect=mock_ddgs_factory):
                results = await researcher._search_ddg("test query")

        assert len(results) == 1
        assert results[0].title == "Text Result"

    @pytest.mark.asyncio
    async def test_ddg_exception_returns_empty(self):
        """DDG exceptions are caught and return empty list."""
        researcher = NewsResearcher()

        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.news.side_effect = Exception("rate limited")

        mock_ddgs_text = MagicMock()
        mock_ddgs_text.__enter__ = MagicMock(return_value=mock_ddgs_text)
        mock_ddgs_text.__exit__ = MagicMock(return_value=False)
        mock_ddgs_text.text.side_effect = Exception("also failed")

        call_count = 0

        def mock_ddgs_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_ddgs
            return mock_ddgs_text

        with patch("src.analysis.news_researcher.DDG_AVAILABLE", True):
            with patch("src.analysis.news_researcher.DDGS", side_effect=mock_ddgs_factory):
                results = await researcher._search_ddg("test query")

        assert results == []


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context_block(self):
        researcher = NewsResearcher()

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
        researcher = NewsResearcher()

        same_result = NewsResult(
            title="Same Article",
            snippet="...",
            source="cnn.com",
            date="",
            url="https://cnn.com/same",
        )

        with patch.object(researcher, "search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = [same_result]
            context = await researcher.get_context("Will X happen?")

        assert context.count('"Same Article"') == 1

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_string(self):
        researcher = NewsResearcher()

        with patch.object(researcher, "search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = []
            context = await researcher.get_context("Will X happen?")

        assert context == ""


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
