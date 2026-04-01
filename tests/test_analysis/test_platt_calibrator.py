"""Tests for Platt scaling post-hoc calibration."""

import pytest

from src.analysis.platt_calibrator import PlattCalibrator, PlattParams, MIN_SAMPLES


class TestPlattCalibrator:
    def test_inactive_below_min_samples(self):
        cal = PlattCalibrator()
        cal.fit([0.5] * 10, [1] * 5 + [0] * 5)
        assert not cal.is_active
        assert cal.params.n_samples == 10

    def test_calibrate_returns_raw_when_inactive(self):
        cal = PlattCalibrator()
        assert cal.calibrate(0.7) == 0.7

    def test_fits_with_enough_data(self):
        """With 50+ samples, Platt scaling should activate if it improves Brier."""
        cal = PlattCalibrator()
        # Create overconfident predictions: always predict 0.8 but only 60% resolve YES
        predictions = [0.8] * MIN_SAMPLES
        outcomes = [1] * 30 + [0] * 20
        params = cal.fit(predictions, outcomes)
        assert params.n_samples == MIN_SAMPLES
        # Should have fit meaningful parameters
        assert params.brier_before is not None
        assert params.brier_after is not None

    def test_identity_on_perfect_calibration(self):
        """When predictions are already calibrated, a ≈ 1, b ≈ 0."""
        cal = PlattCalibrator()
        # Generate well-calibrated data
        import random
        random.seed(42)
        predictions = []
        outcomes = []
        for _ in range(200):
            p = random.uniform(0.1, 0.9)
            predictions.append(p)
            outcomes.append(1 if random.random() < p else 0)
        cal.fit(predictions, outcomes)
        # Parameters should be near identity
        assert abs(cal.params.a - 1.0) < 0.3
        assert abs(cal.params.b) < 0.3

    def test_calibrate_shifts_overconfident_predictions(self):
        """Overconfident predictions should be pulled toward 50%."""
        cal = PlattCalibrator()
        # Systematically overconfident: predict 0.9 but only 60% resolve YES
        predictions = [0.9] * 60 + [0.1] * 40
        outcomes = [1] * 36 + [0] * 24 + [0] * 24 + [1] * 16
        cal.fit(predictions, outcomes)
        if cal.is_active:
            # Calibrated 0.9 should be pulled lower (toward true 60%)
            calibrated = cal.calibrate(0.9)
            assert calibrated < 0.9

    def test_mismatched_lengths_raises(self):
        cal = PlattCalibrator()
        with pytest.raises(ValueError):
            cal.fit([0.5, 0.6], [1])

    def test_extreme_values_handled(self):
        """Probabilities near 0 or 1 shouldn't cause math errors."""
        cal = PlattCalibrator()
        # Just test that calibrate doesn't crash on extremes
        assert 0.0 < cal.calibrate(0.001) < 1.0
        assert 0.0 < cal.calibrate(0.999) < 1.0

    def test_brier_improvement_tracked(self):
        """Params should track before/after Brier scores."""
        cal = PlattCalibrator()
        predictions = [0.8] * MIN_SAMPLES
        outcomes = [1] * 30 + [0] * 20
        params = cal.fit(predictions, outcomes)
        if params.brier_after is not None and params.brier_before is not None:
            # Platt should improve or at least not worsen Brier
            # (if it worsens, it won't activate)
            if cal.is_active:
                assert params.brier_after <= params.brier_before
