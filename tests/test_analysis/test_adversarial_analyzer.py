"""Tests for the pre-mortem adversarial analyzer."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.adversarial_analyzer import AdversarialAnalyzer, AdversarialResult
from src.core.models import ForecastResult


@pytest.fixture
def mock_forecaster():
    f = MagicMock()
    f.assess_market_with_prompt = AsyncMock()
    return f


@pytest.fixture
def analyzer(mock_forecaster):
    return AdversarialAnalyzer(mock_forecaster)


@pytest.fixture
def high_confidence_forecast():
    return ForecastResult(
        probability=0.75,
        confidence_low=0.65,
        confidence_high=0.85,
        reasoning="Strong evidence for YES",
        model_used="claude-sonnet-4-6",
    )


def _make_adversarial_response(plausibility: int = 60):
    result = MagicMock()
    result.parse_failed = False
    result.raw_response = json.dumps({
        "counter_narrative": "A surprise policy reversal occurred",
        "plausibility": plausibility,
        "weakest_assumption": "Assumed current trend continues",
        "key_risks": ["Policy reversal", "External shock"],
    })
    return result


class TestShouldRun:
    def test_runs_on_high_edge_tight_ci(self, analyzer):
        assert analyzer.should_run(edge=0.12, ci_width=0.20)

    def test_skips_low_edge(self, analyzer):
        assert not analyzer.should_run(edge=0.03, ci_width=0.20)

    def test_skips_wide_ci(self, analyzer):
        assert not analyzer.should_run(edge=0.15, ci_width=0.40)

    def test_skips_both_bad(self, analyzer):
        assert not analyzer.should_run(edge=0.03, ci_width=0.40)


class TestRunPremortem:
    @pytest.mark.asyncio
    async def test_basic_premortem(self, analyzer, mock_forecaster, sample_market, high_confidence_forecast):
        mock_forecaster.assess_market_with_prompt.return_value = _make_adversarial_response(60)
        result = await analyzer.run_premortem(sample_market, high_confidence_forecast)
        assert isinstance(result, AdversarialResult)
        assert result.plausibility == pytest.approx(0.60)
        assert "reversal" in result.counter_narrative.lower()

    @pytest.mark.asyncio
    async def test_high_plausibility_triggers_adjustment(self, analyzer, mock_forecaster, sample_market, high_confidence_forecast):
        mock_forecaster.assess_market_with_prompt.return_value = _make_adversarial_response(80)
        result = await analyzer.run_premortem(sample_market, high_confidence_forecast)
        assert result.plausibility == pytest.approx(0.80)
        assert result.ci_adjustment > 0.05
        assert result.probability_adjustment != 0

    @pytest.mark.asyncio
    async def test_low_plausibility_no_adjustment(self, analyzer, mock_forecaster, sample_market, high_confidence_forecast):
        mock_forecaster.assess_market_with_prompt.return_value = _make_adversarial_response(30)
        result = await analyzer.run_premortem(sample_market, high_confidence_forecast)
        assert result.ci_adjustment == 0.0
        assert result.probability_adjustment == 0.0

    @pytest.mark.asyncio
    async def test_api_failure_returns_empty(self, analyzer, mock_forecaster, sample_market, high_confidence_forecast):
        mock_forecaster.assess_market_with_prompt.return_value = None
        result = await analyzer.run_premortem(sample_market, high_confidence_forecast)
        assert result.plausibility == 0.0
        assert result.ci_adjustment == 0.0


class TestApplyAdjustment:
    def test_applies_ci_widening(self, analyzer, high_confidence_forecast):
        adversarial = AdversarialResult(
            plausibility=0.8,
            ci_adjustment=0.10,
            probability_adjustment=-0.05,
            key_risks=["Risk A"],
            weakest_assumption="Assumption X",
        )
        adjusted = analyzer.apply_adjustment(high_confidence_forecast, adversarial)
        assert adjusted.confidence_low < high_confidence_forecast.confidence_low
        assert adjusted.confidence_high > high_confidence_forecast.confidence_high
        assert adjusted.probability == pytest.approx(0.70)

    def test_no_adjustment_when_zero(self, analyzer, high_confidence_forecast):
        adversarial = AdversarialResult()
        adjusted = analyzer.apply_adjustment(high_confidence_forecast, adversarial)
        assert adjusted.probability == high_confidence_forecast.probability

    def test_probability_clamped(self, analyzer):
        extreme = ForecastResult(
            probability=0.99,
            confidence_low=0.95,
            confidence_high=0.99,
            reasoning="test",
        )
        adversarial = AdversarialResult(
            plausibility=0.9,
            ci_adjustment=0.15,
            probability_adjustment=0.05,
        )
        adjusted = analyzer.apply_adjustment(extreme, adversarial)
        assert 0.01 <= adjusted.probability <= 0.99
        assert adjusted.confidence_high <= 1.0

    def test_appends_risks(self, analyzer, high_confidence_forecast):
        adversarial = AdversarialResult(
            plausibility=0.8,
            ci_adjustment=0.05,
            probability_adjustment=-0.02,
            key_risks=["New risk"],
            weakest_assumption="Weak assumption",
        )
        adjusted = analyzer.apply_adjustment(high_confidence_forecast, adversarial)
        assert "New risk" in adjusted.key_factors_against


class TestParseResult:
    def test_parse_valid_json(self, analyzer):
        raw = json.dumps({
            "counter_narrative": "Story",
            "plausibility": 70,
            "weakest_assumption": "Bad assumption",
            "key_risks": ["R1", "R2"],
        })
        result = analyzer._parse_result(raw, 0.75)
        assert result.plausibility == pytest.approx(0.70)
        assert result.ci_adjustment > 0

    def test_parse_embedded_json(self, analyzer):
        raw = 'Here is my analysis:\n{"counter_narrative": "N", "plausibility": 40, "weakest_assumption": "W", "key_risks": []}\nDone.'
        result = analyzer._parse_result(raw, 0.60)
        assert result.plausibility == pytest.approx(0.40)

    def test_parse_invalid(self, analyzer):
        result = analyzer._parse_result("Not JSON", 0.50)
        assert result.plausibility == 0.0
