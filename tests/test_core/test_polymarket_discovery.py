"""Tests for Polymarket discovery and market parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Market, MarketCategory, Platform, TokenOutcome
from src.core.polymarket_discovery import (
    POLYMARKET_TAG_MAP,
    PolymarketDiscovery,
    _parse_outcome_prices,
    parse_polymarket_market,
)

# --- Fixtures ---

def _make_raw_market(**overrides) -> dict:
    """Build a raw Gamma API market dict with sensible defaults."""
    base = {
        "conditionId": "0xabc123",
        "question": "Will the Fed cut rates in June 2026?",
        "description": "Resolves YES if FOMC cuts.",
        "slug": "fed-rate-cut-june-2026",
        "active": True,
        "closed": False,
        "outcomePrices": json.dumps(["0.62", "0.38"]),
        "volume": "150000",
        "volume24hr": "25000",
        "liquidity": "12000",
        "endDate": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "tags": ["Fed", "Economics"],
        "clobTokenIds": json.dumps(["clob_yes_123", "clob_no_456"]),
    }
    base.update(overrides)
    return base


class TestParseOutcomePrices:
    def test_json_string(self):
        raw = {"outcomePrices": '["0.65","0.35"]'}
        yes, no = _parse_outcome_prices(raw)
        assert yes == pytest.approx(0.65)
        assert no == pytest.approx(0.35)

    def test_list_input(self):
        raw = {"outcomePrices": ["0.70", "0.30"]}
        yes, no = _parse_outcome_prices(raw)
        assert yes == pytest.approx(0.70)
        assert no == pytest.approx(0.30)

    def test_missing_returns_zeros(self):
        raw = {}
        yes, no = _parse_outcome_prices(raw)
        assert yes == 0.0
        assert no == 0.0

    def test_single_element_infers_no(self):
        raw = {"outcomePrices": '["0.50"]'}
        yes, no = _parse_outcome_prices(raw)
        assert yes == pytest.approx(0.50)
        assert no == pytest.approx(0.50)

    def test_malformed_json(self):
        raw = {"outcomePrices": "not-json"}
        yes, no = _parse_outcome_prices(raw)
        assert yes == 0.0
        assert no == 0.0


class TestParsePolymarketMarket:
    def test_basic_parsing(self):
        raw = _make_raw_market()
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.platform == Platform.POLYMARKET
        assert market.ticker == "0xabc123"
        assert market.question == "Will the Fed cut rates in June 2026?"
        assert market.active is True
        assert market.closed is False
        assert market.yes_price == pytest.approx(0.62)
        assert market.no_price == pytest.approx(0.38)

    def test_tokens_parsed(self):
        raw = _make_raw_market()
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.yes_token is not None
        assert market.no_token is not None
        assert market.yes_token.token_id == "clob_yes_123"
        assert market.no_token.token_id == "clob_no_456"

    def test_volume_parsing(self):
        raw = _make_raw_market(volume="500000", volume24hr="75000")
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.volume_total == 500000.0
        assert market.volume_24h == 75000.0

    def test_missing_condition_id_returns_none(self):
        raw = _make_raw_market()
        del raw["conditionId"]
        market = parse_polymarket_market(raw)
        assert market is None

    def test_category_classification(self):
        raw = _make_raw_market(tags=["Elections", "US Politics"])
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.category == MarketCategory.POLITICS

    def test_no_prices_returns_none(self):
        """Markets with no price data at all are rejected."""
        raw = _make_raw_market()
        del raw["outcomePrices"]
        market = parse_polymarket_market(raw)
        assert market is None

    def test_end_date_parsing(self):
        future = datetime.now(timezone.utc) + timedelta(days=60)
        raw = _make_raw_market(endDate=future.isoformat())
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.end_date is not None
        assert market.days_to_resolution is not None
        assert 59 <= market.days_to_resolution <= 61

    def test_closed_market(self):
        raw = _make_raw_market(closed=True, active=False)
        market = parse_polymarket_market(raw)
        assert market is not None
        assert market.closed is True
        assert market.active is False


class TestPolymarketTagMap:
    def test_has_expected_categories(self):
        # Ensure our tag map covers key Polymarket categories
        keys = set(POLYMARKET_TAG_MAP.keys())
        assert "Elections" in keys or "Politics" in keys
        assert "Fed" in keys or "Economics" in keys
