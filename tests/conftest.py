"""Shared test fixtures for PolyEdge test suite."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings, load_settings
from src.core.models import (
    CalibrationRecord,
    Direction,
    ForecastResult,
    Market,
    MarketCategory,
    MarketSnapshot,
    MarketToken,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    StrategyName,
    Trade,
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
    """Create a temporary database for testing (FK constraints disabled for isolation)."""
    db_path = str(tmp_path / "test.db")
    db = Database(db_path=db_path, wal_mode=True)
    # Disable FK constraints for test isolation on the persistent connection
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


@pytest.fixture
def sample_market() -> Market:
    """A sample binary market for testing."""
    return Market(
        ticker="FED-RATE-CUT-MAY26",
        question="Will the Federal Reserve cut rates at the May 2026 meeting?",
        description="Resolves YES if the FOMC announces a rate cut at the May 2026 meeting.",
        category=MarketCategory.FED_MACRO,
        tags=["Fed", "Interest Rates", "FOMC"],
        tokens=[
            MarketToken(token_id="FED-RATE-CUT-MAY26_yes", outcome="Yes", price=0.34),
            MarketToken(token_id="FED-RATE-CUT-MAY26_no", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=125000.0,
        volume_total=3500000.0,
        liquidity=45000.0,
        spread=0.02,
        active=True,
        closed=False,
        resolution_source="https://federalreserve.gov",
        slug="FED-RATE-CUT-MAY26",
    )


@pytest.fixture
def sample_market_politics() -> Market:
    """A politics market for testing."""
    return Market(
        ticker="TRUMP-GOP-2028",
        question="Will Trump win the 2028 Republican primary?",
        description="Resolves YES if Trump wins the 2028 GOP presidential primary.",
        category=MarketCategory.POLITICS,
        tags=["Politics", "Elections", "Trump", "Primaries"],
        tokens=[
            MarketToken(token_id="TRUMP-GOP-2028_yes", outcome="Yes", price=0.55),
            MarketToken(token_id="TRUMP-GOP-2028_no", outcome="No", price=0.45),
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
        ticker="ALIENS-2026",
        question="Will aliens contact Earth in 2026?",
        description="Resolves YES if verified alien contact occurs.",
        category=MarketCategory.OTHER,
        tags=["Culture"],
        tokens=[
            MarketToken(token_id="ALIENS-2026_yes", outcome="Yes", price=0.03),
            MarketToken(token_id="ALIENS-2026_no", outcome="No", price=0.97),
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
        ticker="BTC-15MIN",
        question="Bitcoin up or down in 15 minutes?",
        category=MarketCategory.CRYPTO,
        tags=["Crypto Prices", "BTC"],
        tokens=[
            MarketToken(token_id="BTC-15MIN_yes", outcome="Yes", price=0.50),
            MarketToken(token_id="BTC-15MIN_no", outcome="No", price=0.50),
        ],
        volume_24h=1000000.0,
        active=True,
    )


@pytest.fixture
def sample_signal(sample_market) -> Signal:
    """A sample trading signal."""
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id=sample_market.ticker,
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
# Kalshi API mock response fixtures
# ──────────────────────────────────────

@pytest.fixture
def kalshi_market_response() -> dict:
    """Raw Kalshi API market response for mocking (new *_dollars/*_fp format)."""
    return {
        "ticker": "FED-RATE-CUT-MAY26",
        "title": "Will the Federal Reserve cut rates at the May 2026 meeting?",
        "subtitle": "FOMC rate decision",
        "category": "Economics",
        "rules_primary": "Resolves YES if the FOMC announces a rate cut.",
        "yes_bid_dollars": "0.33",
        "yes_ask_dollars": "0.35",
        "no_bid_dollars": "0.65",
        "no_ask_dollars": "0.67",
        "last_price_dollars": "0.34",
        "volume_24h_fp": "125000.00",
        "volume_fp": "3500000.00",
        "open_interest_fp": "45000.00",
        "close_time": (datetime.now(timezone.utc) + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "active",
        "result": "",
        "event_ticker": "FED-MAY-2026",
        "settlement_source_url": "https://federalreserve.gov",
    }


@pytest.fixture
def kalshi_markets_response(kalshi_market_response) -> list[dict]:
    """Multiple Kalshi API market responses (new *_dollars/*_fp format)."""
    politics = {
        "ticker": "TRUMP-GOP-2028",
        "title": "Will Trump be the Republican nominee in 2028?",
        "subtitle": "GOP primary",
        "category": "Politics",
        "yes_bid_dollars": "0.54",
        "yes_ask_dollars": "0.56",
        "last_price_dollars": "0.55",
        "volume_24h_fp": "500000.00",
        "volume_fp": "15000000.00",
        "status": "active",
        "result": "",
    }
    low_vol = {
        "ticker": "ALIENS-2026",
        "title": "Will aliens make contact in 2026?",
        "subtitle": "",
        "category": "Science",
        "yes_bid_dollars": "0.02",
        "yes_ask_dollars": "0.04",
        "last_price_dollars": "0.03",
        "volume_24h_fp": "500.00",
        "volume_fp": "500.00",
        "status": "active",
        "result": "",
    }
    crypto = {
        "ticker": "BTC-15MIN",
        "title": "Bitcoin up or down in 15 minutes?",
        "subtitle": "",
        "category": "Crypto",
        "yes_bid_dollars": "0.49",
        "yes_ask_dollars": "0.51",
        "last_price_dollars": "0.50",
        "volume_24h_fp": "1000000.00",
        "volume_fp": "1000000.00",
        "status": "active",
        "result": "",
    }
    return [kalshi_market_response, politics, low_vol, crypto]
