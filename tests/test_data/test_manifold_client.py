"""Tests for Manifold Markets client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.manifold_client import (
    MIN_BETTORS,
    MIN_SIMILARITY_THRESHOLD,
    ManifoldClient,
)

# ──────────────────────────────────────
# Fixtures
# ──────────────────────────────────────


@pytest.fixture
def manifold_client():
    """ManifoldClient with short TTL for testing."""
    return ManifoldClient(ttl_seconds=5)


@pytest.fixture
def sample_manifold_response():
    """Sample raw API response from Manifold Markets search."""
    return [
        {
            "id": "abc123",
            "question": "Will the Fed cut rates in May 2026?",
            "slug": "will-the-fed-cut-rates-may-2026",
            "creatorUsername": "forecaster1",
            "probability": 0.38,
            "outcomeType": "BINARY",
            "uniqueBettorCount": 42,
            "volume": 15000,
        },
        {
            "id": "def456",
            "question": "Will inflation drop below 2% by end of 2026?",
            "slug": "will-inflation-drop-below-2-2026",
            "creatorUsername": "forecaster2",
            "probability": 0.25,
            "outcomeType": "BINARY",
            "uniqueBettorCount": 18,
            "volume": 5000,
        },
    ]


@pytest.fixture
def non_binary_response():
    """Response containing non-binary and low-bettor markets."""
    return [
        {
            "id": "multi1",
            "question": "Who will win?",
            "slug": "who-will-win",
            "creatorUsername": "user1",
            "probability": None,
            "outcomeType": "MULTIPLE_CHOICE",
            "uniqueBettorCount": 50,
            "volume": 10000,
        },
        {
            "id": "low1",
            "question": "Will X happen?",
            "slug": "will-x-happen",
            "creatorUsername": "user2",
            "probability": 0.50,
            "outcomeType": "BINARY",
            "uniqueBettorCount": 2,  # Below MIN_BETTORS
            "volume": 100,
        },
    ]


# ──────────────────────────────────────
# search_markets tests
# ──────────────────────────────────────


class TestSearchMarkets:
    """Tests for ManifoldClient.search_markets."""

    @pytest.mark.asyncio
    async def test_returns_matching_markets(self, manifold_client, sample_manifold_response):
        mock_response = MagicMock()
        mock_response.json.return_value = sample_manifold_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("Fed rate cut")

        assert len(results) == 2
        assert results[0]["title"] == "Will the Fed cut rates in May 2026?"
        assert results[0]["community_prediction"] == 0.38
        assert results[0]["forecasters_count"] == 42
        assert "manifold.markets" in results[0]["url"]

    @pytest.mark.asyncio
    async def test_empty_api_response(self, manifold_client):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("nonexistent query xyz")

        assert results == []

    @pytest.mark.asyncio
    async def test_filters_non_binary_markets(self, manifold_client, non_binary_response):
        mock_response = MagicMock()
        mock_response.json.return_value = non_binary_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("test query")

        # Non-binary (no probability) and low-bettor markets should be filtered
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self, manifold_client):
        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.TimeoutException("Connection timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self, manifold_client):
        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "Server error", request=MagicMock(), response=MagicMock(status_code=500)
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_caches_results(self, manifold_client, sample_manifold_response):
        mock_response = MagicMock()
        mock_response.json.return_value = sample_manifold_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # First call hits API
            results1 = await manifold_client.search_markets("Fed rate cut")
            # Second call should use cache
            results2 = await manifold_client.search_markets("Fed rate cut")

        assert results1 == results2
        # AsyncClient context manager entered only once (cached second time)
        assert mock_client_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_probability_extraction(self, manifold_client, sample_manifold_response):
        mock_response = MagicMock()
        mock_response.json.return_value = sample_manifold_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await manifold_client.search_markets("Fed")

        assert results[0]["community_prediction"] == 0.38
        assert results[1]["community_prediction"] == 0.25
        assert isinstance(results[0]["community_prediction"], float)


# ──────────────────────────────────────
# get_best_match tests
# ──────────────────────────────────────


class TestGetBestMatch:
    """Tests for ManifoldClient.get_best_match."""

    @pytest.mark.asyncio
    async def test_returns_best_matching_market(self, manifold_client, sample_manifold_response):
        mock_response = MagicMock()
        mock_response.json.return_value = sample_manifold_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            match = await manifold_client.get_best_match(
                "Will the Fed cut rates in May 2026?"
            )

        assert match is not None
        assert match["title"] == "Will the Fed cut rates in May 2026?"
        assert "similarity" in match
        assert match["similarity"] >= MIN_SIMILARITY_THRESHOLD

    @pytest.mark.asyncio
    async def test_returns_none_for_no_results(self, manifold_client):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            match = await manifold_client.get_best_match("Completely unrelated question xyz?")

        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_below_similarity_threshold(self, manifold_client):
        """When no result exceeds the similarity threshold, returns None."""
        low_similarity_response = [
            {
                "id": "xxx",
                "question": "Completely different topic about cooking recipes",
                "slug": "cooking-recipes",
                "creatorUsername": "chef",
                "probability": 0.60,
                "outcomeType": "BINARY",
                "uniqueBettorCount": 20,
                "volume": 5000,
            },
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = low_similarity_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            match = await manifold_client.get_best_match(
                "Will the Federal Reserve cut interest rates?"
            )

        assert match is None


# ──────────────────────────────────────
# get_context tests
# ──────────────────────────────────────


class TestGetContext:
    """Tests for ManifoldClient.get_context."""

    @pytest.mark.asyncio
    async def test_returns_formatted_context(self, manifold_client, sample_manifold_response):
        mock_response = MagicMock()
        mock_response.json.return_value = sample_manifold_response
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ctx = await manifold_client.get_context(
                "Will the Fed cut rates in May 2026?"
            )

        assert "MANIFOLD MARKETS" in ctx
        assert "Community prediction" in ctx

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_no_match(self, manifold_client):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.manifold_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ctx = await manifold_client.get_context("Totally unrelated xyz?")

        assert ctx == ""


# ──────────────────────────────────────
# _calculate_similarity tests
# ──────────────────────────────────────


class TestCalculateSimilarity:
    """Tests for keyword overlap similarity calculation."""

    def test_identical_questions(self, manifold_client):
        sim = manifold_client._calculate_similarity(
            "Will the Fed cut rates?",
            "Will the Fed cut rates?",
        )
        assert sim == 1.0

    def test_no_overlap(self, manifold_client):
        sim = manifold_client._calculate_similarity(
            "Will the Fed cut rates?",
            "Cooking recipes for pasta dinner",
        )
        assert sim == 0.0

    def test_partial_overlap(self, manifold_client):
        sim = manifold_client._calculate_similarity(
            "Will the Fed cut rates in May?",
            "Will the Fed raise rates in June?",
        )
        assert 0.0 < sim < 1.0

    def test_empty_strings(self, manifold_client):
        sim = manifold_client._calculate_similarity("", "")
        assert sim == 0.0

    def test_short_words_ignored(self, manifold_client):
        # Words < 3 chars are filtered out by the regex
        sim = manifold_client._calculate_similarity("a b c", "a b c")
        assert sim == 0.0
