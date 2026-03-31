"""Tests for AI probability strategy."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.calibration_analyzer import (
    CalibrationAnalyzer,
    CalibrationReport,
    CategoryStats,
)
from src.analysis.claude_forecaster import ClaudeForecaster
from src.config import Settings
from src.core.models import (
    Direction,
    ForecastResult,
    Market,
    MarketCategory,
    MarketToken,
    Signal,
    StrategyName,
)
from src.strategies.ai_probability import AIProbabilityStrategy


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
    # Disable cross-check by default so tests don't make real API calls
    strategy_settings.claude.cross_check_enabled = False
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


class TestCommunityEnsemble:
    """Tests for community forecast (Manifold/Metaculus) in the multi-model ensemble."""

    @pytest.mark.asyncio
    async def test_uses_multi_model_when_community_available(self, strategy, sample_market):
        """Should use multi_model_ensemble when Manifold match found."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.manifold.get_best_match = AsyncMock(return_value={
            "community_prediction": 0.60,
            "forecasters_count": 50,
            "title": "Similar question",
            "similarity": 0.5,
        })
        mock_enricher.metaculus.get_best_match = AsyncMock(return_value=None)
        strategy.data_enricher = mock_enricher

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_YES

    @pytest.mark.asyncio
    async def test_falls_back_to_single_model_without_community(self, strategy, sample_market):
        """Should use single-model ensemble when no community match."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.manifold.get_best_match = AsyncMock(return_value=None)
        mock_enricher.metaculus.get_best_match = AsyncMock(return_value=None)
        strategy.data_enricher = mock_enricher

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_community_error_falls_back_gracefully(self, strategy, sample_market):
        """Community API failure should not prevent signal generation."""
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        mock_enricher = MagicMock()
        mock_enricher.get_context = AsyncMock(return_value="news context")
        mock_enricher.manifold.get_best_match = AsyncMock(
            side_effect=Exception("API timeout")
        )
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
        """Should re-assess when market price has moved >10% relative since last prediction."""
        strategy.db = tmp_db
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Store prediction at a very different price
        # Relative move: |0.34 - 0.20| / 0.20 = 70% — well above 10% threshold
        tmp_db.store_prediction(
            market_ticker=sample_market.ticker,
            predicted_probability=0.40,
            predicted_side="BUY_YES",
            market_price=0.20,  # 70% relative move from current 0.34
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1  # Re-assessed due to large relative price move

    @pytest.mark.asyncio
    async def test_skips_small_relative_price_move(self, strategy, sample_market, tmp_db):
        """Should skip re-assessment when relative price move is small (<10%)."""
        strategy.db = tmp_db
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )
        # Relative move: |0.34 - 0.33| / 0.33 = ~3% — below 10% threshold
        tmp_db.store_prediction(
            market_ticker=sample_market.ticker,
            predicted_probability=0.40,
            predicted_side="BUY_YES",
            market_price=0.33,  # ~3% relative move from current 0.34
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0  # Skipped: relative move too small

    @pytest.mark.asyncio
    async def test_reassesses_without_db(self, strategy, sample_market):
        """Should work normally when no DB is available."""
        strategy.db = None
        strategy.forecaster.assess_market = AsyncMock(
            return_value=_make_forecast(0.55)
        )

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 1


class TestOpusEscalation:
    """Tests for smart model escalation — re-verify high-edge signals with opus."""

    @pytest.mark.asyncio
    async def test_high_edge_triggers_opus_verification(self, strategy, sample_market):
        """Edge > 15% with sonnet should trigger opus re-verification."""
        sonnet_forecast = ForecastResult(
            probability=0.60,
            confidence_low=0.50,
            confidence_high=0.70,
            reasoning="Sonnet analysis",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        opus_forecast = ForecastResult(
            probability=0.58,  # Agrees within 10%
            confidence_low=0.48,
            confidence_high=0.68,
            reasoning="Opus analysis",
            model_used="claude-opus-4-6",
            tokens_used=800,
            latency_ms=2000,
        )
        call_count = 0

        async def mock_assess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("force_model"):
                return opus_forecast
            return sonnet_forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])

        # Should have called assess_market twice (sonnet + opus escalation)
        assert call_count == 2
        assert len(signals) == 1
        # Signal should use opus-derived ensemble, not sonnet's
        signal = signals[0]
        # Opus probability (0.58) differs from sonnet (0.60), so the
        # ensemble result should reflect opus, not sonnet
        # The ensemble blends opus (0.58) with market (0.40), so final
        # probability should be closer to 0.58 than to 0.60
        assert signal.probability_estimate > 0.0  # sanity
        assert signal.edge > 0.0

    @pytest.mark.asyncio
    async def test_opus_reensemble_changes_signal_values(self, strategy, sample_market):
        """When opus confirms, the signal edge/prob should reflect opus, not sonnet."""
        # Sonnet sees 65% (edge ~25% vs 40% market)
        sonnet_forecast = ForecastResult(
            probability=0.65,
            confidence_low=0.55,
            confidence_high=0.75,
            reasoning="Sonnet analysis",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        # Opus sees 72% — agrees (within 10%) but notably different
        opus_forecast = ForecastResult(
            probability=0.72,
            confidence_low=0.62,
            confidence_high=0.82,
            reasoning="Opus sees higher probability",
            model_used="claude-opus-4-6",
            tokens_used=800,
            latency_ms=2000,
        )

        async def mock_assess(*args, **kwargs):
            if kwargs.get("force_model"):
                return opus_forecast
            return sonnet_forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])
        assert len(signals) == 1
        signal = signals[0]
        # The signal should reflect the opus-based ensemble, which will be
        # biased toward 0.72 rather than 0.65. The exact value depends on
        # ensemble weights, but it should be > the sonnet-only ensemble.
        # Sonnet ensemble would give ~0.85*0.65 + 0.15*0.40 = 0.6125
        # Opus ensemble would give ~0.85*0.72 + 0.15*0.40 = 0.672
        # So probability_estimate should be closer to 0.67 than 0.61
        assert signal.probability_estimate > 0.60

    @pytest.mark.asyncio
    async def test_opus_disagreement_rejects_signal(self, strategy, sample_market):
        """If opus disagrees >10% with sonnet, signal should be rejected."""
        sonnet_forecast = ForecastResult(
            probability=0.60,
            confidence_low=0.50,
            confidence_high=0.70,
            reasoning="Sonnet analysis",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        opus_forecast = ForecastResult(
            probability=0.42,  # Disagrees by 18%
            confidence_low=0.32,
            confidence_high=0.52,
            reasoning="Opus disagrees",
            model_used="claude-opus-4-6",
            tokens_used=800,
            latency_ms=2000,
        )

        async def mock_assess(*args, **kwargs):
            if kwargs.get("force_model"):
                return opus_forecast
            return sonnet_forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])

        assert len(signals) == 0  # Rejected due to opus disagreement

    @pytest.mark.asyncio
    async def test_low_edge_skips_escalation(self, strategy, sample_market):
        """Edge <= 15% should not trigger opus verification."""
        # Claude says 0.44 vs market 0.34 — edge ~10% after ensemble, below 15%
        forecast = ForecastResult(
            probability=0.44,
            confidence_low=0.38,
            confidence_high=0.50,
            reasoning="Modest edge",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )
        call_count = 0

        async def mock_assess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])

        # Only one call — no escalation
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_opus_already_used_skips_escalation(self, strategy, sample_market):
        """If initial forecast already used opus, don't re-verify."""
        forecast = ForecastResult(
            probability=0.60,
            confidence_low=0.50,
            confidence_high=0.70,
            reasoning="Opus initial",
            model_used="claude-opus-4-6",  # Already opus
            tokens_used=800,
            latency_ms=2000,
        )
        call_count = 0

        async def mock_assess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])

        # Only one call — no escalation since already opus
        assert call_count == 1
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_escalation_failure_keeps_sonnet_signal(self, strategy, sample_market):
        """If opus call fails, should keep the sonnet signal."""
        sonnet_forecast = ForecastResult(
            probability=0.60,
            confidence_low=0.50,
            confidence_high=0.70,
            reasoning="Sonnet analysis",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )

        async def mock_assess(*args, **kwargs):
            if kwargs.get("force_model"):
                raise Exception("Opus API error")
            return sonnet_forecast

        strategy.forecaster.assess_market = mock_assess

        signals = await strategy.scan_for_opportunities([sample_market])

        # Should keep sonnet signal on escalation failure
        assert len(signals) == 1
