"""Tests for ensemble forecasting."""

import pytest

from src.analysis.ensemble import ensemble_forecast, multi_model_ensemble, _compute_model_weights
from src.core.models import ForecastResult, EnsembleForecast


def _make_forecast(prob: float, model: str = "claude-sonnet-4-6") -> ForecastResult:
    return ForecastResult(
        probability=prob,
        confidence_low=max(0, prob - 0.1),
        confidence_high=min(1, prob + 0.1),
        reasoning="test",
        model_used=model,
    )


class TestEnsembleForecast:
    def test_weighted_average(self):
        forecast = _make_forecast(0.70)
        result = ensemble_forecast(forecast, market_price=0.50, claude_weight=0.7)
        # With adaptive weighting (CI width 0.2 → penalty factor 0.12),
        # effective claude_weight ≈ 0.7 * (1 - 0.2/0.5 * 0.3) ≈ 0.616
        # Result should be between market (0.50) and naive weighted (0.64)
        assert 0.58 < result.final_probability < 0.66
        assert result.final_probability > 0.50  # Still leans toward Claude

    def test_edge_positive(self):
        forecast = _make_forecast(0.70)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert result.edge > 0  # Claude thinks YES is underpriced

    def test_edge_negative(self):
        forecast = _make_forecast(0.30)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert result.edge < 0  # Claude thinks YES is overpriced

    def test_edge_zero_when_agreement(self):
        forecast = _make_forecast(0.50)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert abs(result.edge) < 0.01

    def test_confidence_based_on_ci_width(self):
        # Narrow CI → higher confidence
        f1 = ForecastResult(
            probability=0.60, confidence_low=0.55, confidence_high=0.65, reasoning="test"
        )
        r1 = ensemble_forecast(f1, market_price=0.50)

        # Wide CI → lower confidence
        f2 = ForecastResult(
            probability=0.60, confidence_low=0.30, confidence_high=0.90, reasoning="test"
        )
        r2 = ensemble_forecast(f2, market_price=0.50)

        assert r1.confidence > r2.confidence

    def test_clamped_to_valid_range(self):
        forecast = _make_forecast(0.99)
        result = ensemble_forecast(forecast, market_price=0.99, claude_weight=1.0)
        assert result.final_probability <= 0.99
        assert result.final_probability >= 0.01

    def test_includes_individual_forecasts(self):
        forecast = _make_forecast(0.60)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert len(result.individual_forecasts) == 1
        assert result.individual_forecasts[0].probability == 0.60

    def test_market_price_stored(self):
        forecast = _make_forecast(0.60)
        result = ensemble_forecast(forecast, market_price=0.45)
        assert result.market_price == 0.45


