"""Platt scaling post-hoc calibration.

Fits a logistic regression on (predicted_probability, actual_outcome) pairs
to map raw forecasts to calibrated probabilities. Research shows this brings
AI forecasters to near-superforecaster accuracy.

Re-fits weekly as more data accumulates. Requires minimum 50 resolved
predictions before activating.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MIN_SAMPLES = 50  # Minimum resolved predictions before Platt scaling activates


@dataclass
class PlattParams:
    """Fitted Platt scaling parameters: calibrated = sigmoid(a * raw + b)."""

    a: float = 1.0  # Slope (1.0 = identity)
    b: float = 0.0  # Intercept (0.0 = no shift)
    n_samples: int = 0  # Number of samples used to fit
    brier_before: Optional[float] = None  # Brier score without Platt
    brier_after: Optional[float] = None  # Brier score with Platt


class PlattCalibrator:
    """Post-hoc calibration using Platt scaling (logistic regression).

    Usage:
        calibrator = PlattCalibrator()
        calibrator.fit(predictions, outcomes)  # Call periodically (e.g., weekly)
        calibrated_prob = calibrator.calibrate(raw_prob)
    """

    def __init__(self):
        self._params: PlattParams = PlattParams()
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def params(self) -> PlattParams:
        return self._params

    def fit(self, predictions: list[float], outcomes: list[int]) -> PlattParams:
        """Fit Platt scaling parameters from historical data.

        Args:
            predictions: Raw predicted probabilities (0-1)
            outcomes: Actual outcomes (0 or 1)

        Returns:
            Fitted PlattParams
        """
        if len(predictions) != len(outcomes):
            raise ValueError("predictions and outcomes must have same length")

        if len(predictions) < MIN_SAMPLES:
            logger.info(
                f"Platt scaling: only {len(predictions)} samples, "
                f"need {MIN_SAMPLES} — staying inactive"
            )
            self._active = False
            self._params = PlattParams(n_samples=len(predictions))
            return self._params

        # Compute Brier score before calibration
        brier_before = sum(
            (p - o) ** 2 for p, o in zip(predictions, outcomes)
        ) / len(predictions)

        # Fit logistic regression using gradient descent on log-loss
        # calibrated = sigmoid(a * logit(raw) + b)
        # Convert predictions to log-odds space
        logits = []
        filtered_outcomes = []
        for p, o in zip(predictions, outcomes):
            p_clamped = max(0.001, min(0.999, p))
            logits.append(math.log(p_clamped / (1 - p_clamped)))
            filtered_outcomes.append(o)

        a, b = self._gradient_descent(logits, filtered_outcomes)

        # Compute Brier score after calibration
        calibrated = [self._sigmoid(a * x + b) for x in logits]
        brier_after = sum(
            (c - o) ** 2 for c, o in zip(calibrated, filtered_outcomes)
        ) / len(filtered_outcomes)

        self._params = PlattParams(
            a=a, b=b, n_samples=len(predictions),
            brier_before=brier_before, brier_after=brier_after,
        )

        # Only activate if Platt scaling actually improves calibration
        if brier_after < brier_before:
            self._active = True
            logger.info(
                f"Platt scaling fitted: a={a:.3f}, b={b:.3f}, "
                f"n={len(predictions)}, Brier {brier_before:.4f} → {brier_after:.4f} "
                f"(improvement: {brier_before - brier_after:.4f})"
            )
        else:
            self._active = False
            logger.info(
                f"Platt scaling fitted but NOT activated: "
                f"Brier {brier_before:.4f} → {brier_after:.4f} (no improvement)"
            )

        return self._params

    def calibrate(self, probability: float) -> float:
        """Apply Platt scaling to a raw probability.

        Returns the raw probability unchanged if Platt scaling is not active.
        """
        if not self._active:
            return probability

        p_clamped = max(0.001, min(0.999, probability))
        logit = math.log(p_clamped / (1 - p_clamped))
        calibrated = self._sigmoid(self._params.a * logit + self._params.b)
        return max(0.01, min(0.99, calibrated))

    @staticmethod
    def _sigmoid(x: float) -> float:
        if x > 500:
            return 1.0
        if x < -500:
            return 0.0
        return 1.0 / (1.0 + math.exp(-x))

    @staticmethod
    def _gradient_descent(
        logits: list[float],
        outcomes: list[int],
        lr: float = 0.01,
        epochs: int = 1000,
    ) -> tuple[float, float]:
        """Simple gradient descent for logistic regression in log-odds space."""
        a, b = 1.0, 0.0
        n = len(logits)

        for _ in range(epochs):
            grad_a, grad_b = 0.0, 0.0
            for x, y in zip(logits, outcomes):
                pred = 1.0 / (1.0 + math.exp(-max(-500, min(500, a * x + b))))
                error = pred - y
                grad_a += error * x
                grad_b += error
            a -= lr * grad_a / n
            b -= lr * grad_b / n

        return a, b
