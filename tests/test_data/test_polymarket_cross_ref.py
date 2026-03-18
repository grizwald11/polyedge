"""Tests for the Polymarket cross-reference client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.polymarket_cross_ref import PolymarketCrossRef


class TestSearchMarkets:
    @pytest.mark.asyncio
    async def test_successful_search(self):
        client = PolymarketCrossRef()
        mock_response_data = [
            {
                "question": "Will the Fed cut rates in May 2026?",
                "outcomePrices": '[0.38, 0.62]',
                "volume": 2300000,
                "conditionId": "0xabc",
            },
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.polymarket_cross_ref.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_markets("Fed rate cut")

        assert len(results) == 1
        assert results[0]["question"] == "Will the Fed cut rates in May 2026?"
        assert results[0]["yes_price"] == 0.38
        assert results[0]["volume"] == 2300000

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self):
        client = PolymarketCrossRef()

        with patch("src.data.polymarket_cross_ref.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_markets("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_skips_markets_without_question(self):
        client = PolymarketCrossRef()
        mock_response_data = [{"question": "", "outcomePrices": "[0.5, 0.5]"}]

        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_response.raise_for_status = MagicMock()

        with patch("src.data.polymarket_cross_ref.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            results = await client.search_markets("test")

        assert results == []


class TestGetContext:
    @pytest.mark.asyncio
    async def test_formats_context_with_discrepancy(self):
        client = PolymarketCrossRef()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "question": "Will the Fed cut rates in May 2026?",
                "yes_price": 0.38,
                "volume": 2300000,
                "similarity": 0.85,
            }
            context = await client.get_context(
                "Will the Fed cut rates in May 2026?",
                kalshi_yes_price=0.34,
            )

        assert "CROSS-PLATFORM PRICE CHECK" in context
        assert "Polymarket YES: 38%" in context
        assert "Kalshi YES: 34%" in context
        assert "+4% discrepancy" in context
        assert "$2.3M" in context

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        client = PolymarketCrossRef()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = None
            context = await client.get_context("test", kalshi_yes_price=0.5)

        assert context == ""

    @pytest.mark.asyncio
    async def test_no_price_returns_empty(self):
        client = PolymarketCrossRef()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "question": "Test market",
                "yes_price": None,
                "volume": 1000,
                "similarity": 0.8,
            }
            context = await client.get_context("test", kalshi_yes_price=0.5)

        assert context == ""

    @pytest.mark.asyncio
    async def test_formats_volume_thousands(self):
        client = PolymarketCrossRef()

        with patch.object(client, "get_best_match", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "question": "Test",
                "yes_price": 0.50,
                "volume": 50000,
                "similarity": 0.8,
            }
            context = await client.get_context("Test", kalshi_yes_price=0.50)

        assert "$50K" in context
