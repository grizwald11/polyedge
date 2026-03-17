"""Tests for AI probability strategy."""

from unittest.mock import AsyncMock, patch
import json

import pytest

from src.strategies.ai_probability import AIProbabilityStrategy
from src.analysis.claude_forecaster import ClaudeForecaster
from src.config import Settings
from src.core.models import (
    Market, MarketToken, MarketCategory, ForecastResult, Signal, Direction, StrategyName,
)


@pytest.fixture
def strategy_settings() -> Settings:
    s = Settings()
    s.anthropic_api_key = "test-key"
    return s


@pytest.fixture
def mock_forecaster(strategy_settings) -> ClaudeForecaster:
    return ClaudeForecaster(strategy_settings)


@pytest.fixture
def strategy(mock_forecaster, strategy_settings) -> AIProbabilityStrategy:
    return AIProbabilityStrategy(mock_forecaster, strategy_settings)


def _make_forecast(prob: float) -> ForecastResult:
    return ForecastResult(
        probability=prob,
        confidence_low=max(0, prob - 0.1),
        confidence_high=min(1, prob + 0.1),
        key_factors_for=["Factor A"],
        key_factors_against=["Factor B"],
        reasoning="Test reasoning",
        model_used="claude-sonnet-4-6",
        tokens_used=500,
        latency_ms=1000,
    )


class TestAIProbabilityStrategy:
    @pytest.mark.asyncio
    async def test_generates_buy_yes_signal(self, strategy, sample_market):
        # Claude thinks probability is much higher than market
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)  # Market at 0.34
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_YES
        assert signals[0].edge > 0.05
        assert signals[0].strategy == StrategyName.AI_PROBABILITY

    @pytest.mark.asyncio
    async def test_generates_buy_no_signal(self, strategy, sample_market):
        # Claude thinks probability is much lower than market
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.15)  # Market at 0.34
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_NO

    @pytest.mark.asyncio
    async def test_no_signal_below_min_edge(self, strategy, sample_market):
        # Claude agrees with market (no edge)
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.35)  # Market at 0.34, ~1% edge after ensemble
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_respects_max_assessments(self, strategy):
        strategy.settings.claude.max_assessments_per_cycle = 2
        markets = [
            Market(
                ticker=f"M-{i}",
                question=f"Market {i}?",
                tokens=[
                    MarketToken(token_id=f"M-{i}_yes", outcome="Yes", price=0.50),
                    MarketToken(token_id=f"M-{i}_no", outcome="No", price=0.50),
                ],
                volume_24h=100000,
                active=True,
            )
            for i in range(5)
        ]

        call_count = 0

        async def mock_assess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_forecast(0.50)  # No edge

        strategy.forecaster.assess_market = mock_assess

        await strategy.scan_for_opportunities(markets)

        assert call_count == 2  # Only assessed 2 of 5

    @pytest.mark.asyncio
    async def test_handles_assessment_error(self, strategy, sample_market):
        strategy.forecaster.assess_market = AsyncMock(
            side_effect=Exception("API Error")
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0  # Error handled, no crash

    @pytest.mark.asyncio
    async def test_signal_includes_market_info(self, strategy, sample_market):
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
        assert signals[0].market_id == sample_market.ticker
        assert signals[0].market_question == sample_market.question
