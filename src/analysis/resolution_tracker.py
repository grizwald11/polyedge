"""Resolution tracker — checks Kalshi for settled markets and updates calibration records.

Queries the Kalshi API for markets we have predictions on that have settled,
then updates the calibration records with actual outcomes and Brier scores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.kalshi_client import KalshiClient
from src.storage.database import Database

logger = logging.getLogger(__name__)


class ResolutionTracker:
    """Checks Kalshi API for settled markets and resolves calibration predictions."""

    def __init__(self, kalshi: KalshiClient, db: Database):
        self.kalshi = kalshi
        self.db = db

    async def check_resolutions(self) -> int:
        """Check all unresolved predictions against the Kalshi API.

        For each unresolved prediction, queries Kalshi to see if the market
        has settled. If so, records the actual outcome and computes metrics.

        Returns:
            Number of newly resolved markets.
        """
        unresolved = self.db.get_unresolved_predictions()
        if not unresolved:
            logger.debug("No unresolved predictions to check")
            return 0

        # Deduplicate market tickers (multiple predictions per market possible)
        tickers = list({r["market_id"] for r in unresolved})
        logger.debug(f"Checking {len(tickers)} markets for resolution")

        resolved_count = 0
        for ticker in tickers:
            try:
                result = await self._check_single_market(ticker)
                if result is not None:
                    count = self._resolve_predictions(ticker, result)
                    if count > 0:
                        resolved_count += 1
                        logger.info(
                            f"Resolved {ticker}: {'YES' if result else 'NO'} "
                            f"({count} predictions updated)"
                        )
            except Exception as e:
                logger.error(f"Failed to check resolution for {ticker}: {e}")

        if resolved_count > 0:
            logger.info(f"Resolved {resolved_count} markets this cycle")

        return resolved_count

    async def _check_single_market(self, ticker: str) -> Optional[bool]:
        """Check if a single market has settled on Kalshi.

        Returns:
            True if YES, False if NO, None if not yet settled.
        """
        market_data = await self.kalshi.get_market(ticker)
        if market_data is None:
            return None

        status = market_data.get("status", "")
        result = market_data.get("result", "")

        if status != "settled" or not result:
            return None

        # Kalshi result is "yes" or "no" (lowercase)
        if result.lower() == "yes":
            return True
        elif result.lower() == "no":
            return False
        else:
            logger.warning(f"Unknown result value for {ticker}: {result}")
            return None

    def _resolve_predictions(self, market_id: str, actual_outcome: bool) -> int:
        """Update all unresolved predictions for a market with the actual outcome.

        Computes Brier score and profit/loss for each prediction.

        Returns:
            Number of predictions resolved.
        """
        now = datetime.now(timezone.utc).isoformat()
        outcome_int = 1 if actual_outcome else 0

        conn = self.db._get_conn()
        # Get unresolved predictions for this market
        rows = conn.execute(
            """SELECT id, predicted_probability, market_price_at_prediction
               FROM calibration_records
               WHERE market_id = ? AND actual_outcome IS NULL""",
            (market_id,),
        ).fetchall()

        if not rows:
            return 0

        for row in rows:
            pred_prob = row["predicted_probability"]
            brier = (pred_prob - float(outcome_int)) ** 2

            conn.execute(
                """UPDATE calibration_records
                   SET actual_outcome = ?, resolved_at = ?, brier_score = ?
                   WHERE id = ?""",
                (outcome_int, now, brier, row["id"]),
            )

        conn.commit()
        return len(rows)
