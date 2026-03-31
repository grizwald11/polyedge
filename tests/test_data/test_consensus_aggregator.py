"""Tests for the Cross-Platform Consensus Aggregator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Market, MarketToken, Platform
from src.data.consensus_aggregator import ConsensusAggregator


def _make_market(platform: Platform = Platform.KALSHI) -> Market:
    return Market(
        ticker="TEST-MKT",
        platform=platform,
        question="Will the Fed cut rates at the May 2026 meeting?",
        description="Resolves YES if the FOMC announces a rate cut.",
        tokens=[
            MarketToken(token_id="TEST_yes", outcome="Yes", price=0.45),
            MarketToken(token_id="TEST_no", outcome="No", price=0.55),
        ],
        volume_24h=100000,
    )


@pytest.mark.asyncio
async def test_all_sources_available():
    """Test aggregation when all three sources return matches."""
    manifold = MagicMock()
    manifold.get_best_match = AsyncMock(return_value={
        "community_prediction": 0.42,
        "forecasters_count": 80,
    })

    metaculus = MagicMock()
    metaculus.get_best_match = AsyncMock(return_value={
        "community_prediction": 0.38,
        "forecasters_count": 50,
    })

    polymarket = MagicMock()
    polymarket.get_best_match = AsyncMock(return_value={
        "yes_price": 0.40,
        "volume": 500000,
        "similarity": 0.85,
        "question": "Will the Fed cut rates?",
    })

    aggregator = ConsensusAggregator(
        manifold_client=manifold,
        metaculus_client=metaculus,
        polymarket_cross_ref=polymarket,
    )

    market = _make_market(Platform.KALSHI)
    forecasts = await aggregator.get_all_forecasts(market)

    assert len(forecasts) == 3
    models = {f.model_used for f in forecasts}
    assert "polymarket_price" in models
    assert "manifold_community" in models
    assert "metaculus_community" in models


@pytest.mark.asyncio
async def test_polymarket_only_for_kalshi():
    """Polymarket cross-ref should only be used when trading on Kalshi."""
    polymarket = MagicMock()
    polymarket.get_best_match = AsyncMock(return_value={
        "yes_price": 0.40,
        "volume": 500000,
        "similarity": 0.85,
        "question": "Will the Fed cut rates?",
    })

    aggregator = ConsensusAggregator(polymarket_cross_ref=polymarket)

    # Kalshi market → should fetch Polymarket
    kalshi_market = _make_market(Platform.KALSHI)
    forecasts = await aggregator.get_all_forecasts(kalshi_market)
    assert len(forecasts) == 1
    assert forecasts[0].model_used == "polymarket_price"

    # Polymarket market → should NOT fetch Polymarket cross-ref
    poly_market = _make_market(Platform.POLYMARKET)
    forecasts = await aggregator.get_all_forecasts(poly_market)
    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_source_failure_graceful():
    """One source failing shouldn't prevent others from returning."""
    manifold = MagicMock()
    manifold.get_best_match = AsyncMock(return_value={
        "community_prediction": 0.50,
        "forecasters_count": 30,
    })

    metaculus = MagicMock()
    metaculus.get_best_match = AsyncMock(side_effect=RuntimeError("API down"))

    aggregator = ConsensusAggregator(
        manifold_client=manifold,
        metaculus_client=metaculus,
    )

    market = _make_market()
    forecasts = await aggregator.get_all_forecasts(market)

    assert len(forecasts) == 1
    assert forecasts[0].model_used == "manifold_community"


@pytest.mark.asyncio
async def test_no_match_returns_empty():
    """When no sources find a match, return empty list."""
    manifold = MagicMock()
    manifold.get_best_match = AsyncMock(return_value=None)

    metaculus = MagicMock()
    metaculus.get_best_match = AsyncMock(return_value=None)

    aggregator = ConsensusAggregator(
        manifold_client=manifold,
        metaculus_client=metaculus,
    )

    market = _make_market()
    forecasts = await aggregator.get_all_forecasts(market)
    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_no_clients_returns_empty():
    """When no clients are configured, return empty list."""
    aggregator = ConsensusAggregator()
    market = _make_market()
    forecasts = await aggregator.get_all_forecasts(market)
    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_polymarket_confidence_intervals():
    """Verify CI narrows with higher volume."""
    polymarket = MagicMock()

    # High volume market
    polymarket.get_best_match = AsyncMock(return_value={
        "yes_price": 0.50,
        "volume": 2_000_000,
        "similarity": 0.90,
        "question": "Test",
    })

    aggregator = ConsensusAggregator(polymarket_cross_ref=polymarket)
    market = _make_market()
    forecasts = await aggregator.get_all_forecasts(market)
    high_vol = forecasts[0]
    high_vol_ci = high_vol.confidence_high - high_vol.confidence_low

    # Low volume market
    polymarket.get_best_match = AsyncMock(return_value={
        "yes_price": 0.50,
        "volume": 5_000,
        "similarity": 0.90,
        "question": "Test",
    })

    forecasts = await aggregator.get_all_forecasts(market)
    low_vol = forecasts[0]
    low_vol_ci = low_vol.confidence_high - low_vol.confidence_low

    assert high_vol_ci < low_vol_ci


@pytest.mark.asyncio
async def test_manifold_forecast_fields():
    """Verify Manifold forecast has correct structure."""
    manifold = MagicMock()
    manifold.get_best_match = AsyncMock(return_value={
        "community_prediction": 0.65,
        "forecasters_count": 45,
    })

    aggregator = ConsensusAggregator(manifold_client=manifold)
    market = _make_market()
    forecasts = await aggregator.get_all_forecasts(market)

    assert len(forecasts) == 1
    f = forecasts[0]
    assert f.probability == 0.65
    assert f.model_used == "manifold_community"
    assert 0.01 <= f.confidence_low < f.probability
    assert f.probability < f.confidence_high <= 0.99
    assert "Manifold" in f.reasoning
