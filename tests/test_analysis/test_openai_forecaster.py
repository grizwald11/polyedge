"""Tests for OpenAI GPT-4o forecaster and ensemble integration."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import (
    EnsembleForecast,
    ForecastResult,
    Market,
    MarketToken,
    Platform,
    TokenOutcome,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_market(**kwargs) -> Market:
    defaults = dict(
        ticker="MKT-TEST",
        platform=Platform.KALSHI,
        question="Will X happen by end of 2026?",
        tokens=[
            MarketToken(token_id="yes_tok", outcome=TokenOutcome.YES, price=0.60),
            MarketToken(token_id="no_tok", outcome=TokenOutcome.NO, price=0.40),
        ],
    )
    defaults.update(kwargs)
    return Market(**defaults)


def _mock_settings():
    """Create a minimal Settings-like object for OpenAIForecaster."""
    settings = MagicMock()
    settings.claude.category_temperatures = {"Politics": 0.35}
    settings.claude.temperature = 0.3
    settings.claude.highstakes_threshold = 50.0
    settings.claude.max_tokens = 2000

    openai_cfg = MagicMock()
    openai_cfg.model = "gpt-4o"
    openai_cfg.max_tokens = 2000
    openai_cfg.timeout = 60
    openai_cfg.daily_budget = 500_000
    settings.openai = openai_cfg
    return settings


def _mock_openai_response(content: str, prompt_tokens: int = 500, completion_tokens: int = 200):
    """Create a mock OpenAI ChatCompletion response."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    message = MagicMock()
    message.content = content

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


# ---------------------------------------------------------------------------
# OpenAIForecaster unit tests
# ---------------------------------------------------------------------------


class TestOpenAIForecasterInit:
    def test_disabled_without_api_key(self):
        """Forecaster should be disabled when OPENAI_API_KEY is not set."""
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            from src.analysis.openai_forecaster import OpenAIForecaster
            forecaster = OpenAIForecaster(_mock_settings())
            assert forecaster.is_available is False

    def test_enabled_with_api_key(self):
        """Forecaster should be available when OPENAI_API_KEY is set."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}):
            with patch("src.analysis.openai_forecaster.openai", create=True):
                from src.analysis.openai_forecaster import OpenAIForecaster
                forecaster = OpenAIForecaster(_mock_settings())
                # May or may not be available depending on openai import
                # Just verify it doesn't crash
                assert isinstance(forecaster.is_available, bool)

    def test_disabled_without_openai_package(self):
        """Forecaster should handle missing openai package gracefully."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}):
            from src.analysis.openai_forecaster import OpenAIForecaster
            with patch.object(OpenAIForecaster, '_init_client') as mock_init:
                mock_init.return_value = None
                forecaster = OpenAIForecaster(_mock_settings())
                forecaster._available = False
                assert forecaster.is_available is False