class TestMultiModelEnsemble:
    def test_equal_weights_without_brier(self):
        """Without Brier scores, all models get equal weight."""
        f1 = _make_forecast(0.60, model="model_a")
        f2 = _make_forecast(0.40, model="model_b")
        result = multi_model_ensemble([f1, f2], market_price=0.50)
        # market_weight=0.15 → 0.50 * 0.15 = 0.075
        # model share = 0.85, each model = 0.425
        # 0.60 * 0.425 + 0.40 * 0.425 = 0.255 + 0.17 = 0.425
        # total = 0.075 + 0.425 = 0.50
        assert abs(result.final_probability - 0.50) < 0.01

    def test_brier_weighted_favors_better_model(self):
        """Model with lower Brier score gets more weight."""
        f_good = _make_forecast(0.70, model="good")
        f_bad = _make_forecast(0.30, model="bad")
        brier = {"good": 0.10, "bad": 0.40}
        result = multi_model_ensemble(
            [f_good, f_bad], market_price=0.50, brier_scores=brier,
        )
        # "good" has Brier 0.10 → weight (1-0.1)=0.9
        # "bad" has Brier 0.40 → weight (1-0.4)=0.6
        # normalized: good=0.9/1.5=0.6, bad=0.6/1.5=0.4
        # scaled by 0.85: good=0.51, bad=0.34
        # prob = 0.50*0.15 + 0.70*0.51 + 0.30*0.34 = 0.075 + 0.357 + 0.102 = 0.534
        assert result.final_probability > 0.50  # Pulled toward good model's 0.70

    def test_category_brier_overrides_global(self):
        """Category-specific Brier scores take precedence over global."""
        f1 = _make_forecast(0.80, model="model_a")
        f2 = _make_forecast(0.20, model="model_b")
        global_brier = {"model_a": 0.30, "model_b": 0.10}  # b better globally
        category_brier = {"Politics": {"model_a": 0.05, "model_b": 0.40}}  # a better in Politics

        # With global brier (b is better) — pulled toward b's 0.20
        result_global = multi_model_ensemble(
            [f1, f2], market_price=0.50,
            brier_scores=global_brier,
        )

        # With category brier (a is better in Politics) — pulled toward a's 0.80
        result_category = multi_model_ensemble(
            [f1, f2], market_price=0.50,
            brier_scores=global_brier,
            category="Politics",
            category_brier_scores=category_brier,
        )
        # Category result should be higher than global result
        assert result_category.final_probability > result_global.final_probability

    def test_single_forecast(self):
        """Works correctly with a single forecast."""
        f = _make_forecast(0.65, model="solo")
        result = multi_model_ensemble([f], market_price=0.50)
        # 0.65 * 0.85 + 0.50 * 0.15 = 0.5525 + 0.075 = 0.6275
        assert abs(result.final_probability - 0.6275) < 0.01

    def test_empty_forecasts_returns_market_price(self):
        """Empty forecast list returns market price."""
        result = multi_model_ensemble([], market_price=0.50)
        assert abs(result.final_probability - 0.50) < 0.01
        assert result.confidence == 0.01  # Minimal confidence when no forecasts

    def test_disagreement_lowers_confidence(self):
        """High disagreement between models reduces confidence."""
        # Models agree
        f_agree1 = _make_forecast(0.60, model="a")
        f_agree2 = _make_forecast(0.62, model="b")
        r_agree = multi_model_ensemble([f_agree1, f_agree2], market_price=0.50)

        # Models disagree
        f_dis1 = _make_forecast(0.80, model="a")
        f_dis2 = _make_forecast(0.20, model="b")
        r_disagree = multi_model_ensemble([f_dis1, f_dis2], market_price=0.50)

        assert r_agree.confidence > r_disagree.confidence

    def test_includes_all_individual_forecasts(self):
        f1 = _make_forecast(0.60, model="a")
        f2 = _make_forecast(0.70, model="b")
        f3 = _make_forecast(0.55, model="c")
        result = multi_model_ensemble([f1, f2, f3], market_price=0.50)
        assert len(result.individual_forecasts) == 3

    def test_market_weight_zero(self):
        """With market_weight=0, only model forecasts matter."""
        f = _make_forecast(0.70, model="solo")
        result = multi_model_ensemble([f], market_price=0.50, market_weight=0.0)
        assert abs(result.final_probability - 0.70) < 0.01

    def test_clamped_to_valid_range(self):
        f = _make_forecast(0.99, model="extreme")
        result = multi_model_ensemble([f], market_price=0.99, market_weight=0.0)
        assert 0.01 <= result.final_probability <= 0.99


class TestComputeModelWeights:
    def test_equal_weights_no_brier(self):
        forecasts = [
            _make_forecast(0.5, "a"),
            _make_forecast(0.6, "b"),
        ]
        weights = _compute_model_weights(forecasts, None, "", None)
        assert len(weights) == 2
        assert abs(weights[0].weight - 0.5) < 0.01
        assert abs(weights[1].weight - 0.5) < 0.01

    def test_brier_weighted(self):
        forecasts = [
            _make_forecast(0.5, "good"),
            _make_forecast(0.6, "bad"),
        ]
        brier = {"good": 0.10, "bad": 0.40}
        weights = _compute_model_weights(forecasts, brier, "", None)
        # good: (1-0.1)=0.9, bad: (1-0.4)=0.6, total=1.5
        assert weights[0].weight > weights[1].weight
        assert abs(weights[0].weight - 0.6) < 0.01  # 0.9/1.5

    def test_partial_brier_uses_average_for_missing(self):
        """Models without Brier scores get average weight."""
        forecasts = [
            _make_forecast(0.5, "known"),
            _make_forecast(0.6, "unknown"),
        ]
        brier = {"known": 0.10}
        weights = _compute_model_weights(forecasts, brier, "", None)
        # Only 1 model has score, so falls back to equal weights
        assert abs(weights[0].weight - 0.5) < 0.01
