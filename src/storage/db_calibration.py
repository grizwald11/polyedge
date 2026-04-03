"""Calibration and prediction tracking database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import CalibrationRecord

logger = logging.getLogger(__name__)


class CalibrationMixin:
    """Database mixin for calibration and prediction CRUD operations."""

    # ──────────────────────────────────────
    # Calibration Operations
    # ──────────────────────────────────────

    def log_calibration(self, record: CalibrationRecord) -> int:
        """Log a calibration prediction."""
        conn = self._get_conn()
        cursor = conn.execute("""
            INSERT INTO calibration_records (
                market_id, market_question, strategy,
                predicted_probability, market_price_at_prediction,
                actual_outcome, predicted_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.market_id,
            record.market_question,
            record.strategy.value,
            record.predicted_probability,
            record.market_price_at_prediction,
            record.actual_outcome,
            record.predicted_at.isoformat(),
            record.resolved_at.isoformat() if record.resolved_at else None,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_unresolved_predictions(self) -> list[dict]:
        """Get predictions that haven't been resolved yet."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM calibration_records WHERE actual_outcome IS NULL"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_calibration_records(self) -> list[dict]:
        """Get all calibration records for analysis."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM calibration_records ORDER BY predicted_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_resolved_calibration_records(self) -> list[dict]:
        """Get resolved calibration records for Platt scaling.

        Returns only records where actual_outcome is known (0 or 1),
        with predicted_probability and actual_outcome fields.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT predicted_probability, "
            "CASE WHEN actual_outcome = 1 THEN 'Yes' ELSE 'No' END AS actual_outcome "
            "FROM calibration_records "
            "WHERE actual_outcome IS NOT NULL "
            "ORDER BY predicted_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def store_prediction(
        self,
        market_ticker: str,
        predicted_probability: float,
        predicted_side: str,
        market_price: float,
        strategy: str = "ai_probability",
        confidence_low: float = 0.0,
        confidence_high: float = 1.0,
        market_question: str = "",
        prompt_variant: str = "",
    ) -> int:
        """Store a prediction when Claude makes an assessment.

        Args:
            market_ticker: Market ticker.
            predicted_probability: Our estimated probability (0-1).
            predicted_side: "YES" or "NO" -- the side we'd bet.
            market_price: Market price at time of prediction.
            strategy: Strategy name.
            confidence_low: Lower bound of confidence interval.
            confidence_high: Upper bound of confidence interval.
            market_question: Human-readable question.
            prompt_variant: A/B test variant name used for this prediction.

        Returns:
            Database row ID.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        cursor = conn.execute("""
            INSERT INTO calibration_records (
                market_id, market_question, strategy,
                predicted_probability, market_price_at_prediction,
                predicted_at, prompt_variant
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            market_ticker,
            market_question,
            strategy,
            predicted_probability,
            market_price,
            now,
            prompt_variant,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_latest_prediction(self, market_ticker: str) -> Optional[dict]:
        """Get the most recent prediction for a market.

        Returns dict with predicted_probability, market_price_at_prediction,
        predicted_at, or None if no prediction exists.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT predicted_probability, market_price_at_prediction, predicted_at "
            "FROM calibration_records WHERE market_id=? "
            "ORDER BY predicted_at DESC LIMIT 1",
            (market_ticker,),
        ).fetchone()
        return dict(row) if row else None

    def get_resolved_predictions(
        self,
        strategy: Optional[str] = None,
        days: Optional[int] = None,
    ) -> list[dict]:
        """Get all resolved predictions for analysis.

        Args:
            strategy: Filter by strategy name (None = all).
            days: Only include predictions from last N days.

        Returns:
            List of resolved calibration records.
        """
        conn = self._get_conn()
        query = "SELECT * FROM calibration_records WHERE actual_outcome IS NOT NULL"
        params: list = []

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if days:
            query += " AND predicted_at >= datetime('now', ?)"
            params.append(f"-{days} days")

        query += " ORDER BY resolved_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_resolution(
        self,
        market_id: str,
        actual_outcome: int,
        brier_score: Optional[float] = None,
        profit_loss: Optional[float] = None,
    ) -> int:
        """Update calibration records with resolution data.

        Args:
            market_id: Market ticker.
            actual_outcome: 1 for YES, 0 for NO.
            brier_score: (predicted - actual)^2.
            profit_loss: Realized P&L for this prediction.

        Returns:
            Number of records updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE calibration_records
               SET actual_outcome = ?, resolved_at = ?,
                   brier_score = ?, profit_loss = ?
               WHERE market_id = ? AND actual_outcome IS NULL""",
            (actual_outcome, now, brier_score, profit_loss, market_id),
        )
        conn.commit()
        return cursor.rowcount
