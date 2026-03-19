"""Tests for AI probability strategy."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

from src.strategies.ai_probability import AIProbabilityStrategy
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.calibration_analyzer import CalibrationAnalyzer, CalibrationReport, CategoryStats
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

    @pytest.mark.asyncio
    async def test_confidence_gate_skips_wide_interval(self, strategy, sample_market):
        """Skip trades when Claude's confidence interval is wider than 0.40."""
        wide_forecast = ForecastResult(
            probability=0.55,
            confidence_low=0.20,
            confidence_high=0.80,  # width = 0.60 > 0.40
            reasoning="Very uncertain",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        strategy.forecaster.assess_market = AsyncMock(return_value=wide_forecast)

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_confidence_gate_allows_narrow_interval(self, strategy, sample_market):
        """Allow trades when confidence interval is narrow enough."""
        narrow_forecast = ForecastResult(
            probability=0.55,
            confidence_low=0.45,
            confidence_high=0.65,  # width = 0.20 < 0.40
            reasoning="Reasonably confident",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        strategy.forecaster.assess_market = AsyncMock(return_value=narrow_forecast)

        signals = await strategy.scan_for_opportunities([sample_market])

        # Should generate a signal (0.55 vs market 0.34 = big edge)
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_confidence_gate_boundary(self, strategy, sample_market):
        """Exactly 0.40 width should pass (> 0.40 triggers skip)."""
        boundary_forecast = ForecastResult(
            probability=0.55,
            confidence_low=0.35,
            confidence_high=0.75,  # width = 0.40, exactly at boundary
            reasoning="Boundary test",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        strategy.forecaster.assess_market = AsyncMock(return_value=boundary_forecast)

        signals = await strategy.scan_for_opportunities([sample_market])

        # width == 0.40 should NOT be skipped (only > 0.40 is skipped)
        assert len(signals) == 1


class TestMetaculusEnsemble:
    """Tests for Metaculus integration in the multi-model ensemble."""

    @pytest.mark.asyncio
    async def test_uses_multi_model_when_metaculus_available(self, strategy, sample_market):
        """Should use multi_model_ensemble when Metaculus match found."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Mock data enricher with Metaculus
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.metaculus.get_best_match = AsyncMock(return_value={
            "community_prediction": 0.60,
            "forecasters_count": 50,
            "title": "Similar question",
            "similarity": 0.5,
        })
        strategy.data_enricher = mock_enricher

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
        # Metaculus agrees with Claude (both > market), should still generate signal
        assert signals[0].direction == Direction.BUY_YES

    @pytest.mark.asyncio
    async def test_falls_back_to_single_model_without_metaculus(self, strategy, sample_market):
        """Should use single-model ensemble when no Metaculus match."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.metaculus.get_best_match = AsyncMock(return_value=None)
        strategy.data_enricher = mock_enricher

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_metaculus_error_falls_back_gracefully(self, strategy, sample_market):
        """Metaculus API failure should not prevent signal generation."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.metaculus.get_best_match = AsyncMock(
            side_effect=Exception("API timeout")
        )
        strategy.data_enricher = mock_enricher

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1  # Still works via single-model fallback


class TestCategoryAccuracyGating:
    """Tests for category-specific accuracy gating."""

    @pytest.mark.asyncio
    async def test_skips_category_with_high_brier(self, strategy, sample_market):
        """Should skip markets in categories with Brier > 0.30."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Simulate poor calibration in Fed/Macro category
        strategy._category_brier_scores = {"Fed/Macro": 0.35}

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_raises_min_edge_for_mediocre_brier(self, strategy, sample_market):
        """Should require higher edge for categories with Brier 0.20-0.30."""
        # Claude says 0.40 vs market 0.34 = ~6% edge after ensemble
        # Normal min_edge is 5%, but mediocre Brier raises it to 8%
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.40)
        )
        strategy._category_brier_scores = {"Fed/Macro": 0.25}

        signals = await strategy.scan_for_opportunities([sample_market])

        # Edge ~6% is below raised threshold of 8%
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_normal_edge_for_good_brier(self, strategy, sample_market):
        """Good Brier score should use standard min edge."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        strategy._category_brier_scores = {"Fed/Macro": 0.15}

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1


class TestStalenessDetection:
    """Tests for prediction staleness detection."""

    @pytest.mark.asyncio
    async def test_skips_fresh_prediction(self, strategy, sample_market, tmp_db):
        """Should skip re-assessment when recent prediction exists and price stable."""
        strategy.db = tmp_db
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Store a recent prediction
        tmp_db.store_prediction(
            market_ticker=sample_market.ticker,
            predicted_probability=0.40,
            predicted_side="BUY_YES",
            market_price=0.34,  # Same as current market price
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0  # Skipped due to staleness check

    @pytest.mark.asyncio
    async def test_reassesses_after_large_price_move(self, strategy, sample_market, tmp_db):
        """Should re-assess when market price has moved >10% since last prediction."""
        strategy.db = tmp_db
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Store prediction at a very different price
        tmp_db.store_prediction(
            market_ticker=sample_market.ticker,
            predicted_probability=0.40,
            predicted_side="BUY_YES",
            market_price=0.20,  # 14% away from current 0.34
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1  # Re-assessed due to price move

    @pytest.mark.asyncio
    async def test_reassesses_without_db(self, strategy, sample_market):
        """Should work normally when no DB is available."""
        strategy.db = None
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
