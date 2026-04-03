"""Edge Decay Tracker — tracks predicted vs realized edge.

Monitors whether detected edges actually result in profits. If edges
are systematically overestimated (common due to vig, adverse selection,
and information asymmetry), applies a shrinkage multiplier to Kelly
sizing to prevent overallocation.

Example: If predicted edges average 10% but realized edges average 5%,
the edge multiplier is 0.5 — Kelly will use half the predicted edge
for sizing, producing correctly-sized positions.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class EdgeTracker:
    """Tracks predicted vs realized edge to detect systematic overestimation."""

    MIN_RECORDS = 20  # Minimum resolved records for statistical significance

    def __init__(self, db):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create edge_records table if it doesn't exist."""
        try:
            conn = self.db._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS edge_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    predicted_edge REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    direction TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    exit_price REAL,
                    actual_outcome INTEGER,
                    realized_edge REAL,
                    predicted_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edge_records_market
                ON edge_records(market_id)
            """)
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Edge table setup: {e}")

    def record_predicted_edge(
        self,
        market_id: str,
        predicted_edge: float,
        entry_price: float,
        direction: str,
        category: str = "",
    ) -> None:
        """Record a predicted edge when a signal is acted upon.

        Args:
            market_id: Market ticker
            predicted_edge: The edge predicted by the strategy (always positive)
            entry_price: The price at which we entered
            direction: "BUY_YES" or "BUY_NO"
            category: Market category for per-category tracking
        """
        try:
            conn = self.db._get_conn()
            conn.execute(
                """INSERT INTO edge_records
                   (market_id, predicted_edge, entry_price, direction, category, predicted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    market_id, abs(predicted_edge), entry_price, direction,
                    category, datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Failed to record edge: {e}")

    def record_resolution(
        self,
        market_id: str,
        actual_outcome: bool,
        exit_price: Optional[float] = None,
    ) -> None:
        """Record the resolution of a market to compute realized edge.

        Args:
            market_id: Market ticker
            actual_outcome: True if YES, False if NO
            exit_price: Price at exit (if position was closed before resolution)
        """
        try:
            conn = self.db._get_conn()
            records = conn.execute(
                "SELECT * FROM edge_records WHERE market_id = ? AND actual_outcome IS NULL",
                (market_id,),
            ).fetchall()

            for r in records:
                r = dict(r)
                direction = r["direction"]
                entry = r["entry_price"]

                # Compute realized edge based on outcome
                if exit_price is not None:
                    actual_price = exit_price
                else:
                    actual_price = 1.0 if actual_outcome else 0.0

                if direction == "BUY_YES":
                    realized = actual_price - entry
                else:  # BUY_NO
                    realized = (1.0 - actual_price) - (1.0 - entry)

                conn.execute(
                    """UPDATE edge_records
                       SET actual_outcome = ?, exit_price = ?,
                           realized_edge = ?, resolved_at = ?
                       WHERE id = ?""",
                    (
                        1 if actual_outcome else 0,
                        exit_price or actual_price,
                        realized,
                        datetime.now(timezone.utc).isoformat(),
                        r["id"],
                    ),
                )
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Failed to record resolution: {e}")

    def compute_edge_shrinkage(self, category: str = "") -> float:
        """Compute ratio of realized edge / predicted edge.

        Returns:
            Shrinkage ratio clamped to [0.5, 1.5].
            1.0 = edges materialize as predicted.
            0.5 = edges are 50% overestimated.
            1.5 = edges are 50% underestimated (rare).
        """
        records = self._get_resolved_records(category)
        if len(records) < self.MIN_RECORDS:
            return 1.0

        predicted = [abs(r["predicted_edge"]) for r in records]
        realized = [r["realized_edge"] for r in records]

        avg_predicted = sum(predicted) / len(predicted)
        avg_realized = sum(realized) / len(realized)

        if avg_predicted <= 0:
            return 1.0

        ratio = avg_realized / avg_predicted
        return max(0.5, min(1.5, ratio))

    def get_edge_multiplier(self, category: str = "") -> float:
        """Get the edge discount multiplier for position sizing.

        This is the main interface for Kelly sizer integration.
        """
        return self.compute_edge_shrinkage(category)

    def get_summary(self, category: str = "") -> dict:
        """Get summary statistics for edge tracking.

        Returns:
            Dict with avg_predicted, avg_realized, shrinkage, count, win_rate
        """
        records = self._get_resolved_records(category)
        if not records:
            return {
                "avg_predicted": None,
                "avg_realized": None,
                "shrinkage": 1.0,
                "count": 0,
                "win_rate": None,
            }

        predicted = [abs(r["predicted_edge"]) for r in records]
        realized = [r["realized_edge"] for r in records]
        wins = sum(1 for r in realized if r > 0)

        return {
            "avg_predicted": sum(predicted) / len(predicted),
            "avg_realized": sum(realized) / len(realized),
            "shrinkage": self.compute_edge_shrinkage(category),
            "count": len(records),
            "win_rate": wins / len(records),
        }

    def _get_resolved_records(self, category: str = "") -> list[dict]:
        """Get resolved edge records."""
        try:
            conn = self.db._get_conn()
            if category:
                rows = conn.execute(
                    "SELECT * FROM edge_records WHERE actual_outcome IS NOT NULL AND category = ?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM edge_records WHERE actual_outcome IS NOT NULL",
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.debug(f"Failed to get edge records: {e}")
            return []
