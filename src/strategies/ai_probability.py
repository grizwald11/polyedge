"""AI Probability Strategy — Claude-driven market assessment.

Scans markets, runs Claude probability assessment, generates signals
when detected edge exceeds the minimum threshold.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.config import Settings
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.ensemble import ensemble_forecast
from src.core.models import Market, Signal, Direction, StrategyName

logger = logging.getLogger(__name__)


class AIProbabilityStrategy:
    """Strategy 1: Claude assesses true probability, trade when market is mispriced."""

    def __init__(self, forecaster: ClaudeForecaster, settings: Settings):
        self.forecaster = forecaster
        self.settings = settings

    async def scan_for_opportunities(
        self,
        markets: list[Market],
        news_context: str = "",
    ) -> list[Signal]:
        """Scan markets for mispricing opportunities.

        Args:
            markets: Pre-filtered and ranked markets to assess
            news_context: Optional news context to include in prompts

        Returns:
            List of signals where edge >= min_edge_ai
        """
        signals = []
        max_assessments = self.settings.claude.max_assessments_per_cycle
        min_edge = self.settings.trading.min_edge_ai

        for market in markets[:max_assessments]:
            try:
                signal = await self._assess_single_market(market, news_context, min_edge)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Failed to assess {market.ticker}: {e}")

        logger.info(
            f"AI Probability: assessed {min(len(markets), max_assessments)} markets, "
            f"found {len(signals)} signals"
        )
        return signals

    async def _assess_single_market(
        self,
        market: Market,
        news_context: str,
        min_edge: float,
    ) -> Optional[Signal]:
        """Assess a single market and return a signal if edge is sufficient."""
        # Get Claude's forecast
        forecast = await self.forecaster.assess_market(
            market=market,
            news_context=news_context,
        )

        # Confidence gate: skip if confidence interval is too wide
        ci_width = forecast.confidence_high - forecast.confidence_low
        if ci_width > 0.40:
            logger.info(
                f"Skipping {market.ticker}: confidence interval too wide "
                f"({ci_width:.2f})"
            )
            return None

        # Run ensemble (combines Claude + market price)
        ensemble = ensemble_forecast(
            claude_forecast=forecast,
            market_price=market.yes_price,
        )

        # Calculate edge
        edge = ensemble.edge  # positive = YES underpriced, negative = NO underpriced

        if abs(edge) < min_edge:
            return None

        # Determine direction
        if edge > 0:
            direction = Direction.BUY_YES
            probability_estimate = ensemble.final_probability
            market_price = market.yes_price
        else:
            direction = Direction.BUY_NO
            probability_estimate = 1.0 - ensemble.final_probability
            market_price = market.no_price
            edge = abs(edge)

        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=edge,
            probability_estimate=probability_estimate,
            market_price=market_price,
            confidence=ensemble.confidence,
            reasoning=forecast.reasoning,
        )

        logger.info(
            f"Signal: {direction.value} on '{market.question[:50]}...' "
            f"(edge={edge:.1%}, claude={forecast.probability:.0%}, "
            f"market={market.yes_price:.0%})"
        )

        return signal
