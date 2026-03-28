"""AI Probability Strategy — Claude-driven market assessment.

Scans markets, runs Claude probability assessment, generates signals
when detected edge exceeds the minimum threshold.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import Settings
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.ensemble import ensemble_forecast, multi_model_ensemble
from src.analysis.market_classifier import classify_market
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.core.models import Market, Signal, Direction, StrategyName, ForecastResult
from src.storage.database import Database

logger = logging.getLogger(__name__)


class AIProbabilityStrategy:
    """Strategy 1: Claude assesses true probability, trade when market is mispriced."""

    def __init__(
        self,
        forecaster: ClaudeForecaster,
        settings: Settings,
        db: Optional[Database] = None,
        calibration_analyzer: Optional[CalibrationAnalyzer] = None,
        data_enricher=None,
    ):
        self.forecaster = forecaster
        self.settings = settings
        self.db = db
        self.calibration_analyzer = calibration_analyzer
        self.data_enricher = data_enricher
        self._category_adjustments: dict[str, float] = {}
        self._category_base_rates: dict[str, dict] = {}
        self._category_brier_scores: dict[str, float] = {}

    def refresh_calibration_adjustments(self) -> None:
        """Reload per-category bias corrections from calibration data."""
        if self.calibration_analyzer is None:
            return
        try:
            self._category_adjustments = self.calibration_analyzer.get_category_adjustments()
            self._category_base_rates = self.calibration_analyzer.get_category_base_rates()
            # Load per-category Brier scores for accuracy gating
            report = self.calibration_analyzer.generate_report()
            self._category_brier_scores = {
                cs.category: cs.brier_score
                for cs in report.category_stats
                if cs.count >= 5
            }
            if self._category_adjustments:
                logger.info(f"Loaded calibration adjustments: {self._category_adjustments}")
            if self._category_base_rates:
                logger.info(f"Loaded base rates for {len(self._category_base_rates)} categories")
            if self._category_brier_scores:
                logger.info(f"Category Brier scores: {self._category_brier_scores}")
        except Exception as e:
            logger.error(f"Failed to load calibration adjustments: {e}", exc_info=True)

    async def _get_community_forecast(self, market: Market) -> Optional[ForecastResult]:
        """Get community forecast from Manifold Markets or Metaculus.

        Tries Manifold first (free, no auth), falls back to Metaculus.
        Returns None if no matching question found.
        """
        if not self.data_enricher:
            return None

        # Try Manifold Markets first (free, always available)
        try:
            match = await self.data_enricher.manifold.get_best_match(market.question)
            if match is not None:
                prob = match["community_prediction"]
                bettors = match.get("forecasters_count", 0)
                # Heuristic: CI narrows linearly with bettor count (0.20 at 0 bettors → 0.10 at 100)
                ci_half = max(0.05, 0.20 - min(bettors, 100) * 0.001)
                return ForecastResult(
                    probability=prob,
                    confidence_low=max(0.0, prob - ci_half),
                    confidence_high=min(1.0, prob + ci_half),
                    reasoning=f"Manifold Markets: {prob:.0%} ({bettors} bettors)",
                    model_used="manifold_community",
                )
        except Exception as e:
            logger.info(f"Manifold forecast unavailable for {market.ticker}: {e}")

        # Fall back to Metaculus
        try:
            match = await self.data_enricher.metaculus.get_best_match(market.question)
            if match is not None:
                prob = match["community_prediction"]
                forecasters = match.get("forecasters_count", 0)
                ci_half = max(0.05, 0.20 - min(forecasters, 100) * 0.001)
                return ForecastResult(
                    probability=prob,
                    confidence_low=max(0.0, prob - ci_half),
                    confidence_high=min(1.0, prob + ci_half),
                    reasoning=f"Metaculus community: {prob:.0%} ({forecasters} forecasters)",
                    model_used="metaculus_community",
                )
        except Exception as e:
            logger.info(f"Metaculus forecast unavailable for {market.ticker}: {e}")

        return None

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

        cross_check_enabled = self.settings.claude.cross_check_enabled
        cross_check_top_n = self.settings.claude.cross_check_top_n

        # First pass: assess all markets without cross-check
        initial_signals: list[Signal] = []
        for market in markets[:max_assessments]:
            try:
                signal = await self._assess_single_market(
                    market, news_context, min_edge, use_cross_check=False
                )
                if signal:
                    initial_signals.append(signal)
            except Exception as e:
                logger.error(f"Failed to assess {market.ticker}: {e}", exc_info=True)

        if not cross_check_enabled:
            signals = initial_signals
        else:
            # Cross-check only top N signals by edge (saves API calls)
            initial_signals.sort(key=lambda s: abs(s.edge), reverse=True)
            cross_check_candidates = initial_signals[:cross_check_top_n]
            auto_pass = initial_signals[cross_check_top_n:]

            for signal in cross_check_candidates:
                market = next((m for m in markets if m.ticker == signal.market_id), None)
                if market is None:
                    continue
                try:
                    validated_signal = await self._assess_single_market(
                        market, news_context, min_edge, use_cross_check=True
                    )
                    if validated_signal:
                        signals.append(validated_signal)
                    else:
                        logger.info(
                            f"Cross-check rejected signal for {market.ticker}"
                        )
                except Exception as e:
                    logger.error(f"Cross-check failed for {market.ticker}: {e}", exc_info=True)

            # Signals outside top N pass without cross-check
            signals.extend(auto_pass)

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
        # Staleness check: skip re-assessment if recent prediction is still fresh
        if self.db:
            try:
                latest = self.db.get_latest_prediction(market.ticker)
                if latest:
                    predicted_at = datetime.fromisoformat(latest["predicted_at"])
                    age = datetime.now(timezone.utc) - predicted_at
                    price_move = abs(market.yes_price - latest["market_price_at_prediction"])
                    if age < timedelta(hours=48) and price_move < 0.10:
                        logger.debug(
                            f"Skipping {market.ticker}: recent prediction "
                            f"({age.total_seconds()/3600:.0f}h old, price moved {price_move:.2f})"
                        )
                        return None
            except Exception as e:
                logger.debug(f"Staleness check failed for {market.ticker}: {e}")

        category = classify_market(market)

        # Category accuracy gating: skip categories where we're poorly calibrated
        cat_brier = self._category_brier_scores.get(category.value)
        if cat_brier is not None and cat_brier > 0.30:
            logger.info(
                f"Skipping {market.ticker}: category {category.value} has poor "
                f"Brier score ({cat_brier:.3f} > 0.30)"
            )
            return None
        # Raise min edge for categories with mediocre calibration (Brier 0.20-0.30)
        if cat_brier is not None and cat_brier > 0.20:
            min_edge = max(min_edge, 0.08)  # Require 8% edge instead of 5%

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

        # Divergence gate: reject extreme disagreement with the market.
        # When Claude diverges by >40% from the market price, it's far more
        # likely a hallucination than a genuine edge (e.g., Venezuela 80% vs 2%).
        # For extreme-price markets (<15¢ or >85¢), TIGHTEN the threshold:
        # a 10% absolute divergence on a $0.05 market is a 200% relative
        # disagreement — almost certainly a hallucination, not edge.
        max_div = self.settings.claude.max_divergence_from_market
        divergence = abs(forecast.probability - market.yes_price)
        if market.yes_price < 0.15 or market.yes_price > 0.85:
            max_div = min(max_div, 0.25)
            # Also check relative divergence: on extreme-price markets, even small
            # absolute divergences can be huge relative to the price.
            base_price = max(market.yes_price, 1.0 - market.yes_price)
            relative_div = divergence / base_price if base_price > 0 else 0
            if relative_div > 1.2:
                logger.warning(
                    f"Rejecting {market.ticker}: relative divergence {relative_div:.1f}x "
                    f"on extreme-price market ({market.yes_price:.0%})"
                )
                return None
        if divergence > max_div:
            logger.warning(
                f"Rejecting {market.ticker}: Claude ({forecast.probability:.0%}) diverges "
                f"{divergence:.0%} from market ({market.yes_price:.0%}) — exceeds max {max_div:.0%}"
            )
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

        # Confidence gate: skip if confidence interval is too wide.
        # Category-specific thresholds: data-rich categories (Politics, Fed)
        # should have narrower CIs; inherently uncertain categories allow wider.
        ci_width = forecast.confidence_high - forecast.confidence_low
        ci_thresholds = {
            "Politics": 0.35, "Elections": 0.35, "Economics": 0.35,
            "Financials": 0.35, "Fed": 0.35,
            "World": 0.45, "Geopolitics": 0.45,
            "Entertainment": 0.50, "Culture": 0.50,
        }
        max_ci = ci_thresholds.get(category.value, 0.40)
        if ci_width > max_ci:
            logger.info(
                f"Skipping {market.ticker}: confidence interval too wide "
                f"({ci_width:.2f} > {max_ci:.2f} for {category.value})"
            )
            return None

        # Try to get community forecast (Manifold or Metaculus) as a second model
        community_forecast = await self._get_community_forecast(market)

        if community_forecast is not None:
            # Multi-model ensemble: Claude + community forecast + market price
            ensemble = multi_model_ensemble(
                forecasts=[forecast, community_forecast],
                market_price=market.yes_price,
                market_weight=1.0 - self.settings.claude.ensemble_weight,
            )
        else:
            # Single-model fallback: Claude + market price
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
