"""Tests for the Superforecaster Decomposition Pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.decomposer import (
    QuestionDecomposer,
    is_compound_question,
)
from src.core.models import ForecastResult, Market, MarketToken


# ─── is_compound_question tests ───


class TestIsCompoundQuestion:
    def test_simple_question_returns_false(self):
        assert not is_compound_question("Will Trump win?")

    def test_short_question_returns_false(self):
        assert not is_compound_question("Rate cut?")

    def test_and_compound_returns_true(self):
        assert is_compound_question(
            "Will Congress pass the bill and the president sign it into law?"
        )

    def test_before_compound_returns_true(self):
        assert is_compound_question(
            "Will the Fed cut rates before the next unemployment report is released?"
        )

    def test_conditional_returns_true(self):
        assert is_compound_question(
            "If the Democrats win the Senate, will they pass healthcare reform?"
        )

    def test_both_keyword_returns_true(self):
        assert is_compound_question(
            "Will both the House and the Senate approve the spending bill?"
        )

    def test_nominate_confirm_returns_true(self):
        assert is_compound_question(
            "Will the president nominate a new Supreme Court justice and will they be confirmed?"
        )

    def test_pass_sign_returns_true(self):
        assert is_compound_question(
            "Will Congress pass the infrastructure bill and the president sign it?"
        )

    def test_plain_politics_returns_false(self):
        assert not is_compound_question(
            "Will the Federal Reserve cut interest rates at the May 2026 meeting?"
        )


# ─── Recombine tests ───


class TestRecombine:
    def test_and_multiplication(self):
        results = [{"probability": 0.8}, {"probability": 0.7}]
        combined = QuestionDecomposer._recombine("AND", results)
        assert combined == pytest.approx(0.56, abs=0.001)

    def test_and_three_events(self):
        results = [{"probability": 0.9}, {"probability": 0.8}, {"probability": 0.7}]
        combined = QuestionDecomposer._recombine("AND", results)
        assert combined == pytest.approx(0.504, abs=0.001)

    def test_or_complement(self):
        results = [{"probability": 0.3}, {"probability": 0.4}]
        combined = QuestionDecomposer._recombine("OR", results)
        # 1 - (0.7 * 0.6) = 1 - 0.42 = 0.58
        assert combined == pytest.approx(0.58, abs=0.001)

    def test_or_high_probs(self):
        results = [{"probability": 0.9}, {"probability": 0.8}]
        combined = QuestionDecomposer._recombine("OR", results)
        # 1 - (0.1 * 0.2) = 1 - 0.02 = 0.98
        assert combined == pytest.approx(0.98, abs=0.001)

    def test_conditional_multiplication(self):
        results = [{"probability": 0.6}, {"probability": 0.8}]
        combined = QuestionDecomposer._recombine("CONDITIONAL", results)
        assert combined == pytest.approx(0.48, abs=0.001)

    def test_single_result(self):
        results = [{"probability": 0.75}]
        combined = QuestionDecomposer._recombine("AND", results)
        assert combined == pytest.approx(0.75, abs=0.001)

    def test_empty_results(self):
        assert QuestionDecomposer._recombine("AND", []) is None

    def test_unknown_type(self):
        results = [{"probability": 0.5}]
        assert QuestionDecomposer._recombine("UNKNOWN", results) is None


# ─── Parse decomposition tests ───


class TestParseDecomposition:
    def setup_method(self):
        self.decomposer = QuestionDecomposer(MagicMock())

    def test_valid_json(self):
        raw = json.dumps({
            "decomposition_type": "AND",
            "sub_questions": [
                {"question": "Will Congress pass?", "base_rate_hint": "~60% of bills pass committee"},
                {"question": "Will president sign?", "base_rate_hint": "~90% if party-aligned"},
            ],
            "reasoning": "Two independent steps required",
        })
        result = self.decomposer._parse_decomposition(raw)
        assert result is not None
        assert result["decomposition_type"] == "AND"
        assert len(result["sub_questions"]) == 2

    def test_json_in_code_block(self):
        raw = '```json\n{"decomposition_type": "OR", "sub_questions": [{"question": "A"}, {"question": "B"}]}\n```'
        result = self.decomposer._parse_decomposition(raw)
        assert result is not None
        assert result["decomposition_type"] == "OR"

    def test_atomic_type(self):
        raw = json.dumps({
            "decomposition_type": "ATOMIC",
            "sub_questions": [],
        })
        result = self.decomposer._parse_decomposition(raw)
        assert result is not None
        assert result["decomposition_type"] == "ATOMIC"

    def test_invalid_type_defaults_atomic(self):
        raw = json.dumps({
            "decomposition_type": "WEIRD",
            "sub_questions": [{"question": "A"}],
        })
        result = self.decomposer._parse_decomposition(raw)
        assert result is not None
        assert result["decomposition_type"] == "ATOMIC"

    def test_string_sub_questions(self):
        raw = json.dumps({
            "decomposition_type": "AND",
            "sub_questions": ["Will A happen?", "Will B happen?"],
        })
        result = self.decomposer._parse_decomposition(raw)
        assert result is not None
        assert len(result["sub_questions"]) == 2
        assert result["sub_questions"][0]["question"] == "Will A happen?"

    def test_garbage_input(self):
        result = self.decomposer._parse_decomposition("This is not JSON at all")
        assert result is None


# ─── Integration tests with mocked Claude ───


def _make_market(question: str = "Will Congress pass the bill and the president sign it?") -> Market:
    return Market(
        ticker="TEST-COMPOUND",
        question=question,
        description="Resolves YES if both conditions are met.",
        tokens=[
            MarketToken(token_id="TEST-COMPOUND_yes", outcome="Yes", price=0.40),
            MarketToken(token_id="TEST-COMPOUND_no", outcome="No", price=0.60),
        ],
        volume_24h=50000,
        liquidity=20000,
    )


def _mock_forecaster(decomp_response: str, sub_responses: list[str]):
    """Create a mock ClaudeForecaster that returns canned responses."""
    forecaster = MagicMock()
    forecaster._select_model.return_value = "claude-sonnet-4-6"
    forecaster.settings = MagicMock()
    forecaster.settings.claude.api_timeout_seconds = 60
    forecaster._track_tokens = MagicMock()

    # Queue responses: first is decomposition, rest are sub-questions
    all_responses = [decomp_response] + sub_responses
    call_count = {"n": 0}

    async def mock_call_claude(prompt, model, temperature, timeout):
        idx = call_count["n"]
        call_count["n"] += 1
        resp = MagicMock()
        resp.content = [MagicMock(text=all_responses[min(idx, len(all_responses) - 1)])]
        resp.usage = MagicMock(input_tokens=100, output_tokens=200)
        return resp

    forecaster._call_claude = mock_call_claude
    forecaster._extract_text = lambda r: r.content[0].text if r.content else None

    # Use real _parse_response from ClaudeForecaster
    from src.analysis.claude_forecaster import ClaudeForecaster
    forecaster._parse_response = ClaudeForecaster._parse_response.__get__(forecaster)
    # Provide _build_forecast and _validate_prose_extraction since _parse_response calls them
    forecaster._build_forecast = ClaudeForecaster._build_forecast.__get__(forecaster)
    # If _validate_prose_extraction exists, bind it too
    if hasattr(ClaudeForecaster, '_validate_prose_extraction'):
        forecaster._validate_prose_extraction = ClaudeForecaster._validate_prose_extraction.__get__(forecaster)

    return forecaster


@pytest.mark.asyncio
async def test_decompose_and_assess_and_type():
    """Test full decomposition pipeline with AND type."""
    decomp_json = json.dumps({
        "decomposition_type": "AND",
        "sub_questions": [
            {"question": "Will Congress pass the bill?", "base_rate_hint": "60% of bills pass"},
            {"question": "Will the president sign it?", "base_rate_hint": "90% if party-aligned"},
        ],
        "reasoning": "Two sequential steps",
    })
    sub1_json = json.dumps({
        "probability": 0.65,
        "confidence_low": 0.55,
        "confidence_high": 0.75,
        "key_factors_for": ["Committee support"],
        "key_factors_against": ["Opposition filibuster"],
        "uncertainties": ["Vote timing"],
        "reasoning": "Base rate 60%, adjusted up for committee support",
    })
    sub2_json = json.dumps({
        "probability": 0.85,
        "confidence_low": 0.75,
        "confidence_high": 0.95,
        "key_factors_for": ["Party alignment"],
        "key_factors_against": ["Veto threats"],
        "uncertainties": ["Executive priorities"],
        "reasoning": "Base rate 90%, adjusted down slightly for veto rhetoric",
    })

    forecaster = _mock_forecaster(decomp_json, [sub1_json, sub2_json])
    decomposer = QuestionDecomposer(forecaster)
    market = _make_market()

    result = await decomposer.decompose_and_assess(market, "Recent news context")

    assert result is not None
    # AND: 0.65 * 0.85 = 0.5525
    assert result.probability == pytest.approx(0.5525, abs=0.01)
    assert "Decomposed (AND)" in result.reasoning
    assert result.tokens_used > 0


@pytest.mark.asyncio
async def test_decompose_and_assess_or_type():
    """Test decomposition with OR type."""
    decomp_json = json.dumps({
        "decomposition_type": "OR",
        "sub_questions": [
            {"question": "Path A?", "base_rate_hint": "30%"},
            {"question": "Path B?", "base_rate_hint": "40%"},
        ],
    })
    sub1_json = json.dumps({
        "probability": 0.30,
        "confidence_low": 0.20, "confidence_high": 0.40,
        "key_factors_for": [], "key_factors_against": [],
        "uncertainties": [], "reasoning": "30%",
    })
    sub2_json = json.dumps({
        "probability": 0.40,
        "confidence_low": 0.30, "confidence_high": 0.50,
        "key_factors_for": [], "key_factors_against": [],
        "uncertainties": [], "reasoning": "40%",
    })

    forecaster = _mock_forecaster(decomp_json, [sub1_json, sub2_json])
    decomposer = QuestionDecomposer(forecaster)
    market = _make_market("Will either path A or path B succeed?")

    result = await decomposer.decompose_and_assess(market)
    assert result is not None
    # OR: 1 - (0.7 * 0.6) = 0.58
    assert result.probability == pytest.approx(0.58, abs=0.01)


@pytest.mark.asyncio
async def test_atomic_falls_back_to_none():
    """Test that ATOMIC decomposition returns None (caller should use single-shot)."""
    decomp_json = json.dumps({
        "decomposition_type": "ATOMIC",
        "sub_questions": [],
    })

    forecaster = _mock_forecaster(decomp_json, [])
    decomposer = QuestionDecomposer(forecaster)
    market = _make_market("Will the Fed cut rates?")

    result = await decomposer.decompose_and_assess(market)
    assert result is None


@pytest.mark.asyncio
async def test_decomposition_failure_returns_none():
    """Test that Claude API failure returns None gracefully."""
    forecaster = MagicMock()
    forecaster._select_model.return_value = "claude-sonnet-4-6"
    forecaster.settings = MagicMock()
    forecaster.settings.claude.api_timeout_seconds = 60

    async def fail(*args, **kwargs):
        raise RuntimeError("API failure")

    forecaster._call_claude = fail

    decomposer = QuestionDecomposer(forecaster)
    market = _make_market()

    result = await decomposer.decompose_and_assess(market)
    assert result is None
