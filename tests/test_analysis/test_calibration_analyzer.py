"""Tests for calibration analyzer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.analysis.calibration_analyzer import CalibrationAnalyzer, CalibrationReport
from src.storage.database import Database


@pytest.fixture
def analyzer(tmp_db) -> CalibrationAnalyzer:
    return CalibrationAnalyzer(tmp_db)


def _seed_resolved(tmp_db, predictions: list[tuple[str, str, float, int]]):
    """Seed resolved predictions.

    Args:
        predictions: List of (market_id, category, predicted_prob, actual_outcome)
    """
    conn = tmp_db._get_conn()
    now = datetime.now(timezone.utc).isoformat()

    for market_id, category, pred_prob, actual in predictions:
        # Ensure market exists for the join
        conn.execute(
            """INSERT OR IGNORE INTO markets
               (ticker, question, category, first_seen, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            (market_id, f"Question about {market_id}?", category, now, now),
        )
        brier = (pred_prob - float(actual)) ** 2
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, strategy, predicted_probability,
                market_price_at_prediction, actual_outcome, predicted_at,
                resolved_at, brier_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market_id,
                f"Question about {market_id}?",
                "ai_probability",
                pred_prob,
                pred_prob - 0.05,
                actual,
                now,
                now,
                brier,
            ),
        )
    conn.commit()


class TestGenerateReport:
    def test_empty_report(self, analyzer):
        report = analyzer.generate_report()
        assert report.overall_brier is None
        assert report.total_resolved == 0

    def test_basic_report(self, analyzer, tmp_db):
        # market_price_at_prediction = pred_prob - 0.05 (set by _seed_resolved)
        # MKT-1: pred=0.70, threshold=0.65, actual=YES → WIN
        # MKT-2: pred=0.30, threshold=0.25, actual=NO  → 0.30>0.25 so NOT a NO signal → NO WIN
        # MKT-3: pred=0.80, threshold=0.75, actual=YES → WIN
        # win_rate = 2/3
        _seed_resolved(tmp_db, [
            ("MKT-1", "Politics", 0.70, 1),  # Brier = 0.09
            ("MKT-2", "Politics", 0.30, 0),  # Brier = 0.09
            ("MKT-3", "Economics", 0.80, 1),  # Brier = 0.04
        ])

        report = analyzer.generate_report()
        assert report.total_resolved == 3
        assert report.overall_brier is not None
        assert report.overall_brier == pytest.approx((0.09 + 0.09 + 0.04) / 3, abs=0.01)
        assert report.overall_win_rate == pytest.approx(2 / 3, abs=0.01)

    def test_report_with_unresolved(self, analyzer, tmp_db):
        _seed_resolved(tmp_db, [("MKT-1", "Politics", 0.70, 1)])

        # Add unresolved
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, strategy, predicted_probability,
                market_price_at_prediction, predicted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("MKT-OPEN", "Open?", "ai_probability", 0.50, 0.45, now),
        )
        conn.commit()

        report = analyzer.generate_report()
        assert report.total_resolved == 1
        assert report.total_unresolved == 1

    def test_report_category_stats(self, analyzer, tmp_db):
        _seed_resolved(tmp_db, [
            ("MKT-1", "Politics", 0.70, 1),
            ("MKT-2", "Politics", 0.60, 0),
            ("MKT-3", "Economics", 0.80, 1),
            ("MKT-4", "Economics", 0.90, 1),
        ])

        report = analyzer.generate_report()
        assert len(report.category_stats) == 2

        cat_names = {cs.category for cs in report.category_stats}
        assert "Politics" in cat_names
        assert "Economics" in cat_names

    def test_best_worst_category(self, analyzer, tmp_db):
        # Need >= 5 records per category for best/worst ranking
        _seed_resolved(tmp_db, [
            *[(f"POL-{i}", "Politics", 0.90, 0) for i in range(5)],   # Bad: Brier = 0.81
            *[(f"ECO-{i}", "Economics", 0.90, 1) for i in range(5)],  # Good: Brier = 0.01
        ])

        report = analyzer.generate_report()
        assert report.best_category == "Economics"
        assert report.worst_category == "Politics"


class TestCalibrationCurve:
    def test_curve_bins(self, analyzer, tmp_db):
        _seed_resolved(tmp_db, [
            ("MKT-1", "Other", 0.15, 0),
            ("MKT-2", "Other", 0.25, 0),
            ("MKT-3", "Other", 0.75, 1),
            ("MKT-4", "Other", 0.85, 1),
        ])

        report = analyzer.generate_report()
        assert len(report.calibration_curve) == 10

        # Check that bins with data have correct counts
        populated = [b for b in report.calibration_curve if b.count > 0]
        total = sum(b.count for b in populated)
        assert total == 4


