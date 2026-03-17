"""Tests for prompt templates."""

from src.analysis.prompt_templates import (
    get_template, build_prompt, SYSTEM_PROMPT,
    POLITICS_TEMPLATE, FED_MACRO_TEMPLATE, GEOPOLITICS_TEMPLATE,
    TECH_AI_TEMPLATE, CULTURE_TEMPLATE, GENERAL_TEMPLATE,
    CATEGORY_TEMPLATES,
)
from src.core.models import MarketCategory


class TestGetTemplate:
    def test_politics(self):
        assert get_template(MarketCategory.POLITICS) == POLITICS_TEMPLATE

    def test_fed_macro(self):
        assert get_template(MarketCategory.FED_MACRO) == FED_MACRO_TEMPLATE

    def test_geopolitics(self):
        assert get_template(MarketCategory.GEOPOLITICS) == GEOPOLITICS_TEMPLATE

    def test_tech_ai(self):
        assert get_template(MarketCategory.TECH_AI) == TECH_AI_TEMPLATE

    def test_culture(self):
        assert get_template(MarketCategory.CULTURE) == CULTURE_TEMPLATE

    def test_other_uses_general(self):
        assert get_template(MarketCategory.OTHER) == GENERAL_TEMPLATE

    def test_all_categories_have_templates(self):
        for cat in MarketCategory:
            template = get_template(cat)
            assert template is not None
            assert len(template) > 0


class TestBuildPrompt:
    def test_builds_valid_prompt(self):
        prompt = build_prompt(
            question="Will the Fed cut rates?",
            resolution_criteria="Resolves YES if FOMC cuts rates.",
            market_price=0.34,
            close_date="2026-05-15",
            category=MarketCategory.FED_MACRO,
            news_context="CPI came in lower than expected.",
        )
        assert "Will the Fed cut rates?" in prompt
        assert "Resolves YES if FOMC cuts rates." in prompt
        assert "34%" in prompt
        assert "2026-05-15" in prompt
        assert "CPI came in lower than expected." in prompt

    def test_builds_with_empty_context(self):
        prompt = build_prompt(
            question="Test?",
            resolution_criteria="",
            market_price=0.50,
            close_date="",
            category=MarketCategory.OTHER,
        )
        assert "Test?" in prompt
        assert "No additional context available." in prompt

    def test_market_price_formatting(self):
        prompt = build_prompt(
            question="Test?",
            resolution_criteria="Rules",
            market_price=0.72,
            close_date="2026-06-01",
            category=MarketCategory.POLITICS,
        )
        assert "72%" in prompt


class TestSystemPrompt:
    def test_system_prompt_has_calibration_rules(self):
        assert "calibrated" in SYSTEM_PROMPT.lower()
        assert "JSON" in SYSTEM_PROMPT

    def test_system_prompt_specifies_output_format(self):
        assert "probability" in SYSTEM_PROMPT
        assert "confidence_low" in SYSTEM_PROMPT
        assert "confidence_high" in SYSTEM_PROMPT
        assert "key_factors_for" in SYSTEM_PROMPT
        assert "key_factors_against" in SYSTEM_PROMPT
        assert "uncertainties" in SYSTEM_PROMPT
        assert "reasoning" in SYSTEM_PROMPT
