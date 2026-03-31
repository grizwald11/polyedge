"""Contrarian accuracy tracker — learns when to trust Claude over the market.

Tracks whether Claude-vs-market divergences resolve in Claude's favor,
enabling data-driven divergence thresholds instead of hardcoded guesses.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from src.storage.database import Database

logger = logging.getLogger(__name__)

# Default thresholds (used when insufficient data for learning)
DEFAULT_THRESHOLDS = {
    "Politics": 0.30,
    "Elections": 0.30,
    "Fed": 0.30,
    "Economics": 0.30,
    "Financials": 0.30,
    "World": 0.45,
    "Geopolitics": 0.45,
    "Entertainment": 0.50,
    "Culture": 0.50,
}
DEFAULT_THRESHOLD = 0.40

# Minimum resolved records per category to override defaults
MIN_RECORDS_FOR_DYNAMIC = 20

# Shrinkage constant for blending learned thresholds toward defaults
SHRINKAGE_K = 20.0


class ContrarianTracker:
    """Tracks Claude-vs-market divergences and learns optimal thresholds."""

    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Ensure the divergence_records table exists."""
        conn = self.db._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS divergence_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                category TEXT DEFAULT '',
                claude_estimate REAL NOT NULL,
                market_price REAL NOT NULL,
                divergence REAL NOT NULL,
                abs_divergence REAL NOT NULL,
                predicted_at TEXT NOT NULL,
                actual_outcome INTEGER,
                claude_was_right INTEGER,
                resolved_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_divergence_market ON divergence_records(market_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_divergence_category ON divergence_records(category)"
        )
        conn.commit()

    def record_divergence(
        self,
        market_id: str,
        category: str,
        claude_estimate: float,
        market_price: float,
    ) -> None:
        """Record a Claude-vs-market divergence for later accuracy tracking."""
        divergence = claude_estimate - market_price
        abs_divergence = abs(divergence)
        now = datetime.now(timezone.utc).isoformat()

        conn = self.db._get_conn()
        conn.execute(
            """INSERT INTO divergence_records
               (market_id, category, claude_estimate, market_price,
                divergence, abs_divergence, predicted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (market_id, category, claude_estimate, market_price,
             divergence, abs_divergence, now),
        )
        conn.commit()

    def resolve_divergence(
        self,
        market_id: str,
        actual_outcome: bool,
    ) -> int:
        """After market resolves, determine who was right (Claude or market).

        Claude is 'right' if abs(claude - actual) < abs(market - actual).

        Returns the number of records updated.
        """
        actual_float = 1.0 if actual_outcome else 0.0
        now = datetime.now(timezone.utc).isoformat()

        conn = self.db._get_conn()
        unresolved = conn.execute(
            """SELECT id, claude_estimate, market_price FROM divergence_records
               WHERE market_id = ? AND actual_outcome IS NULL""",
            (market_id,),
        ).fetchall()

        updated = 0
        for row in unresolved:
            claude_err = abs(row["claude_estimate"] - actual_float)
            market_err = abs(row["market_price"] - actual_float)
            claude_was_right = 1 if claude_err < market_err else 0

            conn.execute(
                """UPDATE divergence_records
                   SET actual_outcome = ?, claude_was_right = ?, resolved_at = ?
                   WHERE id = ?""",
                (int(actual_outcome), claude_was_right, now, row["id"]),
            )
            updated += 1

        if updated:
            conn.commit()
        return updated

    def get_dynamic_thresholds(self) -> dict[str, float]:
        """Compute per-category max divergence thresholds from historical data.

        For each category with sufficient data:
        - Find the divergence level where Claude's accuracy drops below 50%
          (i.e., Claude is no better than the market at that divergence level)
        - Use shrinkage to blend toward default thresholds when sample is small

        Returns dict of {category: threshold}.
        """
        conn = self.db._get_conn()
        rows = conn.execute(
            """SELECT category, abs_divergence, claude_was_right
               FROM divergence_records
               WHERE actual_outcome IS NOT NULL""",
        ).fetchall()

        if not rows:
            return dict(DEFAULT_THRESHOLDS)

        # Group by category
        by_category: dict[str, list[dict]] = {}
        for r in rows:
            cat = r["category"] or "Other"
            by_category.setdefault(cat, []).append(dict(r))

        thresholds = dict(DEFAULT_THRESHOLDS)

        for cat, records in by_category.items():
            n = len(records)
            if n < MIN_RECORDS_FOR_DYNAMIC:
                continue

            # Sort by divergence and find the threshold where Claude accuracy
            # drops below 50% (using a sliding window)
            sorted_recs = sorted(records, key=lambda r: r["abs_divergence"])
            total_wins = sum(1 for r in sorted_recs if r["claude_was_right"])
            win_rate = total_wins / n

            if win_rate < 0.40:
                # Claude is mostly wrong — tighten significantly
                learned_threshold = 0.15
            elif win_rate > 0.60:
                # Claude is mostly right — loosen
                p80 = sorted_recs[int(n * 0.80)]["abs_divergence"]
                learned_threshold = min(0.60, p80)
            else:
                # Mixed — use median divergence where Claude wins
                wins = [r for r in sorted_recs if r["claude_was_right"]]
                if wins:
                    median_idx = len(wins) // 2
                    learned_threshold = wins[median_idx]["abs_divergence"]
                else:
                    learned_threshold = DEFAULT_THRESHOLDS.get(cat, DEFAULT_THRESHOLD)

            # Shrinkage toward default
            default = DEFAULT_THRESHOLDS.get(cat, DEFAULT_THRESHOLD)
            w = n / (n + SHRINKAGE_K)
            blended = w * learned_threshold + (1 - w) * default

            # Clamp to reasonable range
            blended = max(0.10, min(0.60, blended))
            thresholds[cat] = round(blended, 3)

        return thresholds

    def get_contrarian_accuracy(self, category: str = "") -> dict:
        """Get summary stats on Claude-vs-market accuracy.

        Returns:
            Dict with total, claude_wins, market_wins, win_rate
        """
        conn = self.db._get_conn()
        if category:
            rows = conn.execute(
                """SELECT claude_was_right FROM divergence_records
                   WHERE actual_outcome IS NOT NULL AND category = ?""",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT claude_was_right FROM divergence_records
                   WHERE actual_outcome IS NOT NULL""",
            ).fetchall()

        total = len(rows)
        claude_wins = sum(1 for r in rows if r["claude_was_right"])
        market_wins = total - claude_wins

        return {
            "total": total,
            "claude_wins": claude_wins,
            "market_wins": market_wins,
            "win_rate": claude_wins / total if total > 0 else 0.0,
        }
