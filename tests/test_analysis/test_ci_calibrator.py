"""Tests for the Confidence Interval Calibrator."""

from __future__ import annotations

import pytest

from src.analysis.ci_calibrator import CICalibrator


@pytest.fixture
def calibrator(tmp_db) -> CICalibrator:
    return CICalibrator(tmp_db)


def _seed_ci_records(calibrator, records: list[tuple[str, float, float, float, int]]):
    """Seed resolved CI records.

    Args:
        records: List of (market_id, ci_low, ci_high, predicted_prob, actual_outcome)
    """
    for market_id, ci_low, ci_high, pred_prob, actual in records:
        calibrator.record_ci(market_id, ci_low, ci_high, pred_prob)
        calibrator.resolve_ci(market_id, bool(actual))


class TestRecordAndResolve:
    def test_record_ci(self, calibrator):
        calibrator.record_ci("MKT-1", 0.40, 0.70, 0.55)
        records = calibrator._get_resolved_records()
        assert len(records) == 0  # Not resolved yet

    def test_resolve_ci(self, calibrator):
        calibrator.record_ci("MKT-1", 0.40, 0.70, 0.55)
        calibrator.resolve_ci("MKT-1", True)
        records = calibrator._get_resolved_records()
        assert len(records) == 1
        assert records[0]["actual_outcome"] == 1


class TestCIAccuracy:
    def test_no_records(self, calibrator):
        stats = calibrator.compute_ci_accuracy()
        assert stats["coverage"] is None
        assert stats["count"] == 0

    def test_perfect_coverage(self, calibrator):
        """All outcomes fall within CI."""
        # CI [0.0, 1.0] always covers any outcome
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.0, 1.0, 0.50, i % 2) for i in range(10)
        ])
        stats = calibrator.compute_ci_accuracy()
        assert stats["coverage"] == 1.0

    def test_partial_coverage(self, calibrator):
        """Some outcomes outside CI."""
        # CI [0.45, 0.65] — outcome=1 is outside, outcome=0 is outside
        _seed_ci_records(calibrator, [
            ("MKT-1", 0.45, 0.65, 0.55, 1),  # 1.0 outside [0.45, 0.65]
            ("MKT-2", 0.45, 0.65, 0.55, 0),  # 0.0 outside [0.45, 0.65]
            ("MKT-3", 0.00, 0.60, 0.30, 0),  # 0.0 within [0.00, 0.60]
        ])
        stats = calibrator.compute_ci_accuracy()
        assert stats["coverage"] == pytest.approx(1 / 3, abs=0.01)

    def test_category_filter(self, calibrator):
        calibrator.record_ci("MKT-1", 0.0, 1.0, 0.50, category="Politics")
        calibrator.resolve_ci("MKT-1", True)
        calibrator.record_ci("MKT-2", 0.0, 1.0, 0.50, category="Sports")
        calibrator.resolve_ci("MKT-2", False)

        stats = calibrator.compute_ci_accuracy(category="Politics")
        assert stats["count"] == 1


class TestStretchFactor:
    def test_insufficient_data(self, calibrator):
        """Less than MIN_RECORDS → factor 1.0."""
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.40, 0.60, 0.50, 1) for i in range(5)
        ])
        assert calibrator.get_ci_stretch_factor() == 1.0

    def test_narrow_cis_widen(self, calibrator):
        """Low coverage (CIs too narrow) → factor > 1.0."""
        # CI [0.45, 0.55] — very narrow, outcomes 0 or 1 always outside
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.45, 0.55, 0.50, i % 2) for i in range(25)
        ])
        factor = calibrator.get_ci_stretch_factor()
        assert factor > 1.0  # Should widen

    def test_wide_cis_narrow(self, calibrator):
        """Very high coverage (CIs too wide) → factor < 1.0."""
        # CI [0.0, 1.0] — always covers
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.0, 1.0, 0.50, i % 2) for i in range(25)
        ])
        factor = calibrator.get_ci_stretch_factor()
        assert factor < 1.0  # Should narrow

    def test_good_coverage_no_change(self, calibrator):
        """Coverage near 90% → factor ≈ 1.0."""
        # Need 90% coverage: 23 within CI, 2 outside, out of 25
        records = []
        for i in range(23):
            # Outcome 0 within [0.0, 0.5] or outcome 1 within [0.5, 1.0]
            if i % 2 == 0:
                records.append((f"MKT-{i}", 0.0, 0.50, 0.25, 0))
            else:
                records.append((f"MKT-{i}", 0.50, 1.0, 0.75, 1))
        # 2 outside CI
        records.append(("MKT-OUT1", 0.40, 0.60, 0.50, 1))  # 1.0 outside
        records.append(("MKT-OUT2", 0.40, 0.60, 0.50, 0))  # 0.0 outside

        _seed_ci_records(calibrator, records)
        factor = calibrator.get_ci_stretch_factor()
        assert factor == 1.0


class TestAdjustCI:
    def test_no_adjustment_with_default_factor(self, calibrator):
        """Without enough data, CI should be unchanged."""
        low, high = calibrator.adjust_ci(0.40, 0.60, 0.50)
        assert low == 0.40
        assert high == 0.60

    def test_widening(self, calibrator):
        """When CIs are too narrow, adjust_ci should widen."""
        # Force narrow CI coverage
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.45, 0.55, 0.50, i % 2) for i in range(25)
        ])
        low, high = calibrator.adjust_ci(0.40, 0.60, 0.50)
        # Original width = 0.20, factor > 1.0, so new width > 0.20
        assert (high - low) > 0.20

    def test_clamped_to_valid_range(self, calibrator):
        """Adjusted CI should always be in [0.01, 0.99]."""
        _seed_ci_records(calibrator, [
            (f"MKT-{i}", 0.45, 0.55, 0.50, i % 2) for i in range(25)
        ])
        low, high = calibrator.adjust_ci(0.05, 0.95, 0.50)
        assert low >= 0.01
        assert high <= 0.99
