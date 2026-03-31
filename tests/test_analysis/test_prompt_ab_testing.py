"""Tests for the Prompt Variant A/B Testing Framework."""

from __future__ import annotations

import pytest

from src.analysis.prompt_ab_testing import (
    PromptVariantManager,
    VariantRecord,
    _modifier_control,
    _modifier_devils_advocate,
    _modifier_explicit_base_rate,
    _modifier_no_market_price,
)
from src.core.models import MarketCategory


# ─── Modifier tests ───


class TestModifiers:
    BASE_TEMPLATE = """Assess the probability of this market resolving YES.

MARKET: Will X happen?
CURRENT MARKET PRICE: 50% (YES)
MARKET CLOSES: 2026-06-01

CONTEXT:
Some news context.

Provide your probability estimate as JSON."""

    def test_control_no_change(self):
        result = _modifier_control(self.BASE_TEMPLATE)
        assert result == self.BASE_TEMPLATE

    def test_explicit_base_rate_added(self):
        result = _modifier_explicit_base_rate(self.BASE_TEMPLATE)
        assert "base rate" in result.lower()
        assert "Before considering specific evidence" in result
        # Original content still present
        assert "Will X happen?" in result

    def test_devils_advocate_added(self):
        result = _modifier_devils_advocate(self.BASE_TEMPLATE)
        assert "OPPOSITE outcome" in result
        # Should appear before the JSON instruction
        idx_advocate = result.index("OPPOSITE")
        idx_json = result.index("Provide your probability estimate as JSON.")
        assert idx_advocate < idx_json

    def test_no_market_price_removed(self):
        result = _modifier_no_market_price(self.BASE_TEMPLATE)
        assert "CURRENT MARKET PRICE" not in result
        # Other content still present
        assert "Will X happen?" in result
        assert "Provide your probability estimate as JSON." in result


# ─── VariantRecord tests ───


class TestVariantRecord:
    def test_initial_state(self):
        record = VariantRecord(name="test")
        assert record.alpha == 1.0
        assert record.beta == 1.0
        assert record.total_predictions == 0
        assert record.avg_brier is None

    def test_sample_returns_float(self):
        record = VariantRecord(name="test")
        sample = record.sample()
        assert 0.0 <= sample <= 1.0

    def test_update_with_good_brier(self):
        record = VariantRecord(name="test")
        record.update(0.05)  # Excellent prediction
        assert record.total_predictions == 1
        assert record.alpha > 1.0  # Success should increase alpha

    def test_update_with_bad_brier(self):
        record = VariantRecord(name="test")
        record.update(0.50)  # Terrible prediction
        assert record.total_predictions == 1
        assert record.beta > 1.5  # Failure should increase beta more

    def test_avg_brier_tracks_correctly(self):
        record = VariantRecord(name="test")
        record.update(0.10)
        record.update(0.20)
        assert record.avg_brier == pytest.approx(0.15, abs=0.001)

    def test_good_variant_gets_higher_samples(self):
        """After many good outcomes, samples should be higher on average."""
        good = VariantRecord(name="good")
        bad = VariantRecord(name="bad")

        for _ in range(50):
            good.update(0.05)  # Consistently good
            bad.update(0.40)   # Consistently bad

        # Sample many times and check average
        good_samples = [good.sample() for _ in range(1000)]
        bad_samples = [bad.sample() for _ in range(1000)]

        assert sum(good_samples) / len(good_samples) > sum(bad_samples) / len(bad_samples)


# ─── PromptVariantManager tests ───


class TestPromptVariantManager:
    def test_disabled_returns_control(self):
        manager = PromptVariantManager(enabled=False)
        name, template = manager.select_variant(
            MarketCategory.POLITICS, "test template"
        )
        assert name == "control"
        assert template == "test template"

    def test_enabled_returns_valid_variant(self):
        manager = PromptVariantManager(enabled=True)
        name, template = manager.select_variant(
            MarketCategory.FED_MACRO, "base template"
        )
        assert name in ("control", "explicit_base_rate", "devils_advocate", "no_market_price")
        assert isinstance(template, str)
        assert len(template) > 0

    def test_record_outcome(self):
        manager = PromptVariantManager()
        manager.record_outcome(MarketCategory.POLITICS, "control", 0.10)
        stats = manager.get_variant_stats()
        assert "Politics" in stats
        assert stats["Politics"]["control"]["total_predictions"] == 1

    def test_record_unknown_variant_safe(self):
        """Recording outcome for unknown variant should not crash."""
        manager = PromptVariantManager()
        manager._ensure_category("Politics")
        manager.record_outcome(MarketCategory.POLITICS, "nonexistent_variant", 0.10)
        # Should not raise

    def test_check_significance_no_data(self):
        manager = PromptVariantManager()
        winners = manager.check_significance()
        assert winners == {}

    def test_check_significance_insufficient_samples(self):
        manager = PromptVariantManager()
        # Record a few outcomes — not enough for significance
        for _ in range(5):
            manager.record_outcome(MarketCategory.POLITICS, "control", 0.10)
            manager.record_outcome(MarketCategory.POLITICS, "explicit_base_rate", 0.20)

        winners = manager.check_significance(min_samples=30)
        assert winners.get("Politics") is None

    def test_check_significance_with_clear_winner(self):
        manager = PromptVariantManager()
        # Record enough outcomes with a clear winner
        for _ in range(35):
            manager.record_outcome(MarketCategory.FED_MACRO, "control", 0.20)
            manager.record_outcome(MarketCategory.FED_MACRO, "explicit_base_rate", 0.10)

        winners = manager.check_significance(min_samples=30)
        assert winners.get("Fed/Macro") == "explicit_base_rate"

    def test_variants_initialized_per_category(self):
        manager = PromptVariantManager()
        manager.select_variant(MarketCategory.POLITICS, "test")
        manager.select_variant(MarketCategory.FED_MACRO, "test")

        stats = manager.get_variant_stats()
        assert "Politics" in stats
        assert "Fed/Macro" in stats
        assert len(stats["Politics"]) == 4  # 4 variants
        assert len(stats["Fed/Macro"]) == 4

    def test_thompson_sampling_converges(self):
        """After many rounds, the manager should favor the better variant."""
        manager = PromptVariantManager()
        cat = MarketCategory.POLITICS

        # Simulate: control gets brier=0.20, explicit_base_rate gets brier=0.08
        for _ in range(100):
            manager.record_outcome(cat, "control", 0.20)
            manager.record_outcome(cat, "explicit_base_rate", 0.08)
            manager.record_outcome(cat, "devils_advocate", 0.25)
            manager.record_outcome(cat, "no_market_price", 0.30)

        # Now select many times and check which variant is picked most
        selections = {}
        for _ in range(200):
            name, _ = manager.select_variant(cat, "base")
            selections[name] = selections.get(name, 0) + 1

        # explicit_base_rate should be selected most often
        assert selections.get("explicit_base_rate", 0) > selections.get("control", 0)
        assert selections.get("explicit_base_rate", 0) > selections.get("devils_advocate", 0)
