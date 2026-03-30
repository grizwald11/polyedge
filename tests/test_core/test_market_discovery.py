"""Tests for market discovery (Kalshi API client wrapper)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import (
    MarketDiscovery,
    classify_market_category,
    parse_market,
)
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
        assert market.yes_price == 0.34  # midpoint of 0.33 and 0.35
        assert market.no_price == 0.66
        assert market.volume_24h == 125000.0
        assert market.active is True

    def test_parse_uses_last_price(self):
        raw = {
            "ticker": "TEST-001",
            "title": "Test market?",
            "last_price_dollars": "0.40",
            "volume_24h_fp": "50000.00",
            "status": "active",
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
            "yes_bid_dollars": "0.30",
            "yes_ask_dollars": "0.40",
            "last_price_dollars": "0.00",
            "status": "active",
        }
        market = parse_market(raw)
        assert market is not None
        # Midpoint of 0.30-0.40 = 0.35
        assert market.yes_price == 0.35
        assert market.no_price == 0.65

    def test_parse_end_date(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        assert market.end_date is not None
        assert market.days_to_resolution is not None
        assert market.days_to_resolution > 40

    def test_parse_category_assignment(self, kalshi_market_response):
        # Event category "Economics" maps to FED_MACRO via KALSHI_CATEGORY_MAP
        market = parse_market(kalshi_market_response, event_category="Economics")
        assert market.category == MarketCategory.FED_MACRO

    def test_parse_spread(self, kalshi_market_response):
        market = parse_market(kalshi_market_response)
        # yes_ask_dollars (0.35) - yes_bid_dollars (0.33) = 0.02
        assert market.spread == 0.02

    def test_parse_closed_market(self):
        raw = {
            "ticker": "CLOSED-001",
            "title": "Closed market",
            "last_price_dollars": "0.99",
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
        """Events-based discovery returns nested markets from target categories."""
        mock_response = {
            "events": [
                {
                    "category": "Politics",
                    "markets": [{"ticker": "TEST-001"}, {"ticker": "TEST-002"}],
                },
            ],
            "cursor": None,
        }
        with patch.object(discovery.kalshi, '_request', new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response
            result = await discovery.get_all_active_markets()
            assert len(result) == 2
            assert result[0]["_event_category"] == "Politics"

    @pytest.mark.asyncio
    async def test_get_all_active_markets_pagination(self, discovery):
        """Events-based discovery paginates correctly."""
        page1 = {
            "events": [
                {
                    "category": "Politics",
                    "markets": [{"ticker": f"M-{i}"} for i in range(200)],
                },
            ],
            "cursor": "next_page",
        }
        page2 = {
            "events": [
                {
                    "category": "Economics",
                    "markets": [{"ticker": f"M-{i}"} for i in range(200, 250)],
                },
            ],
            "cursor": None,
        }
        call_count = 0

        async def mock_request(method, path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return page2

        with patch.object(discovery.kalshi, '_request', side_effect=mock_request):
            result = await discovery.get_all_active_markets()
            assert len(result) == 250

    @pytest.mark.asyncio
    async def test_get_all_active_markets_empty(self, discovery):
        with patch.object(discovery.kalshi, '_request', new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"events": [], "cursor": None}
            result = await discovery.get_all_active_markets()
            assert result == []

    @pytest.mark.asyncio
    async def test_get_all_active_markets_filters_non_target_categories(self, discovery):
        """Non-target categories like 'Health' are filtered out."""
        mock_response = {
            "events": [
                {
                    "category": "Health",
                    "markets": [{"ticker": "HEALTH-001"}],
                },
                {
                    "category": "Politics",
                    "markets": [{"ticker": "POL-001"}],
                },
            ],
            "cursor": None,
        }
        with patch.object(discovery.kalshi, '_request', new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response
            result = await discovery.get_all_active_markets()
            # Only Politics is in TARGET_EVENT_CATEGORIES
            assert len(result) == 1
            assert result[0]["ticker"] == "POL-001"

    @pytest.mark.asyncio
    async def test_close(self, discovery):
        with patch.object(discovery.kalshi, 'close', new_callable=AsyncMock):
            await discovery.close()  # Should not raise
