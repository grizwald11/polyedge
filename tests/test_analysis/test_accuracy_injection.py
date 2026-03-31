"""Tests for historical accuracy injection into Claude prompts."""

import pytest

from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.analysis.prompt_templates import build_prompt
from src.core.models import MarketCategory


@pytest.fixture
def analyzer_with_data(tmp_db):
    """CalibrationAnalyzer with seeded resolved calibration records."""
    conn = tmp_db._get_conn()
    # Seed 15 resolved Politics records with known biases
    for i in range(15):
        predicted = 0.60 + (i % 5) * 0.05  # 0.60, 0.65, 0.70, 0.75, 0.80
        # Claude overestimates: actual YES rate is lower than predicted
        actual = 1 if i < 6 else 0  # 6/15 = 40% actual YES rate
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, predicted_probability, market_price_at_prediction,
                actual_outcome, predicted_at, resolved_at, brier_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"POLITICS-{i}",
                f"Will political event {i} happen?",
                predicted,
                predicted - 0.05,
                actual,
                "2026-03-01T00:00:00",
                "2026-03-20T00:00:00",
                (predicted - float(actual)) ** 2,
            ),
        )
    # Ensure market category is set
    for i in range(15):
        conn.execute(
            """INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated)
               VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')""",
            (f"POLITICS-{i}", f"Will political event {i} happen?", "Politics"),
        )
    conn.commit()
    return CalibrationAnalyzer(tmp_db)


@pytest.fixture
def analyzer_sparse(tmp_db):
    """CalibrationAnalyzer with too few records to generate context."""
    conn = tmp_db._get_conn()
    for i in range(5):
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, predicted_probability, market_price_at_prediction,
                actual_outcome, predicted_at, resolved_at, brier_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"SPARSE-{i}",
                f"Sparse event {i}",
                0.60,
                0.55,
                1 if i < 3 else 0,
                "2026-03-01T00:00:00",
                "2026-03-20T00:00:00",
                (0.60 - (1.0 if i < 3 else 0.0)) ** 2,
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated)
               VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')""",
            (f"SPARSE-{i}", f"Sparse event {i}", "Culture"),
        )
    conn.commit()
    return CalibrationAnalyzer(tmp_db)


class TestGetAccuracyContext:
    def test_returns_context_with_sufficient_data(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("Politics")
        assert context != ""
        assert "YOUR HISTORICAL ACCURACY" in context
        assert "Politics" in context
        assert "N=15" in context

    def test_returns_empty_with_insufficient_data(self, analyzer_sparse):
        # Culture has only 5 records, below the 10 minimum
        context = analyzer_sparse.get_accuracy_context("Culture")
        assert context == ""

    def test_returns_empty_for_unknown_category(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("NonexistentCategory")
        assert context == ""

    def test_shows_overestimate_bias(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("Politics")
        # avg predicted ~0.70, actual YES rate 40% → overestimates
        assert "OVERESTIMATE" in context

    def test_shows_brier_score(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("Politics")
        assert "Brier score:" in context

    def test_shows_calibration_bins(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("Politics")
        assert "Calibration by probability range:" in context

    def test_shows_recent_wrong_predictions(self, analyzer_with_data):
        context = analyzer_with_data.get_accuracy_context("Politics")
        # Records with predicted > 0.60 and actual = NO should appear
        assert "Recent incorrect predictions" in context

    def test_no_bias_label_when_neutral(self, tmp_db):
        """When bias is within ±2%, should show 'no significant bias'."""
        conn = tmp_db._get_conn()
        # Create 12 records with balanced outcomes (50% YES rate, 50% predicted)
        for i in range(12):
            predicted = 0.50
            actual = 1 if i < 6 else 0  # 50% actual YES rate
            conn.execute(
                """INSERT INTO calibration_records
                   (market_id, market_question, predicted_probability, market_price_at_prediction,
                    actual_outcome, predicted_at, resolved_at, brier_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"NEUTRAL-{i}",
                    f"Neutral event {i}",
                    predicted,
                    0.48,
                    actual,
                    "2026-03-01T00:00:00",
                    "2026-03-20T00:00:00",
                    (predicted - float(actual)) ** 2,
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated)
                   VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')""",
                (f"NEUTRAL-{i}", f"Neutral event {i}", "Fed"),
            )
        conn.commit()
        analyzer = CalibrationAnalyzer(tmp_db)
        context = analyzer.get_accuracy_context("Fed")
        assert "No significant systematic bias" in context


class TestAccuracyContextInPrompt:
    def test_accuracy_context_injected_into_prompt(self):
        prompt = build_prompt(
            question="Will X happen?",
            resolution_criteria="Yes if X happens",
            market_price=0.50,
            close_date="2026-04-01",
            category=MarketCategory.POLITICS,
            accuracy_context="YOUR HISTORICAL ACCURACY (Politics, N=20):\n- Brier: 0.15",
        )
        assert "YOUR HISTORICAL ACCURACY" in prompt
        assert "Brier: 0.15" in prompt

    def test_empty_accuracy_context_no_artifact(self):
        prompt = build_prompt(
            question="Will X happen?",
            resolution_criteria="Yes if X happens",
            market_price=0.50,
            close_date="2026-04-01",
            category=MarketCategory.POLITICS,
            accuracy_context="",
        )
        # Should not have stray placeholder
        assert "{accuracy_context}" not in prompt

    def test_all_templates_accept_accuracy_context(self):
        """All 6 category templates should accept accuracy_context."""
        for cat in [
            MarketCategory.POLITICS,
            MarketCategory.FED_MACRO,
            MarketCategory.GEOPOLITICS,
            MarketCategory.TECH_AI,
            MarketCategory.CULTURE,
            MarketCategory.OTHER,
        ]:
            prompt = build_prompt(
                question="Test?",
                resolution_criteria="Test.",
                market_price=0.50,
                close_date="2026-04-01",
                category=cat,
                accuracy_context="TEST_ACCURACY",
            )
            assert "TEST_ACCURACY" in prompt
