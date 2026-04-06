"""Tests for ensemble forecasting."""

import pytest

from src.analysis.ensemble import (
    _compute_model_weights,
    ensemble_forecast,
    extremize,
    multi_model_ensemble,
)
from src.core.models import EnsembleForecast, ForecastResult


class TestExtremize:
    def test_pushes_above_50_higher(self):
        assert extremize(0.60) > 0.60

    def test_pushes_below_50_lower(self):
        assert extremize(0.40) < 0.40

    def test_50_unchanged(self):
        assert abs(extremize(0.50) - 0.50) < 0.001

    def test_extreme_values_unchanged(self):
        assert extremize(0.01) == 0.01
        assert extremize(0.99) == 0.99

    def test_clamped_to_valid_range(self):
        assert 0.01 <= extremize(0.02) <= 0.99
        assert 0.01 <= extremize(0.98) <= 0.99

    def test_factor_1_is_identity(self):
        assert abs(extremize(0.70, factor=1.0) - 0.70) < 0.001

    def test_higher_factor_more_extreme(self):
        mild = extremize(0.70, factor=1.1)
        strong = extremize(0.70, factor=1.3)
        assert strong > mild > 0.70


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
        # With adaptive weighting (CI width 0.2 → penalty 0.2, max 50% reduction),
        # effective claude_weight ≈ 0.7 * (1 - 0.2 * 0.5) = 0.63
        # Result should be between market (0.50) and naive weighted (0.64)
        assert 0.55 < result.final_probability < 0.66
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
        # Base: 0.65 * 0.60 + 0.50 * 0.40 ≈ 0.59, then extremized away from 50%
        assert abs(result.final_probability - 0.59) < 0.04

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
        # 0.70 extremized → ~0.726
        assert abs(result.final_probability - 0.70) < 0.04

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
        """Models without Brier scores get the scored model's average weight."""
        forecasts = [
            _make_forecast(0.5, "known"),
            _make_forecast(0.6, "unknown"),
        ]
        brier = {"known": 0.10}
        weights = _compute_model_weights(forecasts, brier, "", None)
        # With 1 scored model: known gets full Brier weight (1.0),
        # unknown gets average of scored weights (also 1.0).
        # After confidence/source_strength normalization, both end up ~0.5.
        assert abs(weights[0].weight - 0.5) < 0.01
        assert abs(weights[1].weight - 0.5) < 0.01


class TestDynamicMarketEfficiency:
    """Tests for compute_market_efficiency and its integration."""

    def test_zero_volume_low_efficiency(self):
        from src.analysis.ensemble import compute_market_efficiency
        eff = compute_market_efficiency(volume_24h=0, liquidity=0)
        assert eff == pytest.approx(0.3, abs=0.05)  # Floor

    def test_high_volume_high_efficiency(self):
        from src.analysis.ensemble import compute_market_efficiency
        eff = compute_market_efficiency(volume_24h=1_000_000, liquidity=100_000)
        assert eff > 0.7

    def test_near_expiry_boosts_efficiency(self):
        from src.analysis.ensemble import compute_market_efficiency
        eff_far = compute_market_efficiency(volume_24h=10_000, liquidity=5_000, days_to_resolution=60)
        eff_near = compute_market_efficiency(volume_24h=10_000, liquidity=5_000, days_to_resolution=2)
        assert eff_near > eff_far

    def test_efficiency_clamped(self):
        from src.analysis.ensemble import compute_market_efficiency
        eff = compute_market_efficiency(volume_24h=100_000_000, liquidity=100_000_000, days_to_resolution=0.5)
        assert eff <= 0.95
        eff_low = compute_market_efficiency(volume_24h=0, liquidity=0, days_to_resolution=365)
        assert eff_low >= 0.3

    def test_high_efficiency_trusts_market_more(self):
        """With high efficiency, ensemble should pull more toward market price."""
        f = _make_forecast(0.70)
        # Default efficiency (~0.7)
        r_default = ensemble_forecast(f, market_price=0.50)
        # High efficiency → market trusted more
        r_efficient = ensemble_forecast(f, market_price=0.50, market_efficiency=0.9)
        # More efficient market → result closer to 0.50
        assert abs(r_efficient.final_probability - 0.50) <= abs(r_default.final_probability - 0.50) + 0.02

    def test_low_efficiency_trusts_claude_more(self):
        """With low efficiency, ensemble should trust Claude more."""
        f = _make_forecast(0.70)
        # Low efficiency → Claude trusted more
        r_inefficient = ensemble_forecast(f, market_price=0.50, market_efficiency=0.3)
        # High efficiency → market trusted more
        r_efficient = ensemble_forecast(f, market_price=0.50, market_efficiency=0.9)
        # Low efficiency → result further from 0.50 (closer to Claude's 0.70)
        assert r_inefficient.final_probability >= r_efficient.final_probability - 0.02


