"""Tests for calibration report formatter."""

from __future__ import annotations

from dataclasses import field
from unittest.mock import MagicMock

import pytest

from src.analysis.calibration_analyzer import (
    CalibrationAnalyzer, CalibrationBin, CalibrationReport, CategoryStats,
)
from src.scripts.calibration_report import format_report


def _make_analyzer(report: CalibrationReport, adjustments: dict | None = None):
    """Create a mock CalibrationAnalyzer returning the given report."""
    analyzer = MagicMock(spec=CalibrationAnalyzer)
    analyzer.generate_report.return_value = report
    analyzer.get_category_adjustments.return_value = adjustments or {}
    return analyzer


class TestFormatReport:
    def test_no_resolved_predictions(self):
        report = CalibrationReport(
            overall_brier=None,
            overall_win_rate=None,
            total_resolved=0,
            total_unresolved=5,
        )
        text = format_report(_make_analyzer(report))
        assert "No resolved predictions yet" in text
        assert "Unresolved predictions: 5" in text

    def test_basic_stats_displayed(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.62,
            total_resolved=50,
            total_unresolved=10,
        )
        text = format_report(_make_analyzer(report))
        assert "0.1500" in text  # Brier score
        assert "62.0%" in text   # Win rate
        assert "50" in text      # Resolved
        assert "10" in text      # Unresolved

    def test_quality_labels(self):
        for brier, expected_quality in [
            (0.08, "Excellent"),
            (0.12, "Good"),
            (0.18, "Decent"),
            (0.22, "Below random"),
            (0.30, "Poor"),
        ]:
            report = CalibrationReport(
                overall_brier=brier,
                overall_win_rate=0.60,
                total_resolved=10,
                total_unresolved=0,
            )
            text = format_report(_make_analyzer(report))
            assert expected_quality in text, f"Expected '{expected_quality}' for Brier={brier}"

    def test_calibration_curve_displayed(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=20,
            total_unresolved=0,
            calibration_curve=[
                CalibrationBin(bin_label="50-60%", predicted_avg=0.55, actual_avg=0.52, count=8),
                CalibrationBin(bin_label="60-70%", predicted_avg=0.65, actual_avg=0.70, count=12),
                CalibrationBin(bin_label="70-80%", predicted_avg=0.75, actual_avg=None, count=0),
            ],
        )
        text = format_report(_make_analyzer(report))
        assert "CALIBRATION CURVE" in text
        assert "50-60%" in text
        assert "60-70%" in text

    def test_empty_bin_shows_dash(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=5,
            total_unresolved=0,
            calibration_curve=[
                CalibrationBin(bin_label="90-100%", predicted_avg=0.0, actual_avg=None, count=0),
            ],
        )
        text = format_report(_make_analyzer(report))
        assert "—" in text

    def test_category_stats_displayed(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=30,
            total_unresolved=0,
            category_stats=[
                CategoryStats("Politics", brier_score=0.10, count=20,
                              avg_predicted=0.60, avg_actual=0.65, bias=0.05),
                CategoryStats("Tech", brier_score=0.25, count=10,
                              avg_predicted=0.55, avg_actual=0.40, bias=-0.15),
            ],
            best_category="Politics",
            worst_category="Tech",
        )
        text = format_report(_make_analyzer(report))
        assert "CATEGORY BREAKDOWN" in text
        assert "Politics" in text
        assert "Tech" in text
        assert "Best category" in text
        assert "Worst category" in text

    def test_category_bias_labels(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=20,
            total_unresolved=0,
            category_stats=[
                CategoryStats("A", brier_score=0.10, count=10,
                              avg_predicted=0.55, avg_actual=0.60, bias=0.05),
                CategoryStats("B", brier_score=0.20, count=10,
                              avg_predicted=0.65, avg_actual=0.50, bias=-0.15),
                CategoryStats("C", brier_score=0.15, count=10,
                              avg_predicted=0.55, avg_actual=0.54, bias=-0.01),
            ],
        )
        text = format_report(_make_analyzer(report))
        assert "under" in text   # bias > 0.02
        assert "over" in text    # bias < -0.02
        assert "neutral" in text # abs(bias) <= 0.02

    def test_adjustments_displayed(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=20,
            total_unresolved=0,
        )
        adjustments = {"Politics": 0.05, "Tech": -0.08}
        text = format_report(_make_analyzer(report, adjustments))
        assert "RECOMMENDED ADJUSTMENTS" in text
        assert "underestimates" in text  # positive adjustment
        assert "overestimates" in text   # negative adjustment

    def test_no_adjustments_message(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=20,
            total_unresolved=0,
        )
        text = format_report(_make_analyzer(report, {}))
        assert "No significant biases" in text

    def test_report_has_header_and_footer(self):
        report = CalibrationReport(
            overall_brier=0.15,
            overall_win_rate=0.60,
            total_resolved=10,
            total_unresolved=0,
        )
        text = format_report(_make_analyzer(report))
        assert "POLYEDGE CALIBRATION REPORT" in text
        assert "=" * 60 in text
