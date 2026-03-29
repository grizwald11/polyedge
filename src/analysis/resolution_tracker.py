"""Resolution tracker — checks Kalshi and Polymarket for settled markets and updates calibration records.

Queries the Kalshi API and Polymarket Gamma API for markets we have predictions on
that have settled, then updates the calibration records with actual outcomes and Brier scores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.kalshi_client import KalshiClient
from src.core.models import Platform
from src.storage.database import Database

logger = logging.getLogger(__name__)


class ResolutionTracker:
    """Checks Kalshi and Polymarket APIs for settled markets and resolves calibration predictions."""

    def __init__(self, kalshi: KalshiClient, db: Database, polymarket_discovery=None):
        self.kalshi = kalshi
        self.db = db
        self.polymarket_discovery = polymarket_discovery  # Optional PolymarketDiscovery

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
        # Group by platform for routing
        market_platforms: dict[str, str] = {}
        for r in unresolved:
            mid = r["market_id"]
            if mid not in market_platforms:
                market_platforms[mid] = r.get("platform", "kalshi")

        logger.debug(f"Checking {len(market_platforms)} markets for resolution")

        resolved_count = 0
        for ticker, platform_str in market_platforms.items():
            try:
                platform = Platform(platform_str) if platform_str else Platform.KALSHI
                if platform == Platform.POLYMARKET:
                    result = await self._check_polymarket_market(ticker)
                else:
                    result = await self._check_single_market(ticker)
                if result is not None:
                    count = self._resolve_predictions(ticker, platform_str, result)
                    if count > 0:
                        resolved_count += 1
                        logger.info(
                            f"Resolved {ticker}: {'YES' if result else 'NO'} "
                            f"({count} predictions updated)"
                        )
            except Exception as e:
                logger.error(f"Failed to check resolution for {ticker}: {e}", exc_info=True)

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

    async def _check_polymarket_market(self, condition_id: str) -> Optional[bool]:
        """Check if a Polymarket market has resolved via Gamma API.

        Returns:
            True if YES, False if NO, None if not yet settled.
        """
        if self.polymarket_discovery is None:
            return None

        try:
            market_data = await self.polymarket_discovery.get_market_by_condition_id(condition_id)
            if market_data is None:
                return None

            resolved = market_data.get("resolved", False)
            if not resolved:
                return None

            # Gamma API provides resolution in various formats
            resolution = market_data.get("resolution", "")
            if resolution:
                return resolution.lower() == "yes"

            # Fallback: check outcome prices (1.0/0.0 after resolution)
            outcome_prices = market_data.get("outcomePrices")
            if outcome_prices:
                import json
                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    # Only accept price-based resolution when prices are near
                    # terminal values (1.0/0.0). Intermediate prices like 0.75
                    # could be incomplete settlement or API lag.
                    if yes_price >= 0.95:
                        return True
                    elif yes_price <= 0.05:
                        return False
                    else:
                        logger.debug(
                            f"Polymarket {condition_id}: resolved=True but "
                            f"outcomePrices not terminal (yes={yes_price:.2f}) — skipping"
                        )
                        return None

            return None
        except Exception as e:
            logger.debug(f"Polymarket resolution check failed for {condition_id}: {e}")
            return None

    def _resolve_predictions(self, market_id: str, platform: str, actual_outcome: bool) -> int:
        """Update all unresolved predictions for a market with the actual outcome.

        Computes Brier score and profit/loss for each prediction.
        Filters by both market_id AND platform to prevent cross-platform
        calibration data corruption.

        Returns:
            Number of predictions resolved.
        """
        now = datetime.now(timezone.utc).isoformat()
        outcome_int = 1 if actual_outcome else 0

        conn = self.db._get_conn()
        # Get unresolved predictions for this market+platform combination
        try:
            rows = conn.execute(
                """SELECT id, predicted_probability, market_price_at_prediction
                   FROM calibration_records
                   WHERE market_id = ? AND platform = ? AND actual_outcome IS NULL""",
                (market_id, platform),
            ).fetchall()
        except Exception:
            # Fallback for legacy records without platform column
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
