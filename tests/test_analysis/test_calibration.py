"""Tests for calibration tracker."""

from __future__ import annotations

import pytest

from src.analysis.calibration import CalibrationTracker
from src.core.models import CalibrationRecord, StrategyName


@pytest.fixture
def tracker(tmp_db) -> CalibrationTracker:
    return CalibrationTracker(tmp_db)


def _seed_predictions(tracker: CalibrationTracker, n: int = 10):
    """Seed resolved predictions for testing."""
    for i in range(n):
        prob = 0.1 * (i + 1)  # 0.1, 0.2, ..., 1.0
        tracker.log_prediction(
            market_id=f"MKT-{i}",
            market_question=f"Market {i}?",
            predicted_probability=prob,
            market_price=prob - 0.05,
        )
    # Resolve: higher predictions tend to be YES
    for i in range(n):
        outcome = i >= 5  # Markets 5-9 resolve YES (prob 0.6-1.0)
        tracker.resolve_prediction(f"MKT-{i}", outcome)


class TestLogPrediction:
    def test_log_returns_id(self, tracker):
        row_id = tracker.log_prediction(
            market_id="FED-RATE",
            market_question="Will Fed cut?",
            predicted_probability=0.42,
            market_price=0.34,
        )
        assert row_id > 0

    def test_log_with_strategy(self, tracker):
        row_id = tracker.log_prediction(
            market_id="FED-RATE",
            market_question="Will Fed cut?",
            predicted_probability=0.42,
            market_price=0.34,
            strategy=StrategyName.OBVIOUS_NO,
        )
        assert row_id > 0


class TestResolvePrediction:
    def test_resolve_yes(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.70, 0.60)
        count = tracker.resolve_prediction("MKT-1", True)
        assert count == 1

    def test_resolve_no(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.30, 0.40)
        count = tracker.resolve_prediction("MKT-1", False)
        assert count == 1

    def test_resolve_multiple(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.70, 0.60)
        tracker.log_prediction("MKT-1", "Q?", 0.75, 0.65)  # Second prediction same market
        count = tracker.resolve_prediction("MKT-1", True)
        assert count == 2

    def test_resolve_nonexistent(self, tracker):
        count = tracker.resolve_prediction("NOPE", True)
        assert count == 0

    def test_double_resolve_ignored(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.70, 0.60)
        tracker.resolve_prediction("MKT-1", True)
        count = tracker.resolve_prediction("MKT-1", False)  # Already resolved
        assert count == 0


class TestBrierScore:
    def test_perfect_prediction(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 1.0, 0.90)
        tracker.resolve_prediction("MKT-1", True)

        brier = tracker.calculate_brier_score()
        assert brier == pytest.approx(0.0)

    def test_worst_prediction(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 1.0, 0.90)
        tracker.resolve_prediction("MKT-1", False)

        brier = tracker.calculate_brier_score()
        assert brier == pytest.approx(1.0)

    def test_moderate_prediction(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.70, 0.60)
        tracker.resolve_prediction("MKT-1", True)

        brier = tracker.calculate_brier_score()
        assert brier == pytest.approx(0.09)  # (0.70 - 1.0)^2

    def test_no_resolved_returns_none(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.50, 0.50)
        assert tracker.calculate_brier_score() is None

    def test_multiple_predictions(self, tracker):
        _seed_predictions(tracker)
        brier = tracker.calculate_brier_score()
        assert brier is not None
        assert 0.0 <= brier <= 1.0

    def test_strategy_filter(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.70, 0.60, StrategyName.AI_PROBABILITY)
        tracker.log_prediction("MKT-2", "Q?", 0.90, 0.95, StrategyName.OBVIOUS_NO)
        tracker.resolve_prediction("MKT-1", True)
        tracker.resolve_prediction("MKT-2", True)

        brier_ai = tracker.calculate_brier_score(strategy=StrategyName.AI_PROBABILITY)
        brier_no = tracker.calculate_brier_score(strategy=StrategyName.OBVIOUS_NO)

        assert brier_ai != brier_no


class TestCalibrationBins:
    def test_bins_structure(self, tracker):
        _seed_predictions(tracker)
        bins = tracker.get_calibration_bins()

        assert len(bins) == 10
        for b in bins:
            assert "bin" in b
            assert "predicted_avg" in b
            assert "actual_avg" in b
            assert "count" in b

    def test_empty_bins(self, tracker):
        bins = tracker.get_calibration_bins()
        assert bins == []

    def test_custom_bin_count(self, tracker):
        _seed_predictions(tracker)
        bins = tracker.get_calibration_bins(n_bins=5)
        assert len(bins) == 5


class TestWinRate:
    def test_perfect_win_rate(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.80, 0.60)
        tracker.log_prediction("MKT-2", "Q?", 0.20, 0.30)
        tracker.resolve_prediction("MKT-1", True)   # Predicted >0.5, was YES ✓
        tracker.resolve_prediction("MKT-2", False)   # Predicted <0.5, was NO ✓

        assert tracker.get_win_rate() == pytest.approx(1.0)

    def test_zero_win_rate(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.80, 0.60)
        tracker.log_prediction("MKT-2", "Q?", 0.20, 0.30)
        tracker.resolve_prediction("MKT-1", False)   # Wrong
        tracker.resolve_prediction("MKT-2", True)    # Wrong

        assert tracker.get_win_rate() == pytest.approx(0.0)

    def test_no_data_returns_none(self, tracker):
        assert tracker.get_win_rate() is None


class TestSummary:
    def test_summary_structure(self, tracker):
        _seed_predictions(tracker)
        summary = tracker.get_summary()

        assert "brier_score" in summary
        assert "win_rate" in summary
        assert "resolved_count" in summary
        assert "unresolved_count" in summary
        assert "total_predictions" in summary
        assert summary["resolved_count"] == 10
        assert summary["unresolved_count"] == 0

    def test_summary_with_unresolved(self, tracker):
        tracker.log_prediction("MKT-1", "Q?", 0.50, 0.50)
        summary = tracker.get_summary()

        assert summary["resolved_count"] == 0
        assert summary["unresolved_count"] == 1
        assert summary["brier_score"] is None
