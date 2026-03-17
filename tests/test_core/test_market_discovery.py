"""Tests for market discovery (Kalshi API client wrapper)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.core.market_discovery import MarketDiscovery, parse_market, classify_market_category
from src.core.kalshi_client import KalshiClient
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
        result = classify_market_category("Will Trump win the presidential election primary vote?", [])
        assert result == MarketCategory.POLITICS


class TestParseMarket:
    def test_parse_valid(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        assert market is not None
        assert market.ticker == "FED-RATE-CUT-MAY26"
        assert market.question == "Will the Federal Reserve cut rates at the May 2026 meeting?"
        assert len(market.tokens) == 2
        assert market.yes_price == 0.34
        assert market.no_price == 0.66
        assert market.volume_24h == 125000.0
        assert market.active is True

    def test_parse_uses_last_price(self):
        raw = {
            "ticker": "TEST-001",
            "title": "Test market?",
            "last_price": 40,
            "volume_24h": 50000,
            "status": "open",
        }
        market = parse_market(raw)
        assert market is not None
        assert len(market.tokens) == 2
        assert market.yes_price == 0.40
        assert market.no_price == 0.60

    def test_parse_missing_ticker(self):
        raw = {"title": "No ticker"}
        market = parse_market(raw)
        assert market is None

    def test_parse_empty_dict(self):
        market = parse_market({})
        assert market is None

    def test_parse_bid_ask_midpoint(self):
        raw = {
            "ticker": "TEST-002",
            "title": "Test?",
            "yes_bid": 30,
            "yes_ask": 40,
            "last_price": 0,
            "status": "open",
        }
        market = parse_market(raw)
        assert market is not None
        # Midpoint of 30-40 = 35
        assert market.yes_price == 0.35
        assert market.no_price == 0.65

    def test_parse_end_date(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        assert market.end_date is not None
        assert market.days_to_resolution is not None
        assert market.days_to_resolution > 40

    def test_parse_category_assignment(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        assert market.category == MarketCategory.FED_MACRO

    def test_parse_spread(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        # yes_ask (35) - yes_bid (33) = 2 cents = $0.02
        assert market.spread == 0.02

    def test_parse_closed_market(self):
        raw = {
            "ticker": "CLOSED-001",
            "title": "Closed market",
            "last_price": 99,
            "status": "settled",
            "result": "yes",
        }
        market = parse_market(raw)
        assert market is not None
        assert market.closed is True
        assert market.active is False


class TestMarketDiscovery:
    @pytest.fixture
    def kalshi(self):
        return KalshiClient("https://demo-api.kalshi.co/trade-api/v2")

    @pytest.fixture
    def discovery(self, kalshi):
        return MarketDiscovery(kalshi)

    @pytest.mark.asyncio
    async def test_get_all_active_markets(self, discovery):
        mock_response = {
            "markets": [{"ticker": "TEST-001"}, {"ticker": "TEST-002"}],
            "cursor": None,
        }
        with patch.object(discovery.kalshi, 'get_markets', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await discovery.get_all_active_markets()
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_all_active_markets_pagination(self, discovery):
        page1 = {
            "markets": [{"ticker": f"M-{i}"} for i in range(200)],
            "cursor": "next_page",
        }
        page2 = {
            "markets": [{"ticker": f"M-{i}"} for i in range(200, 250)],
            "cursor": None,
        }
        call_count = 0

        async def mock_get_markets(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return page2

        with patch.object(discovery.kalshi, 'get_markets', side_effect=mock_get_markets):
            result = await discovery.get_all_active_markets()
            assert len(result) == 250

    @pytest.mark.asyncio
    async def test_get_all_active_markets_empty(self, discovery):
        with patch.object(discovery.kalshi, 'get_markets', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"markets": [], "cursor": None}
            result = await discovery.get_all_active_markets()
            assert result == []

    @pytest.mark.asyncio
    async def test_close(self, discovery):
        with patch.object(discovery.kalshi, 'close', new_callable=AsyncMock):
            await discovery.close()  # Should not raise
