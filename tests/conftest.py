"""Shared test fixtures for PolyEdge test suite."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings, load_settings
from src.core.models import (
    Market, MarketCategory, MarketToken, Signal, StrategyName, Direction,
    Order, Trade, Side, OrderType, OrderStatus, CalibrationRecord,
    MarketSnapshot, ForecastResult,
)
from src.storage.database import Database


@pytest.fixture
def settings() -> Settings:
    """Test settings with safe defaults."""
    return Settings(
        trading=Settings.model_fields["trading"].default_factory()
    )


@pytest.fixture
def tmp_db(tmp_path) -> Database:
    """Create a temporary database for testing."""
    db_path = str(tmp_path / "test.db")
    return Database(db_path=db_path, wal_mode=True)


@pytest.fixture
def sample_market() -> Market:
    """A sample binary market for testing."""
    return Market(
        condition_id="0xabc123def456",
        question="Will the Federal Reserve cut rates at the May 2026 meeting?",
        description="Resolves YES if the FOMC announces a rate cut at the May 2026 meeting.",
        category=MarketCategory.FED_MACRO,
        tags=["Fed", "Interest Rates", "FOMC"],
        tokens=[
            MarketToken(token_id="tok_yes_001", outcome="Yes", price=0.34),
            MarketToken(token_id="tok_no_001", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=125000.0,
        volume_total=3500000.0,
        liquidity=45000.0,
        spread=0.02,
        active=True,
        closed=False,
        resolution_source="https://federalreserve.gov",
        slug="will-fed-cut-rates-may-2026",
    )


@pytest.fixture
def sample_market_politics() -> Market:
    """A politics market for testing."""
    return Market(
        condition_id="0xpol_market_001",
        question="Will Trump win the 2028 Republican primary?",
        description="Resolves YES if Trump wins the 2028 GOP presidential primary.",
        category=MarketCategory.POLITICS,
        tags=["Politics", "Elections", "Trump", "Primaries"],
        tokens=[
            MarketToken(token_id="tok_yes_pol", outcome="Yes", price=0.55),
            MarketToken(token_id="tok_no_pol", outcome="No", price=0.45),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=200),
        volume_24h=500000.0,
        volume_total=15000000.0,
        liquidity=120000.0,
        spread=0.01,
        active=True,
    )


@pytest.fixture
def sample_market_low_volume() -> Market:
    """A low-volume market that should be filtered out."""
    return Market(
        condition_id="0xlow_vol_001",
        question="Will aliens contact Earth in 2026?",
        description="Resolves YES if verified alien contact occurs.",
        category=MarketCategory.OTHER,
        tags=["Culture"],
        tokens=[
            MarketToken(token_id="tok_yes_low", outcome="Yes", price=0.03),
            MarketToken(token_id="tok_no_low", outcome="No", price=0.97),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=300),
        volume_24h=500.0,  # Below min threshold
        volume_total=8000.0,
        liquidity=1000.0,
        active=True,
    )


@pytest.fixture
def sample_market_crypto() -> Market:
    """A crypto price market that should be excluded."""
    return Market(
        condition_id="0xcrypto_001",
        question="Bitcoin up or down in 15 minutes?",
        category=MarketCategory.CRYPTO,
        tags=["Crypto Prices", "BTC"],
        tokens=[
            MarketToken(token_id="tok_yes_btc", outcome="Yes", price=0.50),
            MarketToken(token_id="tok_no_btc", outcome="No", price=0.50),
        ],
        volume_24h=1000000.0,
        active=True,
    )


@pytest.fixture
def sample_signal(sample_market) -> Signal:
    """A sample trading signal."""
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id=sample_market.condition_id,
        market_question=sample_market.question,
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.42,
        market_price=0.34,
        confidence=0.7,
        reasoning="Fed futures pricing in 42% probability, market at 34%. 8% edge.",
    )


@pytest.fixture
def sample_forecast() -> ForecastResult:
    """A sample Claude forecast result."""
    return ForecastResult(
        probability=0.42,
        confidence_low=0.35,
        confidence_high=0.50,
        key_factors_for=["Cooling inflation", "Slowing jobs market"],
        key_factors_against=["Core CPI still elevated", "Fed hawkish rhetoric"],
        uncertainties=["March CPI data not yet released", "Geopolitical risk"],
        reasoning="Based on current economic data, a May rate cut probability is approximately 42%.",
        model_used="claude-sonnet-4-6",
        tokens_used=1200,
        latency_ms=2500,
    )


# ──────────────────────────────────────
# Gamma API mock response fixtures
# ──────────────────────────────────────

@pytest.fixture
def gamma_market_response() -> dict:
    """Raw Gamma API market response for mocking."""
    return {
        "conditionId": "0xabc123def456",
        "question": "Will the Federal Reserve cut rates at the May 2026 meeting?",
        "description": "Resolves YES if the FOMC announces a rate cut.",
        "tags": ["Fed", "Interest Rates"],
        "clobTokenIds": '["tok_yes_001", "tok_no_001"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.34", "0.66"]',
        "endDate": (datetime.now(timezone.utc) + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "volume24hr": "125000",
        "volume": "3500000",
        "liquidity": "45000",
        "spread": "0.02",
        "active": True,
        "closed": False,
        "resolutionSource": "https://federalreserve.gov",
        "slug": "will-fed-cut-rates-may-2026",
        "negRisk": False,
        "eventSlug": "fed-may-2026",
    }


@pytest.fixture
def gamma_markets_response(gamma_market_response) -> list[dict]:
    """Multiple Gamma API market responses."""
    politics = {
        "conditionId": "0xpol_001",
        "question": "Will Trump be the Republican nominee in 2028?",
        "tags": ["Politics", "Elections"],
        "clobTokenIds": '["tok_y_p", "tok_n_p"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.55", "0.45"]',
        "volume24hr": "500000",
        "volume": "15000000",
        "active": True,
        "closed": False,
    }
    low_vol = {
        "conditionId": "0xlow_001",
        "question": "Will aliens make contact in 2026?",
        "tags": ["Culture"],
        "clobTokenIds": '["tok_y_l", "tok_n_l"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.03", "0.97"]',
        "volume24hr": "500",
        "active": True,
        "closed": False,
    }
    crypto = {
        "conditionId": "0xcrypto_001",
        "question": "Bitcoin up or down in 15 minutes?",
        "tags": ["Crypto Prices", "BTC"],
        "clobTokenIds": '["tok_y_c", "tok_n_c"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.50", "0.50"]',
        "volume24hr": "1000000",
        "active": True,
        "closed": False,
    }
    return [gamma_market_response, politics, low_vol, crypto]
