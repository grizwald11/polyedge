"""Cross-Platform Consensus Aggregation — collects probability estimates from multiple sources.

Replaces the single community forecast lookup with a systematic collection
of all available cross-platform prices and community forecasts, feeding them
as weighted ForecastResult objects into the multi-model ensemble.

Prediction market prices (real money at stake) get higher default trust
(lower Brier prior) than community forecasts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.core.models import ForecastResult, Market, Platform

logger = logging.getLogger(__name__)

# Default Brier score priors for sources without historical data.
# Lower = more trusted. Prediction markets get more trust than community forecasts
# because real money is at stake.
DEFAULT_BRIER_PREDICTION_MARKET = 0.15
DEFAULT_BRIER_COMMUNITY_FORECAST = 0.20


class ConsensusAggregator:
    """Collects probability estimates from all available cross-platform sources.

    Sources:
    - Polymarket price (when trading on Kalshi)
    - Manifold Markets community prediction (free API)
    - Metaculus community prediction (free/paid)
    """

    def __init__(
        self,
        manifold_client=None,
        metaculus_client=None,
        polymarket_cross_ref=None,
    ):
        self.manifold = manifold_client
        self.metaculus = metaculus_client
        self.polymarket = polymarket_cross_ref

    async def get_all_forecasts(
        self,
        market: Market,
    ) -> list[ForecastResult]:
        """Fetch probability estimates from all available sources in parallel.

        Returns a list of ForecastResult objects, each tagged with a model_used
        name for Brier-score tracking in the ensemble.

        Args:
            market: The market to find cross-platform prices for

        Returns:
            List of ForecastResult (may be empty if no sources match)
        """
        tasks = []

        # Cross-platform prediction market price
        if market.platform == Platform.KALSHI and self.polymarket:
            tasks.append(self._get_polymarket_price(market))
        # Future: add Kalshi lookup when trading on Polymarket

        # Community forecasts
        if self.manifold:
            tasks.append(self._get_manifold_forecast(market))
        if self.metaculus:
            tasks.append(self._get_metaculus_forecast(market))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

        forecasts = []
        for r in results:
            if isinstance(r, ForecastResult):
                forecasts.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"Consensus source failed: {r}")

        if forecasts:
            sources = [f.model_used for f in forecasts]
            logger.info(
                f"Consensus for {market.ticker}: {len(forecasts)} sources "
                f"({', '.join(sources)})"
            )

        return forecasts

    async def _get_polymarket_price(self, market: Market) -> Optional[ForecastResult]:
        """Get Polymarket's price as a ForecastResult."""
        try:
            match = await self.polymarket.get_best_match(market.question)
            if match is None:
                return None

            price = match.get("yes_price")
            if price is None:
                return None

            volume = match.get("volume", 0)
            similarity = match.get("similarity", 0)

            # Confidence interval narrows with volume (proxy for market depth)
            # High volume = tight CI, low volume = wide CI
            if volume >= 1_000_000:
                ci_half = 0.05
            elif volume >= 100_000:
                ci_half = 0.08
            elif volume >= 10_000:
                ci_half = 0.12
            else:
                ci_half = 0.18

            return ForecastResult(
                probability=price,
                confidence_low=max(0.01, price - ci_half),
                confidence_high=min(0.99, price + ci_half),
                reasoning=f"Polymarket price: {price:.0%} (vol=${volume:,.0f}, sim={similarity:.2f})",
                model_used="polymarket_price",
            )
        except Exception as e:
            logger.info(f"Polymarket cross-ref failed: {e}")
            return None

    async def _get_manifold_forecast(self, market: Market) -> Optional[ForecastResult]:
        """Get Manifold Markets community forecast as a ForecastResult."""
        try:
            match = await self.manifold.get_best_match(market.question)
            if match is None:
                return None

            prob = match["community_prediction"]
            bettors = match.get("forecasters_count", 0)

            # CI narrows with bettor count
            ci_half = max(0.05, 0.20 - min(bettors, 100) * 0.001)

            return ForecastResult(
                probability=prob,
                confidence_low=max(0.01, prob - ci_half),
                confidence_high=min(0.99, prob + ci_half),
                reasoning=f"Manifold Markets: {prob:.0%} ({bettors} bettors)",
                model_used="manifold_community",
            )
        except Exception as e:
            logger.info(f"Manifold forecast failed: {e}")
            return None

    async def _get_metaculus_forecast(self, market: Market) -> Optional[ForecastResult]:
        """Get Metaculus community forecast as a ForecastResult."""
        try:
            match = await self.metaculus.get_best_match(market.question)
            if match is None:
                return None

            prob = match["community_prediction"]
            forecasters = match.get("forecasters_count", 0)

            # CI narrows with forecaster count
            ci_half = max(0.05, 0.20 - min(forecasters, 100) * 0.001)

            return ForecastResult(
                probability=prob,
                confidence_low=max(0.01, prob - ci_half),
                confidence_high=min(0.99, prob + ci_half),
                reasoning=f"Metaculus: {prob:.0%} ({forecasters} forecasters)",
                model_used="metaculus_community",
            )
        except Exception as e:
            logger.info(f"Metaculus forecast failed: {e}")
            return None