class TestCategoryAdjustments:
    def test_no_data(self, analyzer):
        adjustments = analyzer.get_category_adjustments()
        assert adjustments == {}

    def test_detects_overestimate(self, analyzer, tmp_db):
        """Claude predicts high, actuals are low → overestimate → negative adjustment."""
        # Use 15+ records to meet the new minimum sample size threshold
        _seed_resolved(tmp_db, [
            (f"MKT-{i}", "Politics", 0.75, 0) for i in range(1, 14)
        ] + [
            ("MKT-14", "Politics", 0.75, 1),
            ("MKT-15", "Politics", 0.80, 1),
        ])

        adjustments = analyzer.get_category_adjustments()
        # avg_predicted ≈ 0.76, avg_actual ≈ 0.13 → large negative bias
        assert "Politics" in adjustments
        assert adjustments["Politics"] < 0  # Overestimates

    def test_detects_underestimate(self, analyzer, tmp_db):
        """Claude predicts low, actuals are high → underestimate → positive adjustment."""
        # Use 15+ records to meet the new minimum sample size threshold
        _seed_resolved(tmp_db, [
            (f"MKT-{i}", "Economics", 0.30, 1) for i in range(1, 14)
        ] + [
            ("MKT-14", "Economics", 0.25, 0),
            ("MKT-15", "Economics", 0.35, 0),
        ])

        adjustments = analyzer.get_category_adjustments()
        # avg_predicted ≈ 0.30, avg_actual ≈ 0.87 → large positive bias
        assert "Economics" in adjustments
        assert adjustments["Economics"] > 0  # Underestimates

    def test_small_bias_ignored(self, analyzer, tmp_db):
        """Biases under 3% should not produce adjustments."""
        # 8 YES, 7 NO with predictions near 0.50 → minimal bias (15 total)
        _seed_resolved(tmp_db, [
            (f"MKT-{i}", "Other", 0.50, 1) for i in range(1, 9)
        ] + [
            (f"MKT-{i}", "Other", 0.50, 0) for i in range(9, 16)
        ])

        adjustments = analyzer.get_category_adjustments()
        # avg_predicted = 0.50, avg_actual ≈ 0.53 → tiny bias, below threshold
        assert "Other" not in adjustments

    def test_minimum_sample_size(self, analyzer, tmp_db):
        """Categories with fewer than 15 predictions should be excluded."""
        _seed_resolved(tmp_db, [
            (f"MKT-{i}", "Rare", 0.90, 0) for i in range(1, 15)  # 14 records — just below threshold
        ])

        adjustments = analyzer.get_category_adjustments()
        assert "Rare" not in adjustments


class TestDatabaseMethods:
    """Test the new database methods added for resolution tracking."""

    def test_store_prediction(self, tmp_db):
        row_id = tmp_db.store_prediction(
            market_ticker="MKT-TEST",
            predicted_probability=0.65,
            predicted_side="BUY_YES",
            market_price=0.55,
            strategy="ai_probability",
            confidence_low=0.55,
            confidence_high=0.75,
            market_question="Will X happen?",
        )
        assert row_id > 0

        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["market_id"] == "MKT-TEST"
        assert unresolved[0]["predicted_probability"] == 0.65

    def test_get_resolved_predictions(self, tmp_db):
        _seed_resolved(tmp_db, [
            ("MKT-1", "Other", 0.70, 1),
            ("MKT-2", "Other", 0.30, 0),
        ])

        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 2

    def test_get_resolved_by_strategy(self, tmp_db):
        _seed_resolved(tmp_db, [("MKT-1", "Other", 0.70, 1)])

        resolved = tmp_db.get_resolved_predictions(strategy="ai_probability")
        assert len(resolved) == 1

        resolved = tmp_db.get_resolved_predictions(strategy="obvious_no")
        assert len(resolved) == 0

    def test_update_resolution(self, tmp_db):
        tmp_db.store_prediction("MKT-1", 0.70, "YES", 0.60)

        count = tmp_db.update_resolution("MKT-1", actual_outcome=1, brier_score=0.09)
        assert count == 1

        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["actual_outcome"] == 1
        assert resolved[0]["brier_score"] == pytest.approx(0.09)

    def test_update_resolution_nonexistent(self, tmp_db):
        count = tmp_db.update_resolution("NOPE", actual_outcome=1)
        assert count == 0

    def test_schema_migration_adds_columns(self, tmp_path):
        """Verify migration adds brier_score and profit_loss columns."""
        db = Database(db_path=str(tmp_path / "test.db"), wal_mode=True)

        # The columns should exist after initialization
        conn = db._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calibration_records)").fetchall()}

        assert "brier_score" in cols
        assert "profit_loss" in cols

    def test_get_latest_prediction(self, tmp_db):
        """get_latest_prediction returns most recent prediction for a market."""
        tmp_db.store_prediction("MKT-A", 0.60, "YES", 0.50)
        tmp_db.store_prediction("MKT-A", 0.65, "YES", 0.55)

        latest = tmp_db.get_latest_prediction("MKT-A")
        assert latest is not None
        assert latest["predicted_probability"] == 0.65
        assert latest["market_price_at_prediction"] == 0.55

    def test_get_latest_prediction_none(self, tmp_db):
        """get_latest_prediction returns None for unknown market."""
        assert tmp_db.get_latest_prediction("NOPE") is None