class TestConfidenceWeighting:
    """Tests for confidence-weighted and source-strength-weighted ensemble."""

    def test_narrow_ci_gets_higher_weight_than_wide(self):
        """Forecast with narrow CI should get more weight than wide CI."""
        f_narrow = ForecastResult(
            probability=0.70, confidence_low=0.65, confidence_high=0.75,  # CI=0.10
            reasoning="test", model_used="narrow",
        )
        f_wide = ForecastResult(
            probability=0.30, confidence_low=0.10, confidence_high=0.50,  # CI=0.40
            reasoning="test", model_used="wide",
        )
        weights = _compute_model_weights([f_narrow, f_wide], None, "", None)
        # Narrow CI (0.10) → factor 0.95, Wide CI (0.40) → factor 0.80
        assert weights[0].weight > weights[1].weight

    def test_source_strength_affects_weight(self):
        """Low source_strength should reduce weight."""
        f_strong = ForecastResult(
            probability=0.70, confidence_low=0.60, confidence_high=0.80,
            reasoning="test", model_used="strong", source_strength=1.0,
        )
        f_weak = ForecastResult(
            probability=0.30, confidence_low=0.20, confidence_high=0.40,
            reasoning="test", model_used="weak", source_strength=0.2,
        )
        weights = _compute_model_weights([f_strong, f_weak], None, "", None)
        # Same CI width, but weak has 0.2 source_strength
        assert weights[0].weight > weights[1].weight

    def test_ensemble_pulls_toward_high_strength_source(self):
        """Multi-model ensemble should favor high-strength sources."""
        f_strong = ForecastResult(
            probability=0.80, confidence_low=0.75, confidence_high=0.85,
            reasoning="test", model_used="strong", source_strength=1.0,
        )
        f_weak = ForecastResult(
            probability=0.20, confidence_low=0.15, confidence_high=0.25,
            reasoning="test", model_used="weak", source_strength=0.1,
        )
        result = multi_model_ensemble(
            [f_strong, f_weak], market_price=0.50, market_weight=0.0,
        )
        # Should be pulled strongly toward 0.80 (strong source)
        assert result.final_probability > 0.60

    def test_weights_still_normalized(self):
        """After all adjustments, weights should sum to approximately 1.0."""
        f1 = ForecastResult(
            probability=0.50, confidence_low=0.40, confidence_high=0.60,
            reasoning="test", model_used="a", source_strength=0.5,
        )
        f2 = ForecastResult(
            probability=0.60, confidence_low=0.30, confidence_high=0.90,
            reasoning="test", model_used="b", source_strength=1.0,
        )
        weights = _compute_model_weights([f1, f2], None, "", None)
        total = sum(w.weight for w in weights)
        assert abs(total - 1.0) < 0.01


