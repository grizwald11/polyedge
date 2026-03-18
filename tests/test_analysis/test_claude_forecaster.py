"""Tests for the Claude forecaster."""

from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.news_researcher import NewsResearcher
from src.config import Settings
from src.core.models import Market, MarketToken, MarketCategory, ForecastResult


@pytest.fixture
def forecaster_settings() -> Settings:
    s = Settings()
    s.anthropic_api_key = "test-key"
    return s


@pytest.fixture
def forecaster(forecaster_settings) -> ClaudeForecaster:
    return ClaudeForecaster(forecaster_settings)


def _mock_claude_response(prob: float = 0.42) -> MagicMock:
    """Create a mock Claude API response."""
    response = MagicMock()
    content = MagicMock()
    content.text = json.dumps({
        "probability": prob,
        "confidence_low": prob - 0.1,
        "confidence_high": prob + 0.1,
        "key_factors_for": ["Factor A"],
        "key_factors_against": ["Factor B"],
        "uncertainties": ["Uncertainty X"],
        "reasoning": "Test reasoning",
    })
    response.content = [content]
    response.usage = MagicMock()
    response.usage.input_tokens = 500
    response.usage.output_tokens = 200
    return response


class TestClaudeForecaster:
    def test_select_model_routine(self, forecaster):
        assert forecaster._select_model(10.0) == "claude-sonnet-4-6"

    def test_select_model_highstakes(self, forecaster):
        assert forecaster._select_model(100.0) == "claude-opus-4-6"

    def test_parse_valid_json(self, forecaster):
        raw = json.dumps({
            "probability": 0.65,
            "confidence_low": 0.55,
            "confidence_high": 0.75,
            "key_factors_for": ["A"],
            "key_factors_against": ["B"],
            "uncertainties": ["C"],
            "reasoning": "Test",
        })
        result = forecaster._parse_response(raw)
        assert result.probability == 0.65
        assert result.confidence_low == 0.55
        assert len(result.key_factors_for) == 1

    def test_parse_json_with_code_fences(self, forecaster):
        raw = '```json\n{"probability": 0.70, "reasoning": "test"}\n```'
        result = forecaster._parse_response(raw)
        assert result.probability == 0.70

    def test_parse_json_with_plain_code_fences(self, forecaster):
        raw = '```\n{"probability": 0.80, "reasoning": "test"}\n```'
        result = forecaster._parse_response(raw)
        assert result.probability == 0.80

    def test_parse_json_embedded_in_prose(self, forecaster):
        raw = 'Here is my analysis:\n\n{"probability": 0.55, "confidence_low": 0.45, "confidence_high": 0.65, "key_factors_for": ["A"], "key_factors_against": ["B"], "uncertainties": ["C"], "reasoning": "test"}\n\nI hope that helps!'
        result = forecaster._parse_response(raw)
        assert result.probability == 0.55
        assert result.confidence_low == 0.45

    def test_parse_json_after_explanation(self, forecaster):
        raw = 'Based on my assessment, the probability is approximately 62%.\n\n```json\n{"probability": 0.62, "reasoning": "analysis"}\n```\n\nLet me know if you need more detail.'
        result = forecaster._parse_response(raw)
        assert result.probability == 0.62

    def test_parse_probability_from_prose(self, forecaster):
        raw = 'I estimate the probability: 0.73 based on historical data.'
        result = forecaster._parse_response(raw)
        assert result.probability == 0.73

    def test_parse_malformed_json(self, forecaster):
        raw = "This is not JSON at all and has no probability"
        result = forecaster._parse_response(raw)
        assert result.probability == 0.5  # Fallback

    def test_parse_clamps_probability(self, forecaster):
        raw = json.dumps({"probability": 1.5})
        result = forecaster._parse_response(raw)
        assert result.probability == 0.99

        raw = json.dumps({"probability": -0.1})
        result = forecaster._parse_response(raw)
        assert result.probability == 0.01

    @pytest.mark.asyncio
    async def test_assess_market(self, forecaster, sample_market):
        mock_response = _mock_claude_response(0.42)

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market)

        assert isinstance(result, ForecastResult)
        assert result.probability == 0.42
        assert result.model_used == "claude-sonnet-4-6"
        assert result.tokens_used == 700
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_assess_market_highstakes(self, forecaster, sample_market):
        mock_response = _mock_claude_response(0.60)

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market, position_value=100.0)

        assert result.model_used == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_assess_market_api_error(self, forecaster, sample_market):
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market)

        # Should return fallback (market price)
        assert result.probability == sample_market.yes_price
        assert "failed" in result.reasoning.lower()

    def test_parse_failed_flag_set_on_unparseable(self, forecaster):
        """Regression: when Claude returns unparseable text, parse_failed should be True."""
        raw = "I cannot provide a probability estimate for this."
        result = forecaster._parse_response(raw)
        assert result.parse_failed is True
        assert result.probability == 0.5

    @pytest.mark.asyncio
    async def test_api_error_sets_parse_failed(self, forecaster, sample_market):
        """Regression: fallback forecasts from API errors should have parse_failed=True."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market)
        assert result.parse_failed is True

    @pytest.mark.asyncio
    async def test_rate_limit_sets_parse_failed(self, forecaster, sample_market):
        """Regression: rate limit fallback should have parse_failed=True."""
        import anthropic
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}, json=MagicMock(return_value={})),
                body=None,
            )
        )
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market)
        assert result.parse_failed is True

    def test_no_api_key_raises(self):
        s = Settings()
        s.anthropic_api_key = None
        f = ClaudeForecaster(s)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            f._get_client()

    @pytest.mark.asyncio
    async def test_assess_market_calls_news_researcher(self, forecaster, sample_market):
        """Forecaster should call NewsResearcher when no news_context is provided."""
        mock_response = _mock_claude_response(0.42)
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client

        forecaster.news_researcher.get_context = AsyncMock(
            return_value='RECENT NEWS CONTEXT:\n[1] "Test" (reuters.com)\nSome news...'
        )

        result = await forecaster.assess_market(sample_market)

        forecaster.news_researcher.get_context.assert_awaited_once_with(
            sample_market.question
        )
        assert result.probability == 0.42

    @pytest.mark.asyncio
    async def test_assess_market_skips_news_when_context_provided(
        self, forecaster, sample_market
    ):
        """Forecaster should NOT call NewsResearcher when news_context is already provided."""
        mock_response = _mock_claude_response(0.42)
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client

        forecaster.news_researcher.get_context = AsyncMock(return_value="")

        result = await forecaster.assess_market(
            sample_market, news_context="Pre-existing context"
        )

        forecaster.news_researcher.get_context.assert_not_awaited()
        assert result.probability == 0.42
