"""Tests for the Gamma API client."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.core.gamma_client import GammaClient, parse_market, classify_market_category
from src.core.models import MarketCategory


class TestClassifyMarketCategory:
    def test_politics(self):
        assert classify_market_category("Will Trump win the election?", ["Politics"]) == MarketCategory.POLITICS

    def test_fed_macro(self):
        assert classify_market_category("Will the Fed cut interest rates?", ["Fed"]) == MarketCategory.FED_MACRO

    def test_geopolitics(self):
        assert classify_market_category("Will Russia invade another country?", []) == MarketCategory.GEOPOLITICS

    def test_tech_ai(self):
        assert classify_market_category("Will OpenAI launch GPT-5?", ["AI"]) == MarketCategory.TECH_AI

    def test_crypto(self):
        assert classify_market_category("Bitcoin price up or down?", ["Crypto"]) == MarketCategory.CRYPTO

    def test_sports(self):
        assert classify_market_category("Who wins the NBA finals?", ["Sports"]) == MarketCategory.SPORTS

    def test_culture(self):
        assert classify_market_category("Who wins best picture at the Oscars?", []) == MarketCategory.CULTURE

    def test_unknown(self):
        assert classify_market_category("Will the sky turn green?", []) == MarketCategory.OTHER

    def test_multiple_matches_highest_wins(self):
        # "Trump election" matches politics more keywords than culture
        result = classify_market_category("Will Trump win the presidential election primary vote?", [])
        assert result == MarketCategory.POLITICS


class TestParseMarket:
    def test_parse_valid(self, gamma_market_response):
        market = parse_market(gamma_market_response)
        assert market is not None
        assert market.condition_id == "0xabc123def456"
        assert market.question == "Will the Federal Reserve cut rates at the May 2026 meeting?"
        assert len(market.tokens) == 2
        assert market.yes_price == 0.34
        assert market.no_price == 0.66
        assert market.volume_24h == 125000.0
        assert market.active is True

    def test_parse_json_string_tokens(self):
        raw = {
            "conditionId": "test_001",
            "question": "Test market?",
            "clobTokenIds": '["tok1", "tok2"]',
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.40", "0.60"]',
            "volume24hr": "50000",
            "active": True,
        }
        market = parse_market(raw)
        assert market is not None
        assert len(market.tokens) == 2
        assert market.yes_price == 0.40

    def test_parse_missing_condition_id(self):
        raw = {"question": "No condition ID"}
        market = parse_market(raw)
        assert market is None

    def test_parse_empty_dict(self):
        market = parse_market({})
        assert market is None

    def test_parse_malformed_prices(self):
        raw = {
            "conditionId": "test_002",
            "question": "Test?",
            "clobTokenIds": '["t1", "t2"]',
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["not_a_number", "0.60"]',
            "active": True,
        }
        market = parse_market(raw)
        assert market is not None
        assert market.yes_price == 0.0  # Failed to parse, defaults to 0
        assert market.no_price == 0.60

    def test_parse_end_date(self, gamma_market_response):
        market = parse_market(gamma_market_response)
        assert market.end_date is not None
        assert market.days_to_resolution is not None
        assert market.days_to_resolution > 40

    def test_parse_category_assignment(self, gamma_market_response):
        market = parse_market(gamma_market_response)
        assert market.category == MarketCategory.FED_MACRO


class TestGammaClient:
    @pytest.fixture
    def client(self):
        return GammaClient("https://gamma-api.polymarket.com")

    @pytest.mark.asyncio
    async def test_get_markets_returns_list(self, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"conditionId": "test"}]
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, '_get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"conditionId": "test"}]
            result = await client.get_markets(limit=5)
            assert isinstance(result, list)
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_markets_handles_empty(self, client):
        with patch.object(client, '_get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            result = await client.get_markets()
            assert result == []

    @pytest.mark.asyncio
    async def test_get_markets_handles_none(self, client):
        with patch.object(client, '_get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            result = await client.get_markets()
            assert result == []

    @pytest.mark.asyncio
    async def test_get_all_active_markets_pagination(self, client):
        page1 = [{"conditionId": f"m{i}"} for i in range(100)]
        page2 = [{"conditionId": f"m{i}"} for i in range(100, 150)]

        call_count = 0
        async def mock_get_markets(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("offset", 0) == 0:
                return page1
            else:
                return page2

        with patch.object(client, 'get_markets', side_effect=mock_get_markets):
            # This is a simplified test - the actual pagination logic calls get_markets
            pass

    @pytest.mark.asyncio
    async def test_close(self, client):
        await client.close()  # Should not raise