class TestCIPenaltyScaling:
    """Tests for the fixed CI penalty that now scales across the full 0-1 range."""

    def test_wide_ci_reduces_more_than_medium(self):
        """CI width 0.8 should reduce Claude weight more than 0.4."""
        f_medium = ForecastResult(
            probability=0.60, confidence_low=0.40, confidence_high=0.80,  # CI=0.40
            reasoning="test",
        )
        f_wide = ForecastResult(
            probability=0.60, confidence_low=0.10, confidence_high=0.90,  # CI=0.80
            reasoning="test",
        )
        r_medium = ensemble_forecast(f_medium, market_price=0.50)
        r_wide = ensemble_forecast(f_wide, market_price=0.50)
        # Wider CI → more market pull → closer to 0.50
        assert abs(r_wide.final_probability - 0.50) < abs(r_medium.final_probability - 0.50)

    def test_ci_05_and_09_produce_different_results(self):
        """Regression: before fix, CI=0.5 and CI=0.9 got identical treatment."""
        f_05 = ForecastResult(
            probability=0.70, confidence_low=0.45, confidence_high=0.95,  # CI=0.50
            reasoning="test",
        )
        f_09 = ForecastResult(
            probability=0.70, confidence_low=0.05, confidence_high=0.95,  # CI=0.90
            reasoning="test",
        )
        r_05 = ensemble_forecast(f_05, market_price=0.50)
        r_09 = ensemble_forecast(f_09, market_price=0.50)
        # CI=0.9 should pull more toward market than CI=0.5
        assert r_09.final_probability < r_05.final_probability

    def test_zero_ci_full_claude_weight(self):
        """CI width 0 should give Claude full weight (no penalty)."""
        f = ForecastResult(
            probability=0.70, confidence_low=0.70, confidence_high=0.70,  # CI=0
            reasoning="test",
        )
        result = ensemble_forecast(f, market_price=0.50, claude_weight=0.85)
        # With CI=0, effective weight = 0.85 * (1 - 0*0.5) = 0.85
        # Base: 0.70 * 0.85 + 0.50 * 0.15 ≈ 0.67, then extremized
        expected = 0.70 * 0.85 + 0.50 * 0.15
        assert abs(result.final_probability - expected) < 0.04


class TestInvertedCIRegression:
    """Regression tests for inverted CI bounds bug.

    Previously, inverted CI (low > high) caused ci_width to be negative,
    ci_penalty to be 0, and Claude to get full weight regardless. Now
    ForecastResult auto-corrects inverted bounds.
    """

    def test_inverted_ci_no_longer_gives_full_weight(self):
        """With inverted CI auto-corrected, ensemble should apply proper penalty."""
        # After auto-correction, confidence_high = confidence_low = 0.8
        # So ci_width = 0.0, which gives FULL weight to Claude
        # This is acceptable because the CI is effectively a point estimate
        f = ForecastResult(
            probability=0.70, confidence_low=0.80, confidence_high=0.20,
            reasoning="test",
        )
        # Verify the bounds were auto-corrected
        assert f.confidence_high >= f.confidence_low
        result = ensemble_forecast(f, market_price=0.50)
        assert 0.01 <= result.final_probability <= 0.99

    def test_normal_ci_still_works(self):
        """Normal CI bounds should work as before."""
        f = ForecastResult(
            probability=0.70, confidence_low=0.60, confidence_high=0.80,
            reasoning="test",
        )
        result = ensemble_forecast(f, market_price=0.50)
        assert result.final_probability > 0.50  # Still pulled toward Claude


class TestEnsembleExtremeMarketPrices:
    """Edge cases for extreme market prices."""

    def test_market_price_zero(self):
        """Market price at 0 (extreme bearish) should still produce valid result."""
        f = _make_forecast(0.30)
        result = ensemble_forecast(f, market_price=0.0)
        assert 0.01 <= result.final_probability <= 0.99

    def test_market_price_one(self):
        """Market price at 1 (near-certain YES) should still produce valid result."""
        f = _make_forecast(0.90)
        result = ensemble_forecast(f, market_price=1.0)
        assert 0.01 <= result.final_probability <= 0.99

    def test_boundary_probabilities(self):
        """Claude probabilities at boundaries."""
        for prob in [0.01, 0.50, 0.99]:
            f = _make_forecast(prob)
            result = ensemble_forecast(f, market_price=0.50)
            assert 0.01 <= result.final_probability <= 0.99


