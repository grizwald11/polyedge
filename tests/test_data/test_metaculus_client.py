"""Tests for the Metaculus client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.metaculus_client import MetaculusClient


class TestSearchQuestions:
    @pytest.mark.asyncio
    async def test_successful_search(self):
        client = MetaculusClient()
        mock_response_data = {
            "results": [
                {
                    "id": 12345,
                    "title": "Will the Fed cut rates at the May 2026 FOMC?",
                    "community_prediction": 0.42,
                    "number_of_forecasters": 156,
                    "url": "/questions/12345/",
                },
                {
                    "id": 12346,
                    "title": "Will US CPI exceed 3% in March 2026?",
                    "community_prediction": {"full": {"q2": 0.65}},
                    "number_of_forecasters": 89,
                    "url": "/questions/12346/",
                },
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.metaculus_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_questions("Fed rate cut May 2026")

        assert len(results) == 2
        assert results[0]["community_prediction"] == 0.42
        assert results[0]["forecasters_count"] == 156
        assert results[1]["community_prediction"] == 0.65

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self):
        client = MetaculusClient()

        with patch("src.data.metaculus_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_questions("test query")

        assert results == []

    @pytest.mark.asyncio
    async def test_skips_questions_without_prediction(self):
        client = MetaculusClient()
        mock_response_data = {
            "results": [
                {
                    "id": 1,
                    "title": "No prediction",
                    "community_prediction": None,
                    "number_of_forecasters": 0,
                    "url": "/questions/1/",
                },
            ]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.metaculus_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_questions("test")

        assert results == []


class TestCalculateSimilarity:
    def test_identical_questions(self):
        client = MetaculusClient()
        sim = client._calculate_similarity(
            "Will the Fed cut rates?", "Will the Fed cut rates?"
        )
        assert sim == 1.0

    def test_similar_questions(self):
        client = MetaculusClient()
        sim = client._calculate_similarity(
            "Will the Fed cut rates in May 2026?",
            "Will the Federal Reserve cut rates at May FOMC?"
        )
        assert sim > 0.3

    def test_unrelated_questions(self):
        client = MetaculusClient()
        sim = client._calculate_similarity(
            "Will the Fed cut rates?",
            "Who will win the Oscar for Best Picture?"
        )
        assert sim < 0.2

    def test_empty_strings(self):
        client = MetaculusClient()
        assert client._calculate_similarity("", "test") == 0.0
        assert client._calculate_similarity("test", "") == 0.0


class TestGetBestMatch:
    @pytest.mark.asyncio
    async def test_returns_best_match_above_threshold(self):
        client = MetaculusClient()

        with patch.object(client, "search_questions", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {
                    "title": "Will the Fed cut rates at the May 2026 FOMC meeting?",
                    "community_prediction": 0.42,
                    "forecasters_count": 156,
                    "url": "/questions/12345/",
                    "id": 12345,
                },
            ]
            result = await client.get_best_match(
                "Will the Fed cut rates in May 2026?"
            )

        assert result is not None
        assert result["community_prediction"] == 0.42
        assert "similarity" in result

    @pytest.mark.asyncio
    async def test_returns_none_below_threshold(self):
        client = MetaculusClient()

        with patch.object(client, "search_questions", new_callable=AsyncMock) as mock:
            mock.return_value = [
                {
                    "title": "Completely unrelated question about zebras",
                    "community_prediction": 0.5,
                    "forecasters_count": 10,
                    "url": "/questions/99/",
                    "id": 99,
                },
            ]
            result = await client.get_best_match(
                "Will the Fed cut rates in May 2026?"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_results(self):
        client = MetaculusClient()

        with patch.object(client, "search_questions", new_callable=AsyncMock) as mock:
            mock.return_value = []
            result = await client.get_best_match("test question")

        assert result is None


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context(self):
        client = MetaculusClient()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "title": "Will the Fed cut rates at May 2026 FOMC?",
                "community_prediction": 0.42,
                "forecasters_count": 156,
                "similarity": 0.82,
            }
            context = await client.get_context(
                "Will the Fed cut rates in May 2026?"
            )

        assert "METACULUS COMMUNITY FORECAST" in context
        assert "42% YES" in context
        assert "156 forecasters" in context
        assert "82% similarity" in context

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        client = MetaculusClient()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context("test question")

        assert context == ""
