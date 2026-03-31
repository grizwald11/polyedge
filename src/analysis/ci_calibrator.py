"""Confidence Interval Calibrator — tracks and corrects Claude's CI accuracy.

Claude's stated confidence intervals (e.g., [40%, 70%]) may not match
actual accuracy. This module tracks what fraction of outcomes fall within
the stated CIs and applies stretch factors to correct them.

Target: 90% coverage (since CIs are meant as ~90% intervals).
If coverage is too low → CIs are too narrow → widen them.
If coverage is too high → CIs are too wide → narrow them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class CICalibrator:
    """Tracks and corrects Claude's confidence interval accuracy."""

    # Target coverage for CIs (90% of outcomes should fall within stated CI)
    TARGET_COVERAGE = 0.90
    # Minimum resolved records needed for meaningful adjustment
    MIN_RECORDS = 20

    def __init__(self, db):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create ci_records table if it doesn't exist."""
        try:
            conn = self.db._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ci_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    ci_low REAL NOT NULL,
                    ci_high REAL NOT NULL,
                    predicted_prob REAL NOT NULL,
                    category TEXT DEFAULT '',
                    actual_outcome INTEGER,
                    predicted_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ci_records_market
                ON ci_records(market_id)
            """)
            conn.commit()
        except Exception as e:
            logger.debug(f"CI table setup: {e}")

    def record_ci(
        self,
        market_id: str,
        ci_low: float,
        ci_high: float,
        predicted_prob: float,
        category: str = "",
    ) -> None:
        """Store CI bounds at prediction time for later evaluation."""
        try:
            conn = self.db._get_conn()
            conn.execute(
                """INSERT INTO ci_records
                   (market_id, ci_low, ci_high, predicted_prob, category, predicted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    market_id, ci_low, ci_high, predicted_prob, category,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"Failed to record CI: {e}")

    def resolve_ci(self, market_id: str, actual_outcome: bool) -> None:
        """Update CI records with the actual outcome."""
        try:
            conn = self.db._get_conn()
            outcome_int = 1 if actual_outcome else 0
            conn.execute(
                """UPDATE ci_records
                   SET actual_outcome = ?, resolved_at = ?
                   WHERE market_id = ? AND actual_outcome IS NULL""",
                (outcome_int, datetime.now(timezone.utc).isoformat(), market_id),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"Failed to resolve CI: {e}")

    def compute_ci_accuracy(self, category: str = "") -> dict:
        """Compute CI coverage and accuracy statistics.

        Returns:
            Dict with: coverage (fraction within CI), avg_width, count
        """
        records = self._get_resolved_records(category)
        if not records:
            return {"coverage": None, "avg_width": None, "count": 0}

        in_ci = 0
        total_width = 0.0

        for r in records:
            outcome = float(r["actual_outcome"])
            ci_low = r["ci_low"]
            ci_high = r["ci_high"]
            total_width += ci_high - ci_low

            # Check if outcome falls within CI
            if ci_low <= outcome <= ci_high:
                in_ci += 1

        n = len(records)
        return {
            "coverage": in_ci / n,
            "avg_width": total_width / n,
            "count": n,
        }

    def get_ci_stretch_factor(self, category: str = "") -> float:
        """Compute CI stretch factor to correct width.

        Returns:
            < 1.0: CIs too wide (narrow them)
            = 1.0: CIs well-calibrated (no change)
            > 1.0: CIs too narrow (widen them)
        """
        stats = self.compute_ci_accuracy(category)
        if stats["count"] < self.MIN_RECORDS:
            return 1.0

        coverage = stats["coverage"]

        # Coverage too low → CIs are too narrow → need to widen (factor > 1.0)
        if coverage < 0.70:
            return 1.4   # Very narrow CIs, widen 40%
        elif coverage < 0.80:
            return 1.2   # Somewhat narrow, widen 20%
        elif coverage < self.TARGET_COVERAGE:
            return 1.1   # Slightly narrow

        # Coverage too high → CIs are too wide → can narrow (factor < 1.0)
        if coverage > 0.98:
            return 0.7   # Very wide CIs, narrow 30%
        elif coverage > 0.95:
            return 0.85  # Somewhat wide

        return 1.0  # Good coverage, no adjustment

    def adjust_ci(
        self,
        ci_low: float,
        ci_high: float,
        probability: float,
        category: str = "",
    ) -> tuple[float, float]:
        """Apply stretch factor to correct CI width.

        Stretches/narrows the CI symmetrically around the probability estimate.

        Returns:
            Adjusted (ci_low, ci_high) tuple
        """
        factor = self.get_ci_stretch_factor(category)
        if factor == 1.0:
            return ci_low, ci_high

        half_width = (ci_high - ci_low) / 2.0
        new_half = half_width * factor

        new_low = max(0.01, probability - new_half)
        new_high = min(0.99, probability + new_half)

        return new_low, new_high

    def _get_resolved_records(self, category: str = "") -> list[dict]:
        """Get resolved CI records, optionally filtered by category."""
        try:
            conn = self.db._get_conn()
            if category:
                rows = conn.execute(
                    "SELECT * FROM ci_records WHERE actual_outcome IS NOT NULL AND category = ?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ci_records WHERE actual_outcome IS NOT NULL",
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"Failed to get CI records: {e}")
            return []
