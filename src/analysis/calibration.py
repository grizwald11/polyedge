"""Calibration tracker — logs predictions and measures accuracy.

Tracks predicted probabilities vs actual outcomes, computes Brier scores,
and generates calibration data for analysis.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import CalibrationRecord, StrategyName
from src.storage.database import Database

logger = logging.getLogger(__name__)


class CalibrationTracker:
    """Tracks prediction accuracy and calibration metrics."""

    def __init__(self, db: Database):
        self.db = db

    def log_prediction(
        self,
        market_id: str,
        market_question: str,
        predicted_probability: float,
        market_price: float,
        strategy: StrategyName = StrategyName.AI_PROBABILITY,
    ) -> int:
        """Log a new prediction for future calibration.

        Args:
            market_id: Market ticker
            market_question: Human-readable question
            predicted_probability: Our estimated probability (0-1)
            market_price: Market price at time of prediction
            strategy: Which strategy made the prediction

        Returns:
            Database row ID
        """
        record = CalibrationRecord(
            market_id=market_id,
            market_question=market_question,
            strategy=strategy,
            predicted_probability=predicted_probability,
            market_price_at_prediction=market_price,
            predicted_at=datetime.now(timezone.utc),
        )
        row_id = self.db.log_calibration(record)
        logger.debug(
            f"Logged prediction: {market_id} p={predicted_probability:.2f} "
            f"(market={market_price:.2f})"
        )
        return row_id

    def resolve_prediction(
        self,
        market_id: str,
        actual_outcome: bool,
    ) -> int:
        """Resolve all unresolved predictions for a market.

        Args:
            market_id: Market that resolved
            actual_outcome: True if YES, False if NO

        Returns:
            Number of predictions resolved
        """
        conn = self.db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        outcome_int = 1 if actual_outcome else 0
        cursor = conn.execute(
            """UPDATE calibration_records
               SET actual_outcome = ?, resolved_at = ?
               WHERE market_id = ? AND actual_outcome IS NULL""",
            (outcome_int, now, market_id),
        )
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info(
                f"Resolved {count} predictions for {market_id}: "
                f"{'YES' if actual_outcome else 'NO'}"
            )
        return count

    def calculate_brier_score(
        self,
        strategy: Optional[StrategyName] = None,
        days: Optional[int] = None,
    ) -> Optional[float]:
        """Calculate Brier score for resolved predictions.

        Brier score = mean((predicted - actual)^2)
        Perfect = 0.0, random = 0.25, always wrong = 1.0

        Args:
            strategy: Filter by strategy (None = all)
            days: Only include predictions from last N days (None = all)

        Returns:
            Brier score or None if no resolved predictions
        """
        records = self._get_resolved_records(strategy, days)
        if not records:
            return None

        total = 0.0
        for r in records:
            outcome = float(r["actual_outcome"])
            predicted = r["predicted_probability"]
            total += (predicted - outcome) ** 2

        return total / len(records)

    def get_calibration_bins(
        self,
        strategy: Optional[StrategyName] = None,
        n_bins: int = 10,
    ) -> list[dict]:
        """Get calibration data binned by predicted probability.

        Returns bins like:
        [{"bin": "0.0-0.1", "predicted_avg": 0.05, "actual_avg": 0.03, "count": 12}, ...]
        """
        records = self._get_resolved_records(strategy)
        if not records:
            return []

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
                actual_avg = 0.0

            bins.append({
                "bin": f"{lo:.1f}-{hi:.1f}",
                "predicted_avg": pred_avg,
                "actual_avg": actual_avg,
                "count": len(in_bin),
            })

        return bins

    def get_accuracy_by_category(self) -> dict[str, dict]:
        """Get Brier score and count broken down by market category.

        Returns:
            {"Politics": {"brier": 0.15, "count": 20}, ...}
        """
        conn = self.db._get_conn()
        rows = conn.execute("""
            SELECT cr.strategy, m.category,
                   cr.predicted_probability, cr.actual_outcome
            FROM calibration_records cr
            JOIN markets m ON cr.market_id = m.ticker
            WHERE cr.actual_outcome IS NOT NULL
        """).fetchall()

        categories: dict[str, list[dict]] = {}
        for r in rows:
            cat = r["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(dict(r))

        result = {}
        for cat, records in categories.items():
            brier = sum(
                (r["predicted_probability"] - float(r["actual_outcome"])) ** 2
                for r in records
            ) / len(records)
            result[cat] = {"brier": brier, "count": len(records)}

        return result

    def get_win_rate(
        self,
        strategy: Optional[StrategyName] = None,
    ) -> Optional[float]:
        """Calculate win rate: fraction of predictions where we were on the right side.

        A prediction "wins" if:
        - We predicted >0.5 and outcome was YES
        - We predicted <0.5 and outcome was NO
        """
        records = self._get_resolved_records(strategy)
        if not records:
            return None

        wins = 0
        for r in records:
            predicted = r["predicted_probability"]
            actual = bool(r["actual_outcome"])
            if (predicted > 0.5 and actual) or (predicted < 0.5 and not actual):
                wins += 1

        return wins / len(records)

    def get_summary(self) -> dict:
        """Get a full calibration summary."""
        brier = self.calculate_brier_score()
        win_rate = self.get_win_rate()
        records = self._get_resolved_records()
        unresolved = self.db.get_unresolved_predictions()

        return {
            "brier_score": brier,
            "win_rate": win_rate,
            "resolved_count": len(records),
            "unresolved_count": len(unresolved),
            "total_predictions": len(records) + len(unresolved),
        }

    def _get_resolved_records(
        self,
        strategy: Optional[StrategyName] = None,
        days: Optional[int] = None,
    ) -> list[dict]:
        """Get resolved calibration records with optional filters."""
        conn = self.db._get_conn()
        query = "SELECT * FROM calibration_records WHERE actual_outcome IS NOT NULL"
        params: list = []

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy.value)

        if days:
            query += " AND predicted_at >= datetime('now', ?)"
            params.append(f"-{days} days")

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
