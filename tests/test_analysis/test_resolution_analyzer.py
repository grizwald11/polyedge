"""Tests for the resolution criteria analyzer."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.resolution_analyzer import ResolutionAnalyzer, ResolutionAnalysis


@pytest.fixture
def mock_forecaster():
    forecaster = MagicMock()
    forecaster.assess_market_with_prompt = AsyncMock()
    return forecaster


@pytest.fixture
def analyzer(mock_forecaster):
    return ResolutionAnalyzer(mock_forecaster)


def _make_forecast_result(raw_json: dict):
    result = MagicMock()
    result.parse_failed = False
    result.raw_response = json.dumps(raw_json)
    return result


class TestResolutionAnalysis:
    def test_format_for_prompt_with_risks(self):
        analysis = ResolutionAnalysis(
            ambiguities=["'Announced' could mean stated or enacted"],
            edge_cases=["Partial fulfillment unclear"],
            date_boundaries=["Market closes before official result date"],
            definitional_issues=["'Above' may or may not include equality"],
            risk_score=0.6,
            summary="Resolution depends on exact interpretation of 'announced'",
        )
        text = analysis.format_for_prompt()
        assert "RESOLUTION CRITERIA RISKS" in text
        assert "Announced" in text
        assert "Partial fulfillment" in text

    def test_format_for_prompt_empty(self):
        analysis = ResolutionAnalysis()
        text = analysis.format_for_prompt()
        assert text == ""

    def test_is_high_risk(self):
        assert ResolutionAnalysis(risk_score=0.85).is_high_risk
        assert not ResolutionAnalysis(risk_score=0.50).is_high_risk

    @pytest.mark.asyncio
    async def test_analyze_calls_claude(self, analyzer, mock_forecaster, sample_market):
        mock_forecaster.assess_market_with_prompt.return_value = _make_forecast_result({
            "ambiguities": ["Term A is unclear"],
            "edge_cases": ["Edge case B"],
            "date_boundaries": [],
            "definitional_issues": [],
            "risk_score": 35,
            "summary": "Moderate risk",
        })

        result = await analyzer.analyze(sample_market)
        assert isinstance(result, ResolutionAnalysis)
        assert len(result.ambiguities) == 1
        assert result.risk_score == pytest.approx(0.35)
        mock_forecaster.assess_market_with_prompt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_analyze_caches_result(self, analyzer, mock_forecaster, sample_market):
        mock_forecaster.assess_market_with_prompt.return_value = _make_forecast_result({
            "ambiguities": [],
            "edge_cases": [],
            "date_boundaries": [],
            "definitional_issues": [],
            "risk_score": 20,
            "summary": "Low risk",
        })

        result1 = await analyzer.analyze(sample_market)
        result2 = await analyzer.analyze(sample_market)
        # Should only call Claude once (cached)
        assert mock_forecaster.assess_market_with_prompt.await_count == 1
        assert result1.risk_score == result2.risk_score

    @pytest.mark.asyncio
    async def test_analyze_handles_failure(self, analyzer, mock_forecaster, sample_market):
        mock_forecaster.assess_market_with_prompt.return_value = None

        result = await analyzer.analyze(sample_market)
        assert result.risk_score == 0.5  # Default on failure

    @pytest.mark.asyncio
    async def test_analyze_no_description(self, analyzer, mock_forecaster, sample_market):
        sample_market.description = ""
        result = await analyzer.analyze(sample_market)
        assert result.risk_score == 0.3
        mock_forecaster.assess_market_with_prompt.assert_not_awaited()

    def test_parse_analysis_valid_json(self, analyzer):
        raw = json.dumps({
            "ambiguities": ["A", "B"],
            "edge_cases": ["C"],
            "date_boundaries": [],
            "definitional_issues": ["D"],
            "risk_score": 60,
            "summary": "Test",
        })
        result = analyzer._parse_analysis(raw)
        assert len(result.ambiguities) == 2
        assert result.risk_score == pytest.approx(0.60)
        assert result.summary == "Test"

    def test_parse_analysis_code_block(self, analyzer):
        raw = '```json\n{"ambiguities": [], "edge_cases": [], "date_boundaries": [], "definitional_issues": [], "risk_score": 10, "summary": "Clear"}\n```'
        result = analyzer._parse_analysis(raw)
        assert result.risk_score == pytest.approx(0.10)

    def test_parse_analysis_invalid(self, analyzer):
        result = analyzer._parse_analysis("Not JSON at all")
        assert result.risk_score == 0.5  # Default on parse failure

    def test_risk_score_clamped(self, analyzer):
        raw = json.dumps({"risk_score": 150, "summary": "Over max"})
        result = analyzer._parse_analysis(raw)
        assert result.risk_score == 1.0

        raw = json.dumps({"risk_score": -10, "summary": "Under min"})
        result = analyzer._parse_analysis(raw)
        assert result.risk_score == 0.0
