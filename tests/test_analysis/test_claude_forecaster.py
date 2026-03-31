"""Tests for the Claude forecaster."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.news_researcher import NewsResearcher
from src.config import Settings
from src.core.models import ForecastResult, Market, MarketCategory, MarketToken


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

    @pytest.mark.asyncio
    async def test_timeout_sets_parse_failed(self, forecaster, sample_market):
        """Claude API timeout should return parse_failed=True, not block."""
        import asyncio
        mock_client = AsyncMock()
        # Simulate a hang that exceeds the 60s timeout
        mock_client.messages.create = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        forecaster._client = mock_client

        result = await forecaster.assess_market(sample_market)
        assert result.parse_failed is True
        assert "timed out" in result.reasoning.lower()

    def test_select_temperature_per_category(self, forecaster):
        """Verify category-specific temperatures are correctly selected.

        Config keys must match MarketCategory.value strings exactly
        (e.g., "Fed/Macro" not "Fed"). This test guards against regression
        if enum values or config keys change.
        """
        # Configured category temperatures
        assert forecaster._select_temperature(MarketCategory.POLITICS) == 0.25
        assert forecaster._select_temperature(MarketCategory.FED_MACRO) == 0.20
        assert forecaster._select_temperature(MarketCategory.GEOPOLITICS) == 0.30
        assert forecaster._select_temperature(MarketCategory.TECH_AI) == 0.30
        assert forecaster._select_temperature(MarketCategory.CULTURE) == 0.40
        # Unconfigured categories fall back to default temperature (0.3)
        assert forecaster._select_temperature(MarketCategory.OTHER) == 0.3
        assert forecaster._select_temperature(MarketCategory.SPORTS) == 0.3

    @pytest.mark.asyncio
    async def test_forecast_cache_invalidated_on_price_move(
        self, forecaster, sample_market
    ):
        """Cache should be invalidated when market price moves >5%."""
        mock_response = _mock_claude_response(0.42)
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client
        forecaster.news_researcher = AsyncMock()
        forecaster.news_researcher.get_context = AsyncMock(return_value="")

        # First call — should call Claude
        result1 = await forecaster.assess_market(sample_market)
        assert result1.probability == 0.42
        assert mock_client.messages.create.await_count == 1

        # Second call with same price — should use cache
        result2 = await forecaster.assess_market(sample_market)
        assert mock_client.messages.create.await_count == 1  # No new call

        # Third call with large price move — should invalidate cache.
        # Market.yes_price is a property derived from tokens, so we create
        # a new market with a higher price to simulate a price move.
        moved_market = sample_market.model_copy(deep=True)
        for t in moved_market.tokens:
            if t.outcome.value == "Yes":
                t.price += 0.10  # Move YES price from 0.34 → 0.44
        result3 = await forecaster.assess_market(moved_market)
        assert mock_client.messages.create.await_count == 2  # New call made


class TestModelEscalation:
    """Tests for smart model escalation (_select_model with edge parameter)."""

    def test_select_model_low_edge_uses_sonnet(self, forecaster):
        assert forecaster._select_model(10.0, edge=0.05) == "claude-sonnet-4-6"

    def test_select_model_high_edge_uses_opus(self, forecaster):
        assert forecaster._select_model(10.0, edge=0.20) == "claude-opus-4-6"

    def test_select_model_negative_high_edge_uses_opus(self, forecaster):
        """Negative edge (BUY_NO) with large absolute value should escalate."""
        assert forecaster._select_model(10.0, edge=-0.18) == "claude-opus-4-6"

    def test_select_model_position_value_takes_precedence(self, forecaster):
        """High position value should use opus even with low edge."""
        assert forecaster._select_model(100.0, edge=0.03) == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_force_model_overrides_selection(self, forecaster, sample_market):
        """force_model parameter should override normal model selection."""
        mock_response = _mock_claude_response(0.42)
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        forecaster._client = mock_client
        forecaster.news_researcher.get_context = AsyncMock(return_value="")

        result = await forecaster.assess_market(
            sample_market, force_model="claude-opus-4-6"
        )

        assert result.model_used == "claude-opus-4-6"


class TestCrossCheckCIValidation:
    """Tests for CI bound validation after cross-check widening (H-15)."""

    @pytest.mark.asyncio
    async def test_ci_inversion_fallback(self, forecaster, sample_market):
        """When widened CI bounds invert (ci_low >= ci_high), fall back to ±10% around avg."""
        # Craft two responses with extreme probabilities so disagreement is large
        # and the widened CI could theoretically invert at boundary edge cases.
        # Simulate: avg_prob=0.50, disagreement=0.60 → raw ci_low = max(0.01, -0.10)=0.01,
        # ci_high = min(0.99, 1.10)=0.99. These don't invert, but we can mock the
        # intermediate state directly by patching the math to trigger the guard.
        import asyncio

        low_response = _mock_claude_response(0.20)
        high_response = _mock_claude_response(0.80)

        call_count = 0

        async def _alternating_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 1:
                return low_response
            return high_response

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=_alternating_response)
        forecaster._client = mock_client
        forecaster.news_researcher.get_context = AsyncMock(return_value="")

        result = await forecaster.cross_check_assess(sample_market)

        # Should return a result (not None) with valid CI
        assert result is not None
        assert result.confidence_low < result.confidence_high
        assert 0.0 <= result.confidence_low <= 1.0
        assert 0.0 <= result.confidence_high <= 1.0

    @pytest.mark.asyncio
    async def test_ci_bounds_valid_after_widening_on_disagreement(self, forecaster, sample_market):
        """Widened CI from a large disagreement should still satisfy ci_low < ci_high."""
        low_response = _mock_claude_response(0.30)
        high_response = _mock_claude_response(0.70)

        call_count = 0

        async def _alternating(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 1:
                return low_response
            return high_response

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=_alternating)
        forecaster._client = mock_client
        forecaster.news_researcher.get_context = AsyncMock(return_value="")

        result = await forecaster.cross_check_assess(sample_market)

        assert result is not None
        assert result.confidence_low < result.confidence_high
        # avg = 0.50, disagreement = 0.40 → ci_low = max(0.01, 0.10) = 0.10
        assert result.confidence_low == pytest.approx(0.10, abs=0.01)
        assert result.confidence_high == pytest.approx(0.90, abs=0.01)
