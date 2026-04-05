"""Tests for prompt_builder.py — template rendering, model/temperature selection."""

from __future__ import annotations

import pytest

from src.analysis.prompt_builder import (
    select_model,
    select_temperature,
    validate_resolution_criteria,
)
from src.core.models import MarketCategory


class TestValidateResolutionCriteria:
    def test_returns_description_when_adequate(self):
        desc = "This market resolves YES if the Fed cuts rates by 25bps at the next FOMC meeting."
        assert validate_resolution_criteria(desc) == desc

    def test_adds_caution_when_empty(self):
        result = validate_resolution_criteria("")
        assert "WARNING" in result
        assert "ambiguous" in result.lower()

    def test_adds_caution_when_none(self):
        result = validate_resolution_criteria(None)
        assert "WARNING" in result

    def test_adds_caution_when_too_short(self):
        result = validate_resolution_criteria("Yes or no.")
        assert "WARNING" in result
        # Original text should still be present
        assert "Yes or no." in result

    def test_preserves_original_text_with_caution(self):
        short = "Short criteria."
        result = validate_resolution_criteria(short)
        assert short in result
        assert "WARNING" in result

    def test_whitespace_only_treated_as_empty(self):
        result = validate_resolution_criteria("   ")
        assert "WARNING" in result

    def test_exactly_20_chars_is_too_short(self):
        desc = "x" * 19  # 19 chars < 20
        result = validate_resolution_criteria(desc)
        assert "WARNING" in result

    def test_21_chars_is_adequate(self):
        desc = "x" * 21
        result = validate_resolution_criteria(desc)
        assert "WARNING" not in result


class TestSelectModel:
    def test_high_value_uses_highstakes_model(self):
        model = select_model(
            position_value=100.0,
            edge=0.05,
            highstakes_threshold=50.0,
            model_highstakes="claude-opus-4-6",
            model_primary="claude-sonnet-4-6",
        )
        assert model == "claude-opus-4-6"

    def test_low_value_uses_primary_model(self):
        model = select_model(
            position_value=10.0,
            edge=0.05,
            highstakes_threshold=50.0,
            model_highstakes="claude-opus-4-6",
            model_primary="claude-sonnet-4-6",
        )
        assert model == "claude-sonnet-4-6"

    def test_high_edge_triggers_highstakes(self):
        model = select_model(
            position_value=10.0,
            edge=0.20,
            highstakes_threshold=50.0,
            model_highstakes="claude-opus-4-6",
            model_primary="claude-sonnet-4-6",
            edge_highstakes_threshold=0.15,
        )
        assert model == "claude-opus-4-6"

    def test_negative_high_edge_triggers_highstakes(self):
        model = select_model(
            position_value=10.0,
            edge=-0.20,
            highstakes_threshold=50.0,
            model_highstakes="claude-opus-4-6",
            model_primary="claude-sonnet-4-6",
            edge_highstakes_threshold=0.15,
        )
        assert model == "claude-opus-4-6"

    def test_edge_at_threshold_uses_primary(self):
        model = select_model(
            position_value=10.0,
            edge=0.15,
            highstakes_threshold=50.0,
            model_highstakes="claude-opus-4-6",
            model_primary="claude-sonnet-4-6",
            edge_highstakes_threshold=0.15,
        )
        # abs(0.15) > 0.15 is False
        assert model == "claude-sonnet-4-6"

    def test_position_value_takes_priority_over_edge(self):
        model = select_model(
            position_value=100.0,
            edge=0.01,
            highstakes_threshold=50.0,
            model_highstakes="opus",
            model_primary="sonnet",
        )
        assert model == "opus"


class TestSelectTemperature:
    def test_returns_category_temperature(self):
        temps = {"Politics": 0.2, "Tech/AI": 0.4}
        result = select_temperature(MarketCategory.POLITICS, temps, default_temperature=0.3)
        assert result == 0.2

    def test_falls_back_to_default(self):
        temps = {"Politics": 0.2}
        result = select_temperature(MarketCategory.CRYPTO, temps, default_temperature=0.3)
        assert result == 0.3

    def test_empty_temps_uses_default(self):
        result = select_temperature(MarketCategory.POLITICS, {}, default_temperature=0.5)
        assert result == 0.5

    def test_zero_temperature_is_valid(self):
        temps = {"Politics": 0.0}
        result = select_temperature(MarketCategory.POLITICS, temps, default_temperature=0.3)
        assert result == 0.0
