"""AI Probability Strategy — Claude-driven market assessment.

Scans markets, runs Claude probability assessment, generates signals
when detected edge exceeds the minimum threshold.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.analysis.adversarial_analyzer import AdversarialAnalyzer
from src.analysis.analogue_finder import AnalogueFinder
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.analysis.contrarian_tracker import ContrarianTracker
from src.analysis.resolution_analyzer import ResolutionAnalyzer
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.decomposer import QuestionDecomposer, is_compound_question
from src.analysis.ensemble import compute_market_efficiency, ensemble_forecast, multi_model_ensemble
from src.analysis.market_classifier import classify_market
from src.analysis.temporal_analyzer import TemporalAnalyzer
from src.config import Settings
from src.core.models import Direction, ForecastResult, Market, Signal, StrategyName
from src.data.consensus_aggregator import ConsensusAggregator
from src.storage.database import Database

logger = logging.getLogger(__name__)


class AIProbabilityStrategy:
    """Strategy 1: Claude assesses true probability, trade when market is mispriced."""

    # L-2: Category divergence thresholds — max allowed divergence between
    # Claude's estimate and the market price before we reject as hallucination.
    MAX_DIVERGENCE_DATA_RICH = 0.30      # Politics, Elections, Fed, Economics, Financials
    MAX_DIVERGENCE_UNCERTAIN = 0.45      # World, Geopolitics
    MAX_DIVERGENCE_SPECULATIVE = 0.50    # Culture, Entertainment
    MAX_DIVERGENCE_EXTREME_PRICE = 0.25  # Extreme prices (<15¢ or >85¢)

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
        self.decomposer = QuestionDecomposer(forecaster)
        self.temporal_analyzer = TemporalAnalyzer()
        self.analogue_finder: AnalogueFinder | None = AnalogueFinder(db) if db else None
        self.contrarian_tracker: ContrarianTracker | None = ContrarianTracker(db) if db else None
        self.resolution_analyzer = ResolutionAnalyzer(forecaster)
        self.adversarial_analyzer = AdversarialAnalyzer(forecaster)
        self.consensus_aggregator: Optional[ConsensusAggregator] = None
        # Initialize consensus aggregator if data enricher has the required clients
        if data_enricher:
            self.consensus_aggregator = ConsensusAggregator(
                manifold_client=getattr(data_enricher, 'manifold', None),
                metaculus_client=getattr(data_enricher, 'metaculus', None),
                polymarket_cross_ref=getattr(data_enricher, 'polymarket_cross_ref', None),
            )
        self._category_adjustments: dict[str, float] = {}
        self._regime_edge_multiplier: float = 1.0
        self._category_base_rates: dict[str, dict] = {}
        self._category_brier_scores: dict[str, float] = {}

    def set_regime_edge_multiplier(self, multiplier: float) -> None:
        """Set the regime-based edge multiplier.

        Called each cycle by regime detector. In high-volatility regimes,
        increases the effective min_edge to avoid false signals.
        """
        self._regime_edge_multiplier = max(0.5, min(multiplier, 3.0))

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
        min_edge = self.settings.trading.min_edge_ai * self._regime_edge_multiplier

        cross_check_enabled = self.settings.claude.cross_check_enabled
        cross_check_top_n = self.settings.claude.cross_check_top_n

        # First pass: assess all markets concurrently with semaphore
        concurrency = getattr(self.settings.claude, 'max_concurrent_assessments', 5)
        sem = asyncio.Semaphore(concurrency)

        async def _assess_with_semaphore(market: Market) -> Optional[Signal]:
            async with sem:
                try:
                    return await self._assess_single_market(
                        market, news_context, min_edge, use_cross_check=False
                    )
                except Exception as e:
                    logger.error(f"Failed to assess {market.ticker}: {e}", exc_info=True)
                    return None

        results = await asyncio.gather(
            *[_assess_with_semaphore(m) for m in markets[:max_assessments]]
        )
        initial_signals: list[Signal] = [s for s in results if s is not None]

        if not cross_check_enabled:
            signals = initial_signals
        else:
            # H-1: Cross-check the weakest signals (lowest edge), which are most
            # likely to be noise. Auto-pass the highest-edge signals, which are
            # most likely real. This prioritizes validation where it matters most.
            initial_signals.sort(key=lambda s: abs(s.edge), reverse=True)
            auto_pass = initial_signals[:len(initial_signals) - cross_check_top_n] if len(initial_signals) > cross_check_top_n else []
            cross_check_candidates = initial_signals[len(initial_signals) - cross_check_top_n:] if len(initial_signals) > cross_check_top_n else initial_signals

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
        """Assess a single market and return a signal if edge is sufficient.

        L-2: This is a long method (~255 lines) with the following logical stages:
        1. Staleness check — skip if recent prediction is still fresh
        2. Classification — determine market category, apply accuracy gating
        3. Forecasting — get Claude's probability estimate (with optional cross-check)
        4. Divergence & confidence gates — reject hallucinations and wide CIs
        5. Ensemble — combine Claude + community forecasts + calibration adjustments
        6. Edge calculation — compute edge, check significance and minimum threshold
        7. Signal generation — build and return Signal if edge is sufficient
        """
        # Staleness check: skip re-assessment if recent prediction is still fresh
        if self.db:
            try:
                latest = self.db.get_latest_prediction(market.ticker)
                if latest:
                    predicted_at = datetime.fromisoformat(latest["predicted_at"])
                    age = datetime.now(timezone.utc) - predicted_at
                    price_move = abs(market.yes_price - latest["market_price_at_prediction"])
                    staleness_hours = self.settings.claude.reassessment_interval_hours
                    staleness_price_move = self.settings.claude.reassessment_price_move
                    # Use relative price move to be context-sensitive across all price ranges
                    cached_price = latest["market_price_at_prediction"]
                    relative_move = price_move / max(cached_price, 0.01) if cached_price > 0 else price_move
                    if age < timedelta(hours=staleness_hours) and relative_move < staleness_price_move:
                        logger.debug(
                            f"Skipping {market.ticker}: recent prediction "
                            f"({age.total_seconds()/3600:.0f}h old, price moved {price_move:.2f} "
                            f"({relative_move:.0%} relative))"
                        )
                        return None
            except Exception as e:
                logger.debug(f"Staleness check failed for {market.ticker}: {e}")

        category = classify_market(market)

        # M-8: Defense-in-depth — skip excluded categories even if scanner missed them.
        # The scanner already filters, but strategies should enforce independently.
        excluded = set(self.settings.scanning.exclude_categories)
        if category.value in excluded:
            logger.debug(f"Skipping {market.ticker}: category {category.value} is excluded")
            return None

        # Category accuracy gating: skip categories where we're poorly calibrated
        # Brier > 0.30 = worse than random guessing (0.25) → skip entirely
        # Brier 0.20-0.30 = poor calibration → require higher edge (8% vs 5%)
        cat_brier = self._category_brier_scores.get(category.value)
        if cat_brier is not None and cat_brier > 0.30:
            logger.warning(
                f"Category accuracy gate: skipping {market.ticker} — "
                f"{category.value} Brier score {cat_brier:.3f} > 0.30 threshold"
            )
            return None
        # Raise min edge for categories with mediocre calibration (Brier 0.20-0.30)
        if cat_brier is not None and cat_brier > 0.20:
            min_edge = max(min_edge, 0.08)  # Require 8% edge instead of 5%

        base_rate_context = self._build_base_rate_context(category.value)

        # Build historical accuracy context for this category
        accuracy_context = ""
        if self.calibration_analyzer:
            try:
                accuracy_context = self.calibration_analyzer.get_accuracy_context(category.value)
            except Exception as e:
                logger.debug(f"Accuracy context failed for {category.value}: {e}")

        # Analyze resolution criteria for ambiguities (cached per market)
        resolution_context = ""
        try:
            resolution_analysis = await self.resolution_analyzer.analyze(market)
            if resolution_analysis.is_high_risk:
                logger.info(
                    f"Skipping {market.ticker}: resolution criteria too ambiguous "
                    f"(risk={resolution_analysis.risk_score:.0%})"
                )
                return None
            resolution_context = resolution_analysis.format_for_prompt()
        except Exception as e:
            logger.debug(f"Resolution analysis failed for {market.ticker}: {e}")

        # Find similar resolved markets as reference points
        if self.analogue_finder:
            try:
                analogues = self.analogue_finder.find_analogues(
                    market.question, category=category.value,
                )
                analogue_context = self.analogue_finder.format_for_prompt(analogues)
                if analogue_context:
                    accuracy_context = f"{accuracy_context}\n\n{analogue_context}" if accuracy_context else analogue_context
            except Exception as e:
                logger.debug(f"Analogue finder failed for {market.ticker}: {e}")

        # Append resolution analysis to accuracy context
        if resolution_context:
            accuracy_context = f"{accuracy_context}\n\n{resolution_context}" if accuracy_context else resolution_context

        # Use data enricher for context if available, otherwise fall back to news_context
        if self.data_enricher:
            try:
                news_context = await self.data_enricher.get_context(market)
            except Exception as e:
                logger.warning(f"Data enricher failed for {market.ticker}, using news_context: {e}")

        # Try decomposition for compound questions (multi-step reasoning)
        decomposition_enabled = getattr(self.settings.claude, 'decomposition_enabled', True)
        if decomposition_enabled and is_compound_question(market.question):
            try:
                decomposed = await self.decomposer.decompose_and_assess(
                    market=market,
                    news_context=news_context,
                    base_rate_context=base_rate_context,
                )
                if decomposed is not None:
                    logger.info(
                        f"Decomposition succeeded for {market.ticker}: "
                        f"{decomposed.probability:.0%} ({decomposed.reasoning[:80]}...)"
                    )
                    forecast = decomposed
                    # Skip the regular Claude call — jump to divergence gate
                    # by setting a flag; the forecast variable is already set
                    _used_decomposition = True
                else:
                    _used_decomposition = False
            except Exception as e:
                logger.warning(f"Decomposition failed for {market.ticker}: {e}")
                _used_decomposition = False
        else:
            _used_decomposition = False

        # Get Claude's forecast (cross-check or regular) — skip if decomposition succeeded
        if _used_decomposition:
            pass  # forecast already set by decomposer
        elif use_cross_check:
            try:
                forecast = await self.forecaster.cross_check_assess(
                    market=market,
                    news_context=news_context,
                    base_rate_context=base_rate_context,
                    accuracy_context=accuracy_context,
                )
            except Exception as e:
                logger.warning(
                    f"Cross-check assessment failed for {market.ticker}: {e}",
                    exc_info=True,
                )
                return None
            if forecast is None:
                logger.info(f"Skipping {market.ticker}: cross-check disagreement too high")
                return None
        else:
            forecast = await self.forecaster.assess_market(
                market=market,
                news_context=news_context,
                base_rate_context=base_rate_context,
                accuracy_context=accuracy_context,
            )

        # Skip if Claude failed to parse the response (fallback 0.5 is unreliable)
        if getattr(forecast, "parse_failed", False):
            logger.warning(f"Skipping {market.ticker}: Claude response parse failed")
            return None

        # Record Claude-vs-market divergence for contrarian accuracy tracking
        if self.contrarian_tracker:
            try:
                self.contrarian_tracker.record_divergence(
                    market_id=market.ticker,
                    category=category.value,
                    claude_estimate=forecast.probability,
                    market_price=market.yes_price,
                )
            except Exception as e:
                logger.debug(f"Contrarian tracking failed for {market.ticker}: {e}")

        # Pre-mortem adversarial analysis: force counterargument reasoning
        # Only run when edge is significant and CI is tight (avoid wasting API calls)
        preliminary_edge = abs(forecast.probability - market.yes_price)
        ci_width_pre = forecast.confidence_high - forecast.confidence_low
        if self.adversarial_analyzer.should_run(preliminary_edge, ci_width_pre):
            try:
                adversarial = await self.adversarial_analyzer.run_premortem(market, forecast)
                if adversarial.plausibility > 0.5:
                    forecast = self.adversarial_analyzer.apply_adjustment(forecast, adversarial)
            except Exception as e:
                logger.debug(f"Adversarial analysis failed for {market.ticker}: {e}")

        # Divergence gate: reject extreme disagreement with the market.
        # When Claude diverges by >40% from the market price, it's far more
        # likely a hallucination than a genuine edge (e.g., Venezuela 80% vs 2%).
        # For extreme-price markets (<15¢ or >85¢), TIGHTEN the threshold:
        # a 10% absolute divergence on a $0.05 market is a 200% relative
        # disagreement — almost certainly a hallucination, not edge.
        # Per-category divergence thresholds: data-rich categories (Politics,
        # Fed) tend to be well-priced, so large Claude divergences are more
        # likely hallucinations. Uncertain categories (Culture, World) can
        # legitimately diverge more.
        base_max_div = self.settings.claude.max_divergence_from_market
        # Use data-driven thresholds from contrarian tracker when available,
        # otherwise fall back to hardcoded defaults.
        if self.contrarian_tracker:
            try:
                dynamic_thresholds = self.contrarian_tracker.get_dynamic_thresholds()
                category_div_overrides = dynamic_thresholds
            except Exception:
                category_div_overrides = {
                    "Politics": self.MAX_DIVERGENCE_DATA_RICH,
                    "Elections": self.MAX_DIVERGENCE_DATA_RICH,
                    "Fed": self.MAX_DIVERGENCE_DATA_RICH,
                    "Economics": self.MAX_DIVERGENCE_DATA_RICH,
                    "Financials": self.MAX_DIVERGENCE_DATA_RICH,
                    "World": self.MAX_DIVERGENCE_UNCERTAIN,
                    "Geopolitics": self.MAX_DIVERGENCE_UNCERTAIN,
                    "Entertainment": self.MAX_DIVERGENCE_SPECULATIVE,
                    "Culture": self.MAX_DIVERGENCE_SPECULATIVE,
                }
        else:
            category_div_overrides = {
                "Politics": self.MAX_DIVERGENCE_DATA_RICH,
                "Elections": self.MAX_DIVERGENCE_DATA_RICH,
                "Fed": self.MAX_DIVERGENCE_DATA_RICH,
                "Economics": self.MAX_DIVERGENCE_DATA_RICH,
                "Financials": self.MAX_DIVERGENCE_DATA_RICH,
                "World": self.MAX_DIVERGENCE_UNCERTAIN,
                "Geopolitics": self.MAX_DIVERGENCE_UNCERTAIN,
                "Entertainment": self.MAX_DIVERGENCE_SPECULATIVE,
                "Culture": self.MAX_DIVERGENCE_SPECULATIVE,
            }
        max_div = category_div_overrides.get(category.value, base_max_div)
        divergence = abs(forecast.probability - market.yes_price)
        if market.yes_price < 0.15 or market.yes_price > 0.85:
            max_div = min(max_div, self.MAX_DIVERGENCE_EXTREME_PRICE)
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

        # Collect cross-platform consensus forecasts (Polymarket, Manifold, Metaculus)
        consensus_forecasts: list[ForecastResult] = []
        if self.consensus_aggregator:
            try:
                consensus_forecasts = await self.consensus_aggregator.get_all_forecasts(market)
            except Exception as e:
                logger.info(f"Consensus aggregation failed for {market.ticker}: {e}")

        if not consensus_forecasts:
            # Fall back to legacy single community forecast
            community_forecast = await self._get_community_forecast(market)
            if community_forecast is not None:
                consensus_forecasts = [community_forecast]

        # Compute market efficiency from real data (volume, liquidity, time)
        market_eff = compute_market_efficiency(
            volume_24h=market.volume_24h or 0,
            liquidity=market.liquidity or 0,
            days_to_resolution=market.days_to_resolution,
        )

        # Build Brier score data for ensemble weighting
        cat_brier = self._category_brier_scores.get(category.value)
        cat_brier_dict = None
        if cat_brier is not None and forecast.model_used:
            cat_brier_dict = {category.value: {forecast.model_used: cat_brier}}

        if consensus_forecasts:
            # Multi-model ensemble: Claude + all consensus sources + market price
            ensemble = multi_model_ensemble(
                forecasts=[forecast] + consensus_forecasts,
                market_price=market.yes_price,
                market_weight=1.0 - self.settings.claude.ensemble_weight,
                market_efficiency=market_eff,
                category=category.value,
                category_brier_scores=cat_brier_dict,
            )
        else:
            # Single-model fallback: Claude + market price
            ensemble = ensemble_forecast(
                claude_forecast=forecast,
                market_price=market.yes_price,
                claude_weight=self.settings.claude.ensemble_weight,
                market_efficiency=market_eff,
            )

        # Apply calibration adjustment to the ensemble final probability.
        # M-4: When consensus sources are present (multi-model ensemble), dampen
        # the adjustment to avoid double-counting bias that consensus already corrects.
        raw_adjustment = self._category_adjustments.get(category.value, 0.0)
        has_consensus = len(getattr(ensemble, "individual_forecasts", [])) > 1
        adjustment = raw_adjustment * 0.5 if (has_consensus and raw_adjustment != 0.0) else raw_adjustment
        if adjustment != 0.0:
            original_prob = ensemble.final_probability
            adjusted_prob = max(0.01, min(0.99, ensemble.final_probability + adjustment))
            # Recalculate edge after adjustment using the same market price
            from src.core.models import EnsembleForecast as EnsembleForecastModel
            # Recalculate confidence: shift CI by the same adjustment amount
            # and derive confidence from how tight the CI is around new prob
            orig_ci_width = 0.0
            if ensemble.individual_forecasts:
                f0 = ensemble.individual_forecasts[0]
                ci_low = getattr(f0, "confidence_low", None)
                ci_high = getattr(f0, "confidence_high", None)
                if ci_low is not None and ci_high is not None:
                    orig_ci_width = ci_high - ci_low
            # Confidence = 1 - CI_width (narrower CI = higher confidence),
            # clamped to [0.1, 0.95]
            adjusted_confidence = max(0.1, min(0.95, 1.0 - orig_ci_width)) if orig_ci_width > 0 else ensemble.confidence
            ensemble = EnsembleForecastModel(
                final_probability=adjusted_prob,
                individual_forecasts=ensemble.individual_forecasts,
                market_price=ensemble.market_price,
                edge=adjusted_prob - market.yes_price,
                confidence=adjusted_confidence,
            )
            logger.debug(
                f"Calibration adjustment for {category.value}: "
                f"{original_prob:.3f} → {adjusted_prob:.3f} (adj={adjustment:+.3f})"
            )

        # Calculate edge
        edge = ensemble.edge  # positive = YES underpriced, negative = NO underpriced

        # Temporal analysis: discount edge if price has "already priced in" the move
        if self.db:
            try:
                temporal = self.temporal_analyzer.analyze(
                    market_id=market.ticker,
                    current_price=market.yes_price,
                    claude_estimate=ensemble.final_probability,
                    db=self.db,
                )
                if temporal.has_sufficient_data:
                    discount = self.temporal_analyzer.compute_edge_discount(temporal)
                    if discount < 1.0:
                        original_edge = edge
                        edge = edge * discount
                        logger.info(
                            f"Temporal discount for {market.ticker}: edge "
                            f"{original_edge:+.3f} → {edge:+.3f} "
                            f"(discount={discount:.2f}, priced_in={temporal.already_priced_in_pct:.0%})"
                        )
            except Exception as e:
                logger.debug(f"Temporal analysis failed for {market.ticker}: {e}")

        # M-7: Edge significance check — reject when market price falls inside
        # Claude's confidence interval. If CI is [55%, 65%] and market is at 60%,
        # Claude isn't confident the market is wrong. But if market is at 45% and
        # CI is [55%, 65%], the entire CI is above the market — genuine edge.
        ci_low = ensemble.final_probability - ci_width / 2.0
        ci_high = ensemble.final_probability + ci_width / 2.0
        market_inside_ci = ci_low <= market.yes_price <= ci_high
        if market_inside_ci and abs(edge) < 0.15:
            logger.debug(
                f"Edge significance rejection: {market.ticker} market price "
                f"{market.yes_price:.3f} is within CI [{ci_low:.3f}, {ci_high:.3f}] "
                f"— no confident edge"
            )
            return None

        if abs(edge) < min_edge:
            # H-13: Log distinct reasons for edge rejection
            if edge < 0 and abs(edge) < min_edge:
                logger.debug(
                    f"Edge rejection (negative): {market.ticker} edge={edge:+.3f} — "
                    f"market pricing is unfavorable (ensemble={ensemble.final_probability:.3f}, "
                    f"market={market.yes_price:.3f})"
                )
            elif abs(edge) < 0.001:
                logger.debug(
                    f"Edge rejection (zero): {market.ticker} edge={edge:+.3f} — "
                    f"market price matches ensemble estimate"
                )
            else:
                logger.debug(
                    f"Edge rejection (below threshold): {market.ticker} edge={abs(edge):.3f} "
                    f"< min_edge={min_edge:.3f} — marginal opportunity, insufficient edge "
                    f"(ensemble={ensemble.final_probability:.3f}, market={market.yes_price:.3f})"
                )
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

        # Smart Model Escalation: if edge > 15% and initial model was sonnet,
        # re-verify with opus. Opus catches ~10-20% more nuances in resolution
        # criteria and temporal reasoning. If opus disagrees > 10%, reject.
        model_used = getattr(forecast, "model_used", "") or ""
        is_sonnet = "sonnet" in model_used.lower()
        if edge > 0.15 and is_sonnet:
            try:
                opus_model = self.settings.claude.model_highstakes
                opus_forecast = await self.forecaster.assess_market(
                    market=market,
                    news_context=news_context,
                    base_rate_context=base_rate_context,
                    force_model=opus_model,
                    accuracy_context=accuracy_context,
                )
                if not getattr(opus_forecast, "parse_failed", False):
                    opus_prob = opus_forecast.probability
                    sonnet_prob = forecast.probability
                    disagreement = abs(opus_prob - sonnet_prob)
                    if disagreement > 0.10:
                        logger.warning(
                            f"Opus escalation REJECTED {market.ticker}: "
                            f"sonnet={sonnet_prob:.0%}, opus={opus_prob:.0%}, "
                            f"disagreement={disagreement:.0%} > 10% threshold"
                        )
                        return None
                    else:
                        logger.info(
                            f"Opus escalation CONFIRMED {market.ticker}: "
                            f"sonnet={sonnet_prob:.0%}, opus={opus_prob:.0%}, "
                            f"disagreement={disagreement:.0%}"
                        )
                        # Re-run ensemble with opus forecast so edge/probability
                        # reflect the high-stakes model's estimate (not sonnet's).
                        forecast = opus_forecast
                        if consensus_forecasts:
                            ensemble = multi_model_ensemble(
                                forecasts=[opus_forecast] + consensus_forecasts,
                                market_price=market.yes_price,
                                market_weight=1.0 - self.settings.claude.ensemble_weight,
                                market_efficiency=market_eff,
                                category=category.value,
                                category_brier_scores=cat_brier_dict,
                            )
                        else:
                            ensemble = ensemble_forecast(
                                claude_forecast=opus_forecast,
                                market_price=market.yes_price,
                                claude_weight=self.settings.claude.ensemble_weight,
                                market_efficiency=market_eff,
                            )
                        # Recalculate edge from new ensemble
                        edge = ensemble.edge
                        if edge > 0:
                            direction = Direction.BUY_YES
                            probability_estimate = ensemble.final_probability
                            market_price = market.yes_price
                        else:
                            direction = Direction.BUY_NO
                            probability_estimate = 1.0 - ensemble.final_probability
                            market_price = market.no_price
                            edge = abs(edge)
                        logger.info(
                            f"Opus re-ensemble for {market.ticker}: "
                            f"edge={edge:.1%}, prob={ensemble.final_probability:.0%}"
                        )
            except Exception as e:
                logger.warning(f"Opus escalation failed for {market.ticker}: {e}")
                # Continue with sonnet signal on escalation failure

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
