"""Calibration analyzer — generates calibration reports and category adjustments.

Reads all resolved predictions and computes overall and per-category Brier scores,
calibration curves, systematic biases, and correction factors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class CalibrationBin:
    """One bin in a calibration curve."""
    bin_label: str
    predicted_avg: float
    actual_avg: Optional[float]
    count: int


@dataclass
class CategoryStats:
    """Calibration stats for a single category."""
    category: str
    brier_score: float
    count: int
    avg_predicted: float
    avg_actual: float
    bias: float  # positive = overestimates, negative = underestimates


@dataclass
class CalibrationReport:
    """Full calibration report with all stats."""
    overall_brier: Optional[float]
    overall_win_rate: Optional[float]
    total_resolved: int
    total_unresolved: int
    calibration_curve: list[CalibrationBin] = field(default_factory=list)
    category_stats: list[CategoryStats] = field(default_factory=list)
    best_category: Optional[str] = None
    worst_category: Optional[str] = None


class CalibrationAnalyzer:
    """Analyzes resolved predictions to compute calibration metrics and adjustments."""

    def __init__(self, db: Database):
        self.db = db

    def generate_report(self) -> CalibrationReport:
        """Generate a full calibration report from all resolved predictions.

        Returns:
            CalibrationReport with overall stats, calibration curve, and category breakdowns.
        """
        resolved = self._get_resolved_with_category()
        unresolved_count = len(self.db.get_unresolved_predictions())

        if not resolved:
            return CalibrationReport(
                overall_brier=None,
                overall_win_rate=None,
                total_resolved=0,
                total_unresolved=unresolved_count,
            )

        overall_brier = self._compute_brier(resolved)
        overall_win_rate = self._compute_win_rate(resolved)
        calibration_curve = self._compute_calibration_curve(resolved)
        category_stats = self._compute_category_stats(resolved)

        # Only consider categories with enough data for best/worst ranking
        valid_stats = [c for c in category_stats if c.count >= 5]
        best = min(valid_stats, key=lambda c: c.brier_score) if valid_stats else None
        worst = max(valid_stats, key=lambda c: c.brier_score) if valid_stats else None

        return CalibrationReport(
            overall_brier=overall_brier,
            overall_win_rate=overall_win_rate,
            total_resolved=len(resolved),
            total_unresolved=unresolved_count,
            calibration_curve=calibration_curve,
            category_stats=category_stats,
            best_category=best.category if best else None,
            worst_category=worst.category if worst else None,
        )

    def get_category_adjustments(self) -> dict[str, float]:
        """Get per-category adjustment factors to correct Claude's estimates.

        Uses James-Stein shrinkage to blend category-specific bias toward the
        global bias, weighted by sample size. This allows categories with as
        few as 3 samples to receive partial corrections (heavily shrunk toward
        global), while categories with 30+ samples are mostly self-determined.

        Returns a dict of {category: adjustment_factor} where:
        - Positive adjustment means Claude underestimates (add to prediction)
        - Negative adjustment means Claude overestimates (subtract from prediction)

        The ensemble can apply: adjusted_prob = claude_prob + adjustment
        """
        import math

        resolved = self._get_resolved_with_category()
        if not resolved:
            return {}

        # Compute global bias as the shrinkage prior
        global_avg_predicted = sum(r["predicted_probability"] for r in resolved) / len(resolved)
        global_avg_actual = sum(float(r["actual_outcome"]) for r in resolved) / len(resolved)
        global_bias = global_avg_actual - global_avg_predicted

        # Group by category
        by_category: dict[str, list[dict]] = {}
        for r in resolved:
            cat = r.get("category", "Other")
            by_category.setdefault(cat, []).append(r)

        adjustments = {}
        for cat, records in by_category.items():
            n = len(records)
            if n < 3:  # Absolute minimum for any signal
                continue

            # Category-specific bias
            avg_predicted = sum(r["predicted_probability"] for r in records) / n
            avg_actual = sum(float(r["actual_outcome"]) for r in records) / n
            cat_bias = avg_actual - avg_predicted

            # James-Stein shrinkage: blend category bias toward global bias.
            # shrinkage_weight = n / (n + k), where k controls shrinkage strength.
            # k=15 means at 15 samples we're 50/50 category vs global;
            # at 3 samples we're 17% category / 83% global;
            # at 50 samples we're 77% category / 23% global.
            k = 15.0
            shrinkage_weight = n / (n + k)
            blended_bias = shrinkage_weight * cat_bias + (1 - shrinkage_weight) * global_bias

            # Still require meaningful bias magnitude
            threshold = max(0.03, 1.96 * math.sqrt(0.25 / n))
            # For small samples, use a softer threshold since shrinkage
            # already dampens the estimate toward the global prior
            if n < 15:
                threshold = max(0.03, threshold * 0.6)

            if abs(blended_bias) > threshold:
                adjustments[cat] = round(blended_bias, 3)

        return adjustments

    def get_category_base_rates(self) -> dict[str, dict]:
        """Get historical YES resolution rates by category.

        Uses shrinkage toward the global base rate for small-sample categories.
        Returns dict like {"Politics": {"total": 25, "yes_rate": 0.44}}.
        Includes categories with as few as 3 resolved predictions (shrunk
        toward global rate).
        """
        resolved = self._get_resolved_with_category()
        if not resolved:
            return {}

        # Global base rate as shrinkage prior
        global_yes = sum(1 for r in resolved if bool(r["actual_outcome"]))
        global_rate = global_yes / len(resolved)

        by_category: dict[str, list[dict]] = {}
        for r in resolved:
            cat = r.get("category", "Other")
            by_category.setdefault(cat, []).append(r)

        base_rates = {}
        for cat, records in by_category.items():
            n = len(records)
            if n < 3:
                continue
            yes_count = sum(1 for r in records if bool(r["actual_outcome"]))
            cat_rate = yes_count / n

            # Shrinkage: blend toward global rate for small samples
            k = 15.0
            shrinkage_weight = n / (n + k)
            blended_rate = shrinkage_weight * cat_rate + (1 - shrinkage_weight) * global_rate

            base_rates[cat] = {
                "total": n,
                "yes_rate": round(blended_rate, 3),
            }

        return base_rates

    def _get_resolved_with_category(self) -> list[dict]:
        """Get all resolved predictions joined with market category."""
        conn = self.db._get_conn()
        rows = conn.execute("""
            SELECT cr.*, COALESCE(m.category, 'Other') as category
            FROM calibration_records cr
            LEFT JOIN markets m ON cr.market_id = m.ticker
            WHERE cr.actual_outcome IS NOT NULL
            ORDER BY cr.resolved_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def _compute_brier(self, records: list[dict]) -> float:
        """Compute overall Brier score."""
        total = sum(
            (r["predicted_probability"] - float(r["actual_outcome"])) ** 2
            for r in records
        )
        return total / len(records)

    def _compute_win_rate(self, records: list[dict]) -> float:
        """Compute overall win rate."""
        wins = 0
        total = 0
        for r in records:
            predicted = r["predicted_probability"]
            actual = bool(r["actual_outcome"])
            # Use market price as threshold when available — this measures
            # whether we added value beyond what the market already knew.
            market_price = r.get("market_price_at_prediction")
            threshold = float(market_price) if market_price is not None else 0.5
            total += 1
            if (predicted > threshold and actual) or (predicted < threshold and not actual):
                wins += 1
        return wins / total if total > 0 else 0.0

    def _compute_calibration_curve(
        self, records: list[dict], n_bins: int = 10
    ) -> list[CalibrationBin]:
        """Compute calibration curve as binned predicted vs actual."""
        bin_width = 1.0 / n_bins
        bins = []

        for i in range(n_bins):
            lo = i * bin_width
            hi = (i + 1) * bin_width

            in_bin = [
                r for r in records
                if lo <= r["predicted_probability"] < hi
                or (i == n_bins - 1 and r["predicted_probability"] == hi)
            ]

            if in_bin:
                pred_avg = sum(r["predicted_probability"] for r in in_bin) / len(in_bin)
                actual_avg = sum(float(r["actual_outcome"]) for r in in_bin) / len(in_bin)
            else:
                pred_avg = (lo + hi) / 2
                actual_avg = None

            bins.append(CalibrationBin(
                bin_label=f"{lo:.0%}-{hi:.0%}",
                predicted_avg=pred_avg,
                actual_avg=actual_avg,
                count=len(in_bin),
            ))

        return bins

    def _compute_category_stats(self, records: list[dict]) -> list[CategoryStats]:
        """Compute per-category Brier scores and biases."""
        by_category: dict[str, list[dict]] = {}
        for r in records:
            cat = r.get("category", "Other")
            by_category.setdefault(cat, []).append(r)

        stats = []
        for cat, recs in sorted(by_category.items()):
            avg_predicted = sum(r["predicted_probability"] for r in recs) / len(recs)
            avg_actual = sum(float(r["actual_outcome"]) for r in recs) / len(recs)
            brier = sum(
                (r["predicted_probability"] - float(r["actual_outcome"])) ** 2
                for r in recs
            ) / len(recs)
            bias = avg_actual - avg_predicted  # positive = underestimates

            stats.append(CategoryStats(
                category=cat,
                brier_score=brier,
                count=len(recs),
                avg_predicted=avg_predicted,
                avg_actual=avg_actual,
                bias=bias,
            ))

        return stats
