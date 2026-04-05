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
from src.analysis.platt_calibrator import PlattCalibrator
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
    # L-7: These thresholds are initial estimates. Recalibrate empirically
    # after 100+ resolved predictions per category.
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
        self.platt_calibrator: Optional[PlattCalibrator] = None
        self._init_platt_calibrator()
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
        self.orderbook_analyzer = None  # Set externally if available
        self._category_base_rates: dict[str, dict] = {}
        self._category_brier_scores: dict[str, float] = {}

    def _fallback_from_cached_predictions(
        self, markets: list[Market], min_edge: float,
    ) -> list[Signal]:
        """Generate signals from recent unresolved predictions when Claude is down.

        Looks up predictions from the calibration_records table that:
        - Are unresolved (no actual_outcome yet)
        - Were made recently (within reassessment_interval_hours * 3)
        - Still show edge vs the current market price

        This keeps some capital working during brief Claude API outages
        instead of going completely idle.
        """
        if not self.db:
            return []

        signals: list[Signal] = []
        market_by_ticker = {m.ticker: m for m in markets}
        reassess_hours = self.settings.claude.reassessment_interval_hours

        try:
            recent_predictions = self.db.get_unresolved_predictions()
        except Exception as e:
            logger.error(f"Fallback prediction lookup failed: {e}")
            return []

        for pred in recent_predictions:
            market_id = pred.get("market_id")
            market = market_by_ticker.get(market_id)
            if market is None:
                continue

            predicted_prob = pred.get("predicted_probability", 0)
            if predicted_prob <= 0 or predicted_prob >= 1:
                continue

            # Check staleness — skip predictions older than 3x reassessment interval
            predicted_at = pred.get("predicted_at", "")
            if predicted_at:
                from datetime import datetime, timedelta, timezone
                try:
                    pred_time = datetime.fromisoformat(predicted_at)
                    max_age = timedelta(hours=reassess_hours * 3)
                    if datetime.now(timezone.utc) - pred_time > max_age:
                        continue
                except (ValueError, TypeError):
                    continue

            # Compute edge against current market price
            edge = predicted_prob - market.yes_price
            if abs(edge) < min_edge:
                continue

            direction = Direction.BUY_YES if edge > 0 else Direction.BUY_NO
            signals.append(Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id=market_id,
                market_question=market.question,
                direction=direction,
                edge=abs(edge),
                probability_estimate=predicted_prob,
                market_price=market.yes_price,
                confidence=0.60,  # Lower confidence for cached predictions
                reasoning=f"Fallback: cached prediction from {predicted_at[:16]} (Claude API down)",
            ))

        if signals:
            logger.info(
                f"Claude fallback: generated {len(signals)} signals from "
                f"{len(recent_predictions)} cached predictions"
            )
        return signals

    def set_regime_edge_multiplier(self, multiplier: float) -> None:
        """Set the regime-based edge multiplier.

        Called each cycle by regime detector. In high-volatility regimes,
        increases the effective min_edge to avoid false signals.
        """
        self._regime_edge_multiplier = max(0.5, min(multiplier, 3.0))

    def _init_platt_calibrator(self) -> None:
        """Initialize Platt calibrator from historical calibration records."""
        if not self.db:
            return
        try:
            records = self.db.get_resolved_calibration_records()
            if not records:
                return
            predictions = [r["predicted_probability"] for r in records]
            outcomes = [1 if r["actual_outcome"] == "Yes" else 0 for r in records]
            self.platt_calibrator = PlattCalibrator()
            params = self.platt_calibrator.fit(predictions, outcomes)
            if self.platt_calibrator.is_active:
                logger.info(
                    f"Platt calibrator active: a={params.a:.3f}, b={params.b:.3f}, "
                    f"Brier {params.brier_before:.4f} → {params.brier_after:.4f}"
                )
        except Exception as e:
            logger.debug(f"Platt calibrator init failed: {e}")
            self.platt_calibrator = None

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

        # Graceful degradation: if Claude API circuit breaker is open,
        # fall back to recent unresolved predictions from the DB that
        # still show edge vs current market prices.
        if self.forecaster.is_circuit_open():
            logger.warning(
                "Claude API circuit breaker open — using cached prediction fallback"
            )
            return self._fallback_from_cached_predictions(markets, min_edge)

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
            # M-10: Cross-check MEDIUM-edge signals — these sit in the zone where
            # the edge is large enough to matter but uncertain enough to benefit
            # from dual-temperature validation. Auto-pass the highest-edge signals
            # (most likely real) and skip the lowest-edge ones (barely tradeable).
            initial_signals.sort(key=lambda s: abs(s.edge), reverse=True)
            n_signals = len(initial_signals)
            if n_signals > cross_check_top_n:
                # Skip the top tier (high edge, auto-pass) and bottom tier (low edge, skip).
                # Cross-check the middle band.
                top_cutoff = max(1, n_signals // 3)  # top third auto-passes
                bottom_cutoff = max(top_cutoff + 1, n_signals - n_signals // 3)  # bottom third skipped
                auto_pass = initial_signals[:top_cutoff] + initial_signals[bottom_cutoff:]
                cross_check_candidates = initial_signals[top_cutoff:bottom_cutoff][:cross_check_top_n]
            else:
                auto_pass = []
                cross_check_candidates = initial_signals

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

        Orchestrates these stages via helper methods:
        1. Staleness check — skip if recent prediction is still fresh
        2. Classification — determine market category, apply accuracy gating
        3. Context gathering — build base rate, accuracy, resolution contexts
        4. Forecasting — get Claude's probability estimate (with optional cross-check)
        5. Divergence & confidence gates — reject hallucinations and wide CIs
        6. Ensemble — combine Claude + community forecasts + calibration adjustments
        7. Edge calculation — compute edge, check significance and minimum threshold
        8. Signal generation — build and return Signal if edge is sufficient
        """
        # Cheap contract filter: contracts under 12¢ are structural losers.
        # Research on 300K+ Kalshi contracts shows <10¢ contracts lose 60%+.
        # Applied before Claude API call to save tokens.
        if market.yes_price < 0.12 and market.no_price < 0.12:
            logger.debug(
                f"Cheap contract rejection: {market.ticker} both sides under 12¢ "
                f"(YES={market.yes_price:.0%}, NO={market.no_price:.0%})"
            )
            return None

        # 1. Staleness check
        if self._check_staleness(market):
            return None

        # 2. Classification and accuracy gating
        classify_result = self._classify_and_gate(market, min_edge)
        if classify_result is None:
            return None
        category, min_edge = classify_result

        # 3. Gather context (base rate, accuracy, resolution, analogues, news)
        context = await self._gather_context(market, category, news_context)
        if context is None:
            return None  # Resolution criteria too ambiguous

        # 4. Run forecast (decomposition, cross-check, or regular)
        forecast = await self._run_forecast(
            market, context, use_cross_check,
        )
        if forecast is None:
            return None

        # 4b. Pre-mortem adversarial analysis: force counterargument reasoning
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

        # 5. Divergence and confidence gates
        if not self._apply_divergence_gates(market, forecast, category):
            return None

        # 6. Ensemble: combine Claude + community forecasts + calibration adjustments
        ci_width = forecast.confidence_high - forecast.confidence_low
        ensemble, consensus_forecasts, market_eff, cat_brier_dict = await self._compute_ensemble(
            market, forecast, category,
        )

        # H-10: ensemble is None when post-ensemble validation fails
        if ensemble is None:
            return None

        # 7. Edge calculation, significance check, and signal generation
        return await self._calculate_edge_and_signal(
            market=market,
            forecast=forecast,
            ensemble=ensemble,
            category=category,
            ci_width=ci_width,
            min_edge=min_edge,
            news_context=context["news_context"],
            base_rate_context=context["base_rate_context"],
            accuracy_context=context["accuracy_context"],
            consensus_forecasts=consensus_forecasts,
            market_eff=market_eff,
            cat_brier_dict=cat_brier_dict,
        )

    # ------------------------------------------------------------------
    # Stage 1: Staleness check
    # ------------------------------------------------------------------

    def _check_staleness(self, market: Market) -> bool:
        """Return True if market should be skipped due to a recent fresh prediction."""
        if not self.db:
            return False
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
                    # Also check if volume changed significantly — a volume spike
                    # can signal new information even without a price move
                    volume_changed = False
                    cached_volume = latest.get("volume_at_prediction")
                    if cached_volume and cached_volume > 0 and market.volume_24h > 0:
                        volume_ratio = market.volume_24h / cached_volume
                        volume_changed = volume_ratio > 1.5 or volume_ratio < 0.5
                    if not volume_changed:
                        logger.debug(
                            f"Skipping {market.ticker}: recent prediction "
                            f"({age.total_seconds()/3600:.0f}h old, price moved {price_move:.2f} "
                            f"({relative_move:.0%} relative), volume stable)"
                        )
                        return True
        except Exception as e:
            logger.debug(f"Staleness check failed for {market.ticker}: {e}")
        return False

    # ------------------------------------------------------------------
    # Stage 2: Classification and accuracy gating
    # ------------------------------------------------------------------

    def _classify_and_gate(self, market: Market, min_edge: float) -> Optional[tuple]:
        """Classify market and apply accuracy gating.

        Returns (category, min_edge) on success, or None to skip this market.
        """
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

        return category, min_edge

    # ------------------------------------------------------------------
    # Stage 3: Context gathering
    # ------------------------------------------------------------------

    async def _gather_context(
        self,
        market: Market,
        category,
        news_context: str,
    ) -> Optional[dict]:
        """Gather all context needed for forecasting.

        Returns a dict with keys: base_rate_context, accuracy_context, news_context.
        Returns None if the market should be skipped (e.g. ambiguous resolution).
        """
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

        return {
            "base_rate_context": base_rate_context,
            "accuracy_context": accuracy_context,
            "news_context": news_context,
        }

    # ------------------------------------------------------------------
    # Stage 4: Forecasting
    # ------------------------------------------------------------------

    async def _run_forecast(
        self,
        market: Market,
        context: dict,
        use_cross_check: bool,
    ) -> Optional[ForecastResult]:
        """Run Claude forecast (decomposition, cross-check, or regular).

        Returns ForecastResult on success, or None to skip this market.
        """
        news_context = context["news_context"]
        base_rate_context = context["base_rate_context"]
        accuracy_context = context["accuracy_context"]

        # Try decomposition for compound questions (multi-step reasoning)
        forecast: Optional[ForecastResult] = None
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
            except Exception as e:
                logger.warning(f"Decomposition failed for {market.ticker}: {e}")

        # Get Claude's forecast (cross-check or regular) — skip if decomposition succeeded
        if forecast is not None:
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

        # Apply Platt scaling to correct systematic calibration bias
        if self.platt_calibrator and self.platt_calibrator.is_active:
            raw_prob = forecast.probability
            forecast.probability = self.platt_calibrator.calibrate(raw_prob)
            if abs(forecast.probability - raw_prob) > 0.01:
                logger.debug(
                    f"Platt calibration: {market.ticker} {raw_prob:.3f} → {forecast.probability:.3f}"
                )

        return forecast

    # ------------------------------------------------------------------
    # Stage 5: Divergence and confidence gates
    # ------------------------------------------------------------------

    def _apply_divergence_gates(self, market: Market, forecast: ForecastResult, category) -> bool:
        """Apply divergence and confidence gates. Return True if forecast passes all gates."""
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
                return False
        if divergence > max_div:
            logger.warning(
                f"Rejecting {market.ticker}: Claude ({forecast.probability:.0%}) diverges "
                f"{divergence:.0%} from market ({market.yes_price:.0%}) — exceeds max {max_div:.0%}"
            )
            return False

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
            return False

        return True

    # ------------------------------------------------------------------
    # Stage 6: Ensemble computation
    # ------------------------------------------------------------------

    async def _compute_ensemble(
        self,
        market: Market,
        forecast: ForecastResult,
        category,
    ) -> tuple:
        """Combine Claude + community forecasts + calibration adjustments.

        Returns (ensemble, consensus_forecasts, market_eff, cat_brier_dict).
        """
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

        # C-3 FIX: Post-adjustment divergence cap. Track raw Claude probability
        # before any adjustments so we can clamp total drift from compounding
        # Platt + bias + consensus + base rate adjustments.
        raw_claude_prob = forecast.probability

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

        # C-3 FIX: Clamp total adjustment drift. If the final ensemble probability
        # has drifted more than 15% from the raw Claude estimate, the compounding
        # adjustments are likely amplifying noise rather than correcting bias.
        MAX_ADJUSTMENT_DRIFT = 0.15
        total_drift = ensemble.final_probability - raw_claude_prob
        if abs(total_drift) > MAX_ADJUSTMENT_DRIFT:
            clamped_prob = raw_claude_prob + (MAX_ADJUSTMENT_DRIFT if total_drift > 0 else -MAX_ADJUSTMENT_DRIFT)
            clamped_prob = max(0.01, min(0.99, clamped_prob))
            logger.warning(
                f"C-3 divergence cap: {market.ticker} total drift {total_drift:+.3f} "
                f"exceeds {MAX_ADJUSTMENT_DRIFT:.0%} — clamping from "
                f"{ensemble.final_probability:.3f} to {clamped_prob:.3f} "
                f"(raw Claude={raw_claude_prob:.3f})"
            )
            from src.core.models import EnsembleForecast as EnsembleForecastModel
            ensemble = EnsembleForecastModel(
                final_probability=clamped_prob,
                individual_forecasts=ensemble.individual_forecasts,
                market_price=ensemble.market_price,
                edge=clamped_prob - market.yes_price,
                confidence=ensemble.confidence,
            )

        # H-10: Post-ensemble reasonableness validation. Reject non-finite or
        # out-of-range probabilities before they propagate to edge calculation
        # and signal generation. This guards against NaN/inf from numerical
        # instabilities in Platt calibration, extremization, or log-odds math.
        import math as _math
        final_p = ensemble.final_probability
        if not _math.isfinite(final_p) or final_p < 0.01 or final_p > 0.99:
            logger.warning(
                f"H-10 reasonableness rejection: {market.ticker} ensemble probability "
                f"{final_p} is non-finite or outside [0.01, 0.99] — skipping"
            )
            return None, consensus_forecasts, market_eff, cat_brier_dict

        return ensemble, consensus_forecasts, market_eff, cat_brier_dict

    # ------------------------------------------------------------------
    # Stage 7: Edge calculation and signal generation
    # ------------------------------------------------------------------

    async def _calculate_edge_and_signal(
        self,
        market: Market,
        forecast: ForecastResult,
        ensemble,
        category,
        ci_width: float,
        min_edge: float,
        news_context: str,
        base_rate_context: str,
        accuracy_context: str,
        consensus_forecasts: list,
        market_eff: float,
        cat_brier_dict: Optional[dict],
    ) -> Optional[Signal]:
        """Calculate edge, apply temporal/significance checks, and build Signal if sufficient."""
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

        # Uncertain zone: markets priced 30-70% are hardest to predict.
        # Require 50% higher edge to trade these.
        effective_min_edge = min_edge
        if 0.30 <= market.yes_price <= 0.70:
            effective_min_edge = min_edge * 1.5

        if abs(edge) < effective_min_edge:
            # H-13: Log distinct reasons for edge rejection
            if edge < 0 and abs(edge) < effective_min_edge:
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
                    f"< min_edge={effective_min_edge:.3f} — marginal opportunity, insufficient edge "
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

        # Cheap contract filter (direction-aware): reject buying contracts under 12¢.
        # Research: contracts under 10¢ lose 60%+ of invested capital on average.
        if market_price < 0.12:
            logger.debug(
                f"Cheap contract rejection: {market.ticker} {direction.value} "
                f"at {market_price:.0%} — structural loser under 12¢"
            )
            return None

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

        # Apply orderbook imbalance signal if analyzer is available
        ob_confidence = ensemble.confidence
        if self.orderbook_analyzer is not None:
            try:
                from src.data.orderbook_analyzer import apply_orderbook_signal
                orderbook = getattr(market, '_orderbook', None)
                if orderbook:
                    analysis = self.orderbook_analyzer(orderbook, "yes" if direction == Direction.BUY_YES else "no")
                    edge, ob_confidence = apply_orderbook_signal(edge, ensemble.confidence, analysis, direction)
            except Exception as e:
                logger.debug(f"Orderbook signal skipped for {market.ticker}: {e}")

        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=edge,
            probability_estimate=probability_estimate,
            market_price=market_price,
            confidence=ob_confidence,
            reasoning=forecast.reasoning,
        )

        logger.info(
            f"Signal: {direction.value} on '{market.question[:50]}...' "
            f"(edge={edge:.1%}, claude={forecast.probability:.0%}, "
            f"market={market.yes_price:.0%})"
        )

        return signal