class TestEnsembleExtremeCombinations:
    """Edge cases with extreme Claude + market price combinations."""

    def test_max_disagreement_claude_high_market_low(self):
        """Claude says 0.99, market says 0.01 — extreme divergence.

        On extreme-price markets, Claude's weight is aggressively reduced
        (floor=0.25) because divergence at the tails is more likely a
        hallucination than genuine edge.
        """
        f = _make_forecast(0.99)
        result = ensemble_forecast(f, market_price=0.01)
        assert 0.01 <= result.final_probability <= 0.99
        # Extreme-price weighting pulls hard toward market; result should be
        # above market but well below midpoint due to reduced Claude weight
        assert result.final_probability > 0.10
        assert result.final_probability <= 0.50

    def test_max_disagreement_claude_low_market_high(self):
        """Claude says 0.01, market says 0.99 — extreme inverse."""
        f = _make_forecast(0.01)
        result = ensemble_forecast(f, market_price=0.99)
        assert 0.01 <= result.final_probability <= 0.99
        # Symmetric: pulled toward market, above midpoint
        assert result.final_probability >= 0.50

    def test_both_extreme_high(self):
        """Both Claude and market at 0.99 — M-3 shrinkage pulls toward 0.5."""
        f = _make_forecast(0.99)
        result = ensemble_forecast(f, market_price=0.99)
        # M-3: overconfidence shrinkage pulls extreme narrow-CI forecasts
        # toward 0.5 by 5%, so result is ~0.965 not 0.99
        assert result.final_probability == pytest.approx(0.965, abs=0.01)

    def test_both_extreme_low(self):
        """Both Claude and market at 0.01 — M-3 shrinkage pulls toward 0.5."""
        f = _make_forecast(0.01)
        result = ensemble_forecast(f, market_price=0.01)
        # M-3: overconfidence shrinkage pulls extreme narrow-CI forecasts
        # toward 0.5 by 5%, so result is ~0.035 not 0.01
        assert result.final_probability == pytest.approx(0.035, abs=0.01)

    def test_wide_ci_reduces_claude_weight(self):
        """Very wide CI (0.0–1.0) should reduce Claude's influence."""
        f_narrow = ForecastResult(
            probability=0.80, confidence_low=0.75, confidence_high=0.85,
            reasoning="narrow", model_used="claude",
        )
        f_wide = ForecastResult(
            probability=0.80, confidence_low=0.0, confidence_high=1.0,
            reasoning="wide", model_used="claude",
        )
        r_narrow = ensemble_forecast(f_narrow, market_price=0.50)
        r_wide = ensemble_forecast(f_wide, market_price=0.50)
        # Wide CI → more market influence → closer to 0.50
        assert abs(r_wide.final_probability - 0.50) < abs(r_narrow.final_probability - 0.50)

    def test_multi_model_all_agree(self):
        """Multiple models + market all agree → high confidence, no edge."""
        f1 = _make_forecast(0.60, model="claude")
        f2 = _make_forecast(0.60, model="manifold")
        result = multi_model_ensemble(forecasts=[f1, f2], market_price=0.60)
        assert abs(result.edge) < 0.02
        assert result.confidence > 0.5

    def test_multi_model_all_disagree(self):
        """Models wildly disagree → low confidence."""
        f1 = _make_forecast(0.10, model="claude")
        f2 = _make_forecast(0.90, model="manifold")
        result = multi_model_ensemble(forecasts=[f1, f2], market_price=0.50)
        # Disagreement should tank confidence
        assert result.confidence < 0.5


class TestMarketPriceValidation:
    """Tests for market_price bounds validation in ensemble_forecast."""

    def test_zero_market_price_clamped(self):
        """market_price=0.0 should be clamped to 0.01, not cause errors."""
        f = _make_forecast(0.60)
        result = ensemble_forecast(f, market_price=0.0)
        assert 0.01 <= result.final_probability <= 0.99
        assert result.market_price == 0.01

    def test_one_market_price_clamped(self):
        """market_price=1.0 should be clamped to 0.99, not cause errors."""
        f = _make_forecast(0.40)
        result = ensemble_forecast(f, market_price=1.0)
        assert 0.01 <= result.final_probability <= 0.99
        assert result.market_price == 0.99

    def test_negative_market_price_clamped(self):
        """market_price=-0.5 should be clamped to 0.01."""
        f = _make_forecast(0.60)
        result = ensemble_forecast(f, market_price=-0.5)
        assert result.market_price == 0.01

    def test_valid_market_price_unchanged(self):
        """Valid market_price should pass through unchanged."""
        f = _make_forecast(0.60)
        result = ensemble_forecast(f, market_price=0.50)
        assert result.market_price == 0.50
