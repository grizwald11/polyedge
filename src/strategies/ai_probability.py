"""AI Probability Strategy — Claude-driven market assessment.

Scans markets, runs Claude probability assessment, generates signals
when detected edge exceeds the minimum threshold.
"""

from __future__ import annotations

import logging
from typing import Optional

from typing import Optional as _Optional

from src.config import Settings
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.ensemble import ensemble_forecast
from src.analysis.market_classifier import classify_market
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.core.models import Market, Signal, Direction, StrategyName
from src.storage.database import Database

logger = logging.getLogger(__name__)


class AIProbabilityStrategy:
    """Strategy 1: Claude assesses true probability, trade when market is mispriced."""

    def __init__(
        self,
        forecaster: ClaudeForecaster,
        settings: Settings,
        db: _Optional[Database] = None,
        calibration_analyzer: _Optional[CalibrationAnalyzer] = None,
        data_enricher=None,
    ):
        self.forecaster = forecaster
        self.settings = settings
        self.db = db
        self.calibration_analyzer = calibration_analyzer
        self.data_enricher = data_enricher
        self._category_adjustments: dict[str, float] = {}
        self._category_base_rates: dict[str, dict] = {}

    def refresh_calibration_adjustments(self) -> None:
        """Reload per-category bias corrections from calibration data."""
        if self.calibration_analyzer is None:
            return
        try:
            self._category_adjustments = self.calibration_analyzer.get_category_adjustments()
            self._category_base_rates = self.calibration_analyzer.get_category_base_rates()
            if self._category_adjustments:
                logger.info(f"Loaded calibration adjustments: {self._category_adjustments}")
            if self._category_base_rates:
                logger.info(f"Loaded base rates for {len(self._category_base_rates)} categories")
        except Exception as e:
            logger.error(f"Failed to load calibration adjustments: {e}")

    def _build_base_rate_context(self, category_value: str) -> str:
        """Build base rate context string from calibration history."""
        stats = self._category_base_rates.get(category_value)
        if not stats:
            return ""
        total = stats["total"]
        yes_rate = stats["yes_rate"]
        no_rate = 1.0 - yes_rate
        return (
            f"HISTORICAL BASE RATE: In {total} resolved {category_value} markets, "
            f"{yes_rate:.0%} resolved YES and {no_rate:.0%} resolved NO."
        )

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
        # Refresh calibration adjustments once per scan cycle
        self.refresh_calibration_adjustments()

        signals = []
        max_assessments = self.settings.claude.max_assessments_per_cycle
        min_edge = self.settings.trading.min_edge_ai

        # Determine which markets get cross-checked (top N by volume)
        cross_check_enabled = self.settings.claude.cross_check_enabled
        cross_check_top_n = self.settings.claude.cross_check_top_n
        cross_check_tickers: set[str] = set()
        if cross_check_enabled:
            sorted_by_vol = sorted(markets[:max_assessments], key=lambda m: m.volume_24h, reverse=True)
            cross_check_tickers = {m.ticker for m in sorted_by_vol[:cross_check_top_n]}

        for market in markets[:max_assessments]:
            try:
                use_cross_check = market.ticker in cross_check_tickers
                signal = await self._assess_single_market(
                    market, news_context, min_edge, use_cross_check=use_cross_check
                )
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
        use_cross_check: bool = False,
    ) -> Optional[Signal]:
        """Assess a single market and return a signal if edge is sufficient."""
        category = classify_market(market)
        base_rate_context = self._build_base_rate_context(category.value)

        # Use data enricher for context if available, otherwise fall back to news_context
        if self.data_enricher:
            try:
                news_context = await self.data_enricher.get_context(market)
            except Exception as e:
                logger.warning(f"Data enricher failed for {market.ticker}, using news_context: {e}")

        # Get Claude's forecast (cross-check or regular)
        if use_cross_check:
            forecast = await self.forecaster.cross_check_assess(
                market=market,
                news_context=news_context,
                base_rate_context=base_rate_context,
            )
            if forecast is None:
                logger.info(f"Skipping {market.ticker}: cross-check disagreement too high")
                return None
        else:
            forecast = await self.forecaster.assess_market(
                market=market,
                news_context=news_context,
                base_rate_context=base_rate_context,
            )

        # Skip if Claude failed to parse the response (fallback 0.5 is unreliable)
        if getattr(forecast, "parse_failed", False):
            logger.warning(f"Skipping {market.ticker}: Claude response parse failed")
            return None

        # Apply calibration adjustment before ensemble
        adjustment = self._category_adjustments.get(category.value, 0.0)
        if adjustment != 0.0:
            original = forecast.probability
            forecast.probability = max(0.01, min(0.99, forecast.probability + adjustment))
            logger.debug(
                f"Calibration adjustment for {category.value}: "
                f"{original:.3f} → {forecast.probability:.3f} (adj={adjustment:+.3f})"
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
            claude_weight=self.settings.claude.ensemble_weight,
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