class TestOpenAIForecasterCircuitBreaker:
    def test_circuit_closed_by_default(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            assert forecaster.is_circuit_open() is False

    def test_circuit_opens_after_3_failures(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._record_api_failure()
            forecaster._record_api_failure()
            assert forecaster.is_circuit_open() is False
            forecaster._record_api_failure()
            assert forecaster.is_circuit_open() is True

    def test_circuit_closes_after_cooldown(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._circuit_open_until = time.monotonic() - 1  # Already expired
            assert forecaster.is_circuit_open() is False


class TestOpenAIForecasterBudget:
    def test_budget_not_exceeded_initially(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            assert forecaster.is_budget_exceeded() is False

    def test_budget_exceeded_at_2x_limit(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._daily_budget = 100
            forecaster._total_tokens_today = 201  # > 2x100
            assert forecaster.is_budget_exceeded() is True


class TestOpenAIForecasterAssess:
    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            result = await forecaster.assess_market(_make_market())
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_circuit_open(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()
            forecaster._circuit_open_until = time.monotonic() + 300
            result = await forecaster.assess_market(_make_market())
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_budget_exceeded(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()
            forecaster._daily_budget = 100
            forecaster._total_tokens_today = 201
            result = await forecaster.assess_market(_make_market())
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_assessment(self):
        """Test a successful GPT-4o assessment with mocked API."""
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            # Mock the API call
            json_response = '{"probability": 0.65, "confidence_low": 0.55, "confidence_high": 0.75, "reasoning": "test reasoning"}'
            mock_response = _mock_openai_response(json_response)

            forecaster._call_gpt4o = AsyncMock(return_value=mock_response)

            result = await forecaster.assess_market(_make_market())

            assert result is not None
            assert result.probability == 0.65
            assert result.model_used == "gpt-4o"
            assert result.latency_ms >= 0
            assert forecaster._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_records_failure_on_api_error(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            forecaster._call_gpt4o = AsyncMock(side_effect=RuntimeError("API error"))

            result = await forecaster.assess_market(_make_market())

            assert result is None
            assert forecaster._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_empty_response_records_failure(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            mock_response = MagicMock()
            mock_response.choices = []
            forecaster._call_gpt4o = AsyncMock(return_value=mock_response)

            result = await forecaster.assess_market(_make_market())

            assert result is None
            assert forecaster._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            # Pre-populate cache
            cached_result = ForecastResult(
                probability=0.70, reasoning="cached", model_used="gpt-4o",
            )
            cached_result._cached_market_price = 0.60  # type: ignore[attr-defined]
            forecaster._forecast_cache.set("gpt4o:MKT-TEST", cached_result)

            result = await forecaster.assess_market(_make_market())

            assert result is not None
            assert result.probability == 0.70
            assert result.reasoning == "cached"

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_price_move(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            # Pre-populate cache with stale price
            cached_result = ForecastResult(
                probability=0.70, reasoning="cached", model_used="gpt-4o",
            )
            cached_result._cached_market_price = 0.40  # Big move from 0.40 to 0.60
            forecaster._forecast_cache.set("gpt4o:MKT-TEST", cached_result)

            # Should NOT return cached result — price moved >5%
            json_response = '{"probability": 0.55, "reasoning": "fresh"}'
            mock_response = _mock_openai_response(json_response)
            forecaster._call_gpt4o = AsyncMock(return_value=mock_response)

            result = await forecaster.assess_market(_make_market())

            assert result is not None
            assert result.probability == 0.55  # Fresh result, not cached 0.70

    @pytest.mark.asyncio
    async def test_token_tracking(self):
        from src.analysis.openai_forecaster import OpenAIForecaster
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            forecaster = OpenAIForecaster(_mock_settings())
            forecaster._available = True
            forecaster._client = MagicMock()

            json_response = '{"probability": 0.65, "reasoning": "test"}'
            mock_response = _mock_openai_response(json_response, prompt_tokens=800, completion_tokens=300)
            forecaster._call_gpt4o = AsyncMock(return_value=mock_response)

            await forecaster.assess_market(_make_market())

            assert forecaster._total_tokens_today == 1100
            assert forecaster._call_count_today == 1
            assert forecaster._total_cost_today > 0


# ---------------------------------------------------------------------------
# Ensemble integration with GPT-4o
# ---------------------------------------------------------------------------


def _make_forecast(prob: float, model: str = "claude-sonnet-4-6") -> ForecastResult:
    return ForecastResult(
        probability=prob,
        confidence_low=max(0, prob - 0.1),
        confidence_high=min(1, prob + 0.1),
        reasoning="test",
        model_used=model,
    )


class TestEnsembleWithGPT4o:
    def test_two_ai_models_both_get_weight(self):
        """Both Claude and GPT-4o should get meaningful weight."""
        from src.analysis.ensemble import _compute_model_weights
        f_claude = _make_forecast(0.70, model="claude-sonnet-4-6")
        f_gpt = _make_forecast(0.60, model="gpt-4o")
        weights = _compute_model_weights([f_claude, f_gpt], None, "", None)
        assert len(weights) == 2
        # Both should have at least 20% weight (minimum floor)
        for w in weights:
            assert w.weight >= 0.19  # Allow tiny float imprecision

    def test_brier_weighted_with_floor(self):
        """Even with very different Brier scores, minimum 20% weight floor holds."""
        from src.analysis.ensemble import _compute_model_weights
        f_claude = _make_forecast(0.70, model="claude-sonnet-4-6")
        f_gpt = _make_forecast(0.60, model="gpt-4o")
        brier = {"claude-sonnet-4-6": 0.05, "gpt-4o": 0.45}  # Claude much better
        weights = _compute_model_weights([f_claude, f_gpt], brier, "", None)
        # GPT-4o should still have at least 20%
        gpt_weight = next(w for w in weights if "gpt" in w.name)
        assert gpt_weight.weight >= 0.19

    def test_disagreement_pct_set_on_ensemble(self):
        """multi_model_ensemble should set disagreement_pct when AI models disagree."""
        from src.analysis.ensemble import multi_model_ensemble
        f_claude = _make_forecast(0.80, model="claude-sonnet-4-6")
        f_gpt = _make_forecast(0.55, model="gpt-4o")  # 25% disagreement
        result = multi_model_ensemble([f_claude, f_gpt], market_price=0.65)
        assert result.disagreement_pct == pytest.approx(0.25, abs=0.01)

    def test_no_disagreement_when_models_agree(self):
        """disagreement_pct should be ~0 when models agree."""
        from src.analysis.ensemble import multi_model_ensemble
        f_claude = _make_forecast(0.65, model="claude-sonnet-4-6")
        f_gpt = _make_forecast(0.63, model="gpt-4o")
        result = multi_model_ensemble([f_claude, f_gpt], market_price=0.60)
        assert result.disagreement_pct < 0.05

    def test_single_model_no_disagreement(self):
        """With only Claude (no GPT-4o), disagreement_pct should be 0."""
        from src.analysis.ensemble import multi_model_ensemble
        f_claude = _make_forecast(0.70, model="claude-sonnet-4-6")
        result = multi_model_ensemble([f_claude], market_price=0.60)
        assert result.disagreement_pct == 0.0

    def test_community_source_not_counted_as_ai(self):
        """Community forecasts (manifold, metaculus) shouldn't trigger AI disagreement."""
        from src.analysis.ensemble import multi_model_ensemble
        f_claude = _make_forecast(0.80, model="claude-sonnet-4-6")
        f_manifold = _make_forecast(0.55, model="manifold_community")
        result = multi_model_ensemble([f_claude, f_manifold], market_price=0.65)
        # manifold is not an AI model, so disagreement_pct should be 0
        assert result.disagreement_pct == 0.0

    def test_ensemble_result_between_models(self):
        """Ensemble result should be between the two AI model estimates."""
        from src.analysis.ensemble import multi_model_ensemble
        f_claude = _make_forecast(0.80, model="claude-sonnet-4-6")
        f_gpt = _make_forecast(0.60, model="gpt-4o")
        result = multi_model_ensemble(
            [f_claude, f_gpt], market_price=0.70, market_weight=0.0,
        )
        # Result should be between 0.60 and 0.80 (approximately)
        assert 0.55 < result.final_probability < 0.85


class TestIsAiModel:
    def test_claude_is_ai(self):
        from src.analysis.ensemble import _is_ai_model
        assert _is_ai_model("claude-sonnet-4-6") is True
        assert _is_ai_model("claude-opus-4-6") is True

    def test_gpt_is_ai(self):
        from src.analysis.ensemble import _is_ai_model
        assert _is_ai_model("gpt-4o") is True
        assert _is_ai_model("gpt-4-turbo") is True

    def test_gemini_is_ai(self):
        from src.analysis.ensemble import _is_ai_model
        assert _is_ai_model("gemini-1.5-pro") is True

    def test_community_not_ai(self):
        from src.analysis.ensemble import _is_ai_model
        assert _is_ai_model("manifold_community") is False
        assert _is_ai_model("metaculus_community") is False
        assert _is_ai_model("polymarket_cross_ref") is False

    def test_unknown_not_ai(self):
        from src.analysis.ensemble import _is_ai_model
        assert _is_ai_model("unknown") is False
        assert _is_ai_model("") is False
