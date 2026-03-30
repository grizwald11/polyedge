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
        if not (0.0 <= predicted_probability <= 1.0):
            logger.warning(
                f"Invalid probability {predicted_probability} for {market_id} — "
                f"clamping to [0, 1]"
            )
            predicted_probability = max(0.0, min(1.0, predicted_probability))
        if not (0.0 <= market_price <= 1.0):
            logger.warning(
                f"Invalid market_price {market_price} for {market_id} — "
                f"clamping to [0, 1]"
            )
            market_price = max(0.0, min(1.0, market_price))

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
        use_time_decay: bool = True,
    ) -> Optional[float]:
        """Calculate Brier score for resolved predictions.

        Brier score = weighted_mean((predicted - actual)^2)
        Perfect = 0.0, random = 0.25, always wrong = 1.0

        M-2: When use_time_decay=True, recent predictions are weighted more
        heavily using exponential decay (half-life = 30 days). This ensures
        the Brier score reflects current forecasting accuracy rather than
        being diluted by stale predictions from months ago.

        Args:
            strategy: Filter by strategy (None = all)
            days: Only include predictions from last N days (None = all)
            use_time_decay: Weight recent predictions more heavily (default True)

        Returns:
            Brier score or None if no resolved predictions
        """
        import math

        records = self._get_resolved_records(strategy, days)
        if not records:
            return None

        now = datetime.now(timezone.utc)
        HALF_LIFE_DAYS = 30.0  # M-2: half-life for exponential decay

        weighted_total = 0.0
        weight_sum = 0.0
        valid_count = 0
        skipped_null = 0
        skipped_bad = 0
        bucket_scores: dict[str, list[float]] = {}

        for r in records:
            if r["actual_outcome"] is None:
                skipped_null += 1
                continue
            try:
                outcome = float(r["actual_outcome"])
                predicted = float(r["predicted_probability"])
            except (TypeError, ValueError) as e:
                logger.warning(
                    f"Skipping calibration record with bad data: "
                    f"market={r.get('market_id')}, outcome={r.get('actual_outcome')!r}, "
                    f"predicted={r.get('predicted_probability')!r}: {e}"
                )
                skipped_bad += 1
                continue

            # M-4: Bounds validation — skip records with out-of-range values
            if not (0.0 <= predicted <= 1.0) or not (0.0 <= outcome <= 1.0):
                logger.warning(
                    f"Skipping calibration record with out-of-bounds values: "
                    f"market={r.get('market_id')}, predicted={predicted}, "
                    f"outcome={outcome} (must be in [0.0, 1.0])"
                )
                skipped_bad += 1
                continue

            brier = (predicted - outcome) ** 2
            self._bucket_brier_score(r, brier, bucket_scores)

            # M-2: Compute time-decay weight
            if use_time_decay and r.get("predicted_at"):
                try:
                    pred_time = datetime.fromisoformat(r["predicted_at"])
                    age_days = (now - pred_time).total_seconds() / 86400.0
                    weight = math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)
                except (ValueError, TypeError):
                    weight = 1.0
            else:
                weight = 1.0

            weighted_total += brier * weight
            weight_sum += weight
            valid_count += 1

        if skipped_null or skipped_bad:
            logger.info(
                f"Brier score: {valid_count} valid records, "
                f"{skipped_null} skipped (cancelled/NULL outcome), "
                f"{skipped_bad} skipped (bad data)"
            )

        self._log_bucket_scores(bucket_scores)

        if valid_count == 0:
            return None

        return weighted_total / weight_sum

    @staticmethod
    def _bucket_brier_score(
        record: dict, brier: float, bucket_scores: dict[str, list[float]],
    ) -> None:
        """Assign a Brier score to a time-staleness bucket for diagnostics."""
        if not record.get("predicted_at") or not record.get("resolved_at"):
            return
        try:
            pred_time = datetime.fromisoformat(record["predicted_at"])
            res_time = datetime.fromisoformat(record["resolved_at"])
            staleness_days = (res_time - pred_time).total_seconds() / 86400
        except (ValueError, TypeError):
            return
        if staleness_days <= 30:
            time_bucket = "0-30d"
        elif staleness_days <= 90:
            time_bucket = "30-90d"
        else:
            time_bucket = "90d+"
        bucket_scores.setdefault(time_bucket, []).append(brier)

    @staticmethod
    def _log_bucket_scores(bucket_scores: dict[str, list[float]]) -> None:
        """Log per-bucket Brier averages for diagnostics."""
        for bucket, scores in sorted(bucket_scores.items()):
            avg = sum(scores) / len(scores) if scores else 0.0
            logger.info(
                f"Brier by time bucket [{bucket}]: "
                f"avg={avg:.4f}, count={len(scores)}"
            )

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
                valid_outcomes = [float(r["actual_outcome"]) for r in in_bin
                                 if r["actual_outcome"] is not None]
                actual_avg = sum(valid_outcomes) / len(valid_outcomes) if valid_outcomes else None
            else:
                pred_avg = (lo + hi) / 2
                actual_avg = None  # No data — don't bias calibration plot

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
            JOIN markets m ON cr.market_id = m.ticker AND cr.platform = m.platform
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
            if not records:
                continue
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
        """Calculate win rate: fraction of predictions where we had edge AND were right.

        A prediction "wins" if our predicted probability was on the correct side
        of the market price (i.e., we had a directionally correct edge).
        Falls back to 0.5 threshold when market_price_at_prediction is unavailable.
        """
        records = self._get_resolved_records(strategy)
        if not records:
            return None

        wins = 0
        total = 0
        for r in records:
            if r["actual_outcome"] is None:
                continue
            predicted = r["predicted_probability"]
            actual = int(r["actual_outcome"]) == 1
            market_price = r.get("market_price_at_prediction")
            # Use market price as threshold when available — this measures
            # whether we added value beyond what the market already knew.
            threshold = float(market_price) if market_price is not None else 0.5
            total += 1
            if (predicted > threshold and actual) or (predicted < threshold and not actual):
                wins += 1

        if total == 0:
            return None
        return wins / total

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
