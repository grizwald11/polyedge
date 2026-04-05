"""Ensemble forecasting — combines multiple probability estimates.

Supports two modes:
1. Single-model: Claude + market price extremal adjustment (original)
2. Multi-model: Multiple forecasts weighted by Brier score performance

The ensemble applies Brier-score-weighted averaging when historical accuracy
data is available, falling back to equal weights otherwise. Market price is
always included as an additional "forecast" with configurable weight.

When both Claude and GPT-4o forecasts are available, uses Brier-score-weighted
averaging with a 20% minimum weight floor for either model. When models disagree
by >15 percentage points, logs a warning and the disagreement_pct field is set
on the EnsembleForecast so downstream sizing can reduce position by 50%.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from src.core.models import EnsembleForecast, ForecastResult

logger = logging.getLogger(__name__)

# Default extremization factor. Research shows extremizing aggregated
# forecasts by 15-25% improves calibration because simple averaging pulls
# too far toward 50%. A factor of 1.15 = 15% extremization (conservative).
DEFAULT_EXTREMIZE_FACTOR = 1.15


def extremize(probability: float, factor: float = DEFAULT_EXTREMIZE_FACTOR) -> float:
    """Push probability away from 50% toward 0 or 1 using log-odds scaling.

    Research: extremizing aggregated forecasts improves calibration
    because averaging pulls too far toward 50%.

    Args:
        probability: Raw probability (0-1)
        factor: Extremization strength. 1.0 = no change, 1.2 = 20% extremization.

    Returns:
        Extremized probability, clamped to [0.01, 0.99]
    """
    if probability <= 0.01 or probability >= 0.99:
        return probability
    log_odds = math.log(probability / (1 - probability))
    extremized_odds = log_odds * factor
    result = 1 / (1 + math.exp(-extremized_odds))
    return max(0.01, min(0.99, result))


def apply_overconfidence_shrinkage(
    prob: float, ci_width: float, shrinkage: float = 0.05,
) -> float:
    """M-3: Shrink extreme, narrow-CI forecasts toward 0.5.

    Claude is systematically overconfident by 5-15% on extreme predictions.
    When the confidence interval is narrow (high confidence) AND the
    probability is extreme, apply conservative shrinkage toward 0.5.

    Args:
        prob: Probability estimate (0-1)
        ci_width: Width of confidence interval (confidence_high - confidence_low)
        shrinkage: Shrinkage factor toward 0.5 (default 5%)

    Returns:
        Adjusted probability, shrunk toward 0.5 if conditions met.
    """
    if (prob > 0.93 or prob < 0.07) and ci_width < 0.15:
        return prob * (1 - shrinkage) + 0.5 * shrinkage
    return prob


@dataclass
class ModelWeight:
    """A forecast source with its weight."""

    name: str
    forecast: ForecastResult
    weight: float = 1.0
    brier_score: Optional[float] = None  # Historical performance (lower = better)


def compute_market_efficiency(
    volume_24h: float = 0,
    liquidity: float = 0,
    days_to_resolution: Optional[float] = None,
) -> float:
    """Estimate market efficiency from observable liquidity proxies.

    Higher efficiency means the market price is more informative and should
    get more weight in the ensemble. Used by ensemble_forecast and
    BayesianUpdater to dynamically adjust trust in market price.

    Args:
        volume_24h: 24-hour trading volume in dollars
        liquidity: Current order book depth in dollars
        days_to_resolution: Days until market resolves (near-expiry = more efficient)

    Returns:
        Efficiency score from 0.3 (thin/inefficient) to 0.95 (deep/efficient)
    """
    # Volume score: log-scaled, saturates around $1M
    vol_score = min(1.0, math.log1p(max(0, volume_24h)) / math.log1p(1_000_000))

    # Liquidity score: order book depth, saturates at $100K
    liq_score = min(1.0, math.log1p(max(0, liquidity)) / math.log1p(100_000))

    # Time-to-resolution: markets near expiry are heavily arbitraged
    time_score = 0.5
    if days_to_resolution is not None:
        if days_to_resolution < 3:
            time_score = 0.9
        elif days_to_resolution < 14:
            time_score = 0.7
        else:
            time_score = 0.5

    efficiency = 0.4 * vol_score + 0.3 * liq_score + 0.3 * time_score
    return max(0.3, min(0.95, efficiency))


def ensemble_forecast(
    claude_forecast: ForecastResult,
    market_price: float,
    claude_weight: float = 0.85,
    market_efficiency: Optional[float] = None,
    extremize_factor: float = DEFAULT_EXTREMIZE_FACTOR,
) -> EnsembleForecast:
    """Combine Claude forecast with market price using adaptive weights.

    The ensemble applies a weighted average biased toward Claude but pulled
    toward the market price for humility. This prevents overconfidence
    when Claude disagrees significantly with the market.

    Args:
        claude_forecast: Claude's probability assessment
        market_price: Current market YES price (0-1)
        claude_weight: Weight given to Claude (0-1), remainder to market

    Returns:
        EnsembleForecast with final combined probability
    """
    # Adaptive weighting based on CI width AND divergence from market.
    # Use abs() so inverted bounds still produce a meaningful CI penalty
    # rather than silently zeroing out.
    ci_width = abs(claude_forecast.confidence_high - claude_forecast.confidence_low)
    ci_penalty = min(1.0, max(0.0, ci_width))
    effective_claude_weight = claude_weight * (1.0 - ci_penalty * 0.5)

    # Divergence-based adjustment: when Claude strongly disagrees with market,
    # the market likely hasn't repriced — trust Claude more. When marginal
    # disagreement, be more humble.
    # For extreme-price markets (<5¢ or >95¢), REDUCE Claude's weight rather
    # than boosting it — very cheap/expensive contracts are noisy and Claude's
    # divergence is more likely a hallucination than genuine edge.
    # Threshold at 5%/95% (not 15%/85%) to avoid filtering out legitimate
    # mid-rare opportunities like FDA approvals at 10-15%.
    divergence = abs(claude_forecast.probability - market_price)
    # M-5 FIX: Tightened from 0.05/0.95 to 0.02/0.98 — 5c markets still have
    # tradeable volume and shouldn't automatically discount Claude's view.
    extreme_price = market_price < 0.02 or market_price > 0.98

    # Dynamic efficiency adjustment: when market is highly efficient (>0.7),
    # trust market price more on divergence; when inefficient (<0.5),
    # trust Claude more. Falls back to original hardcoded behavior when
    # efficiency is not provided.
    eff = market_efficiency if market_efficiency is not None else 0.7

    # H-8: Scale Claude weight by market efficiency. More efficient markets
    # (higher liquidity/volume) deserve more market weight. Scale factor
    # ranges from 1.0 (eff=0.3, least efficient) to ~0.87 (eff=0.95, most
    # efficient), reducing Claude's weight for well-traded markets.
    efficiency_scale = 1.0 - (eff - 0.3) * 0.2  # 1.0 at eff=0.3, 0.87 at eff=0.95
    efficiency_scale = max(0.80, min(1.0, efficiency_scale))
    effective_claude_weight *= efficiency_scale

    if extreme_price:
        # On extreme-price markets, trust the market more — Claude divergence
        # here is usually wrong. Reduce Claude weight with floor at 25% (M-1).
        effective_claude_weight = max(0.25, effective_claude_weight - divergence * 0.5)
    elif divergence > 0.20:
        # High divergence: hold steady. Large disagreements are ambiguous —
        # could be genuine edge OR hallucination. Don't amplify either way.
        # The divergence gate in ai_probability.py handles extreme cases.
        pass
    elif divergence < 0.05:
        # Marginal call: trust market more when it's efficient
        reduction = 0.10 * (0.5 + eff)  # 0.12 at eff=0.7, 0.08 at eff=0.3
        effective_claude_weight = max(0.50, effective_claude_weight - reduction)

    market_weight = 1.0 - effective_claude_weight

    # Weighted average
    final_prob = (
        claude_forecast.probability * effective_claude_weight
        + market_price * market_weight
    )

    # Extremize: push away from 50% to correct for averaging regression
    final_prob = extremize(final_prob, factor=extremize_factor)

    # M-3: Overconfidence shrinkage for extreme forecasts with narrow CI.
    # Claude systematically overconfident by 5-15% on extreme predictions.
    # When CI is narrow (high confidence) AND probability is extreme,
    # apply conservative 5% shrinkage toward 0.5.
    final_prob = apply_overconfidence_shrinkage(final_prob, ci_width)

    # Clamp
    final_prob = max(0.01, min(0.99, final_prob))

    edge = final_prob - market_price

    # Confidence based on Claude's CI width (narrow CI = high confidence)
    confidence = max(0.1, min(0.95, 1.0 - ci_width))

    return EnsembleForecast(
        final_probability=final_prob,
        individual_forecasts=[claude_forecast],
        market_price=market_price,
        edge=edge,
        confidence=confidence,
    )


def multi_model_ensemble(
    forecasts: list[ForecastResult],
    market_price: float,
    market_weight: float = 0.40,
    brier_scores: Optional[dict[str, float]] = None,
    category: str = "",
    category_brier_scores: Optional[dict[str, dict[str, float]]] = None,
    market_efficiency: Optional[float] = None,
    extremize_factor: float = DEFAULT_EXTREMIZE_FACTOR,
) -> EnsembleForecast:
    """Combine multiple model forecasts using Brier-score-weighted averaging.

    When Brier scores are available, models with better historical accuracy
    get higher weights. The market price is included as an additional source
    with a fixed weight, scaled by market efficiency.

    Args:
        forecasts: List of ForecastResult from different models/approaches
        market_price: Current market YES price
        market_weight: Fixed weight for market price (0-1)
        brier_scores: Optional {model_name: brier_score} for global weighting
        category: Market category for category-specific weighting
        category_brier_scores: Optional {category: {model_name: brier_score}}
        market_efficiency: Market efficiency score (0.3-0.95); higher = trust market more

    Returns:
        EnsembleForecast with Brier-weighted combined probability
    """
    if not forecasts:
        logger.warning(
            "multi_model_ensemble called with no forecasts — "
            "returning market price as fallback (no edge, minimal confidence)"
        )
        return EnsembleForecast(
            final_probability=max(0.01, min(0.99, market_price)),
            market_price=market_price,
            edge=0.0,
            confidence=0.01,
        )

    # Adjust market_weight based on efficiency: efficient markets deserve more weight
    if market_efficiency is not None:
        # Scale market_weight by efficiency: at eff=0.95 → 1.2x, at eff=0.3 → 0.6x
        eff_scale = 0.4 + market_efficiency
        market_weight = max(0.10, min(0.70, market_weight * eff_scale))

    # Build model weights
    model_weights = _compute_model_weights(
        forecasts, brier_scores, category, category_brier_scores,
    )

    # Scale model weights to fill (1 - market_weight)
    model_share = 1.0 - market_weight
    total_raw = sum(mw.weight for mw in model_weights)
    if total_raw > 0:
        for mw in model_weights:
            mw.weight = (mw.weight / total_raw) * model_share

    # Weighted average
    final_prob = market_price * market_weight
    for mw in model_weights:
        final_prob += mw.forecast.probability * mw.weight

    # Extremize: push away from 50% to correct for averaging regression
    final_prob = extremize(final_prob, factor=extremize_factor)

    # M-3: Overconfidence shrinkage for extreme forecasts with narrow CI.
    # Claude systematically overconfident by 5-15% on extreme predictions.
    # When CI is narrow (high confidence) AND probability is extreme,
    # apply conservative 5% shrinkage toward 0.5.
    avg_ci_width = sum(abs(f.confidence_high - f.confidence_low) for f in forecasts) / len(forecasts)
    final_prob = apply_overconfidence_shrinkage(final_prob, avg_ci_width)

    final_prob = max(0.01, min(0.99, final_prob))
    edge = final_prob - market_price

    # Confidence: average of individual CIs, penalize disagreement
    ci_widths = [abs(f.confidence_high - f.confidence_low) for f in forecasts]
    avg_ci = sum(ci_widths) / len(ci_widths) if ci_widths else 0.5

    # Disagreement penalty: std dev of probability estimates
    probs = [f.probability for f in forecasts]
    mean_prob = sum(probs) / len(probs)
    variance = sum((p - mean_prob) ** 2 for p in probs) / len(probs)
    disagreement = variance ** 0.5  # std dev

    # Compute max pairwise disagreement between AI models for sizing adjustment.
    # When AI models (Claude vs GPT-4o) disagree by >15pp, downstream sizing
    # should reduce position by 50% to account for model uncertainty.
    ai_forecasts = [f for f in forecasts if _is_ai_model(f.model_used or "")]
    max_ai_disagreement = 0.0
    if len(ai_forecasts) >= 2:
        for i, fa in enumerate(ai_forecasts):
            for fb in ai_forecasts[i + 1:]:
                pairwise = abs(fa.probability - fb.probability)
                max_ai_disagreement = max(max_ai_disagreement, pairwise)
        if max_ai_disagreement > 0.15:
            logger.warning(
                f"AI model disagreement: {max_ai_disagreement:.0%} between "
                f"{[f.model_used for f in ai_forecasts]} — "
                f"recommend 50% position sizing reduction"
            )

    # Multiplicative penalty: disagreement scales down confidence rather than
    # subtracting a fixed amount, which was overly punitive (e.g., 0.15 std dev
    # would wipe 15pp of confidence).  A disagreement of 0.25 now reduces
    # confidence by ~25% instead of a flat 25pp subtraction.
    disagreement_factor = max(0.3, 1.0 - disagreement)
    confidence = max(0.1, min(0.95, (1.0 - avg_ci) * disagreement_factor))

    weight_strs = [f"{mw.name}={mw.weight:.2f}" for mw in model_weights]
    logger.debug(
        f"Ensemble: {len(forecasts)} models, market_w={market_weight:.2f}, "
        f"weights=[{', '.join(weight_strs)}], final={final_prob:.3f}, "
        f"disagreement={disagreement:.3f}"
    )

    return EnsembleForecast(
        final_probability=final_prob,
        individual_forecasts=forecasts,
        market_price=market_price,
        edge=edge,
        confidence=confidence,
        disagreement_pct=max_ai_disagreement,
    )


def _compute_model_weights(
    forecasts: list[ForecastResult],
    brier_scores: Optional[dict[str, float]],
    category: str,
    category_brier_scores: Optional[dict[str, dict[str, float]]],
) -> list[ModelWeight]:
    """Compute weights for each forecast based on Brier score performance.

    Uses category-specific Brier scores if available, falls back to global,
    then to equal weighting.

    Weight formula: w_i = (1 - brier_i) / sum(1 - brier_j)
    Lower Brier score → higher weight.
    """
    weights: list[ModelWeight] = []

    for forecast in forecasts:
        model_name = forecast.model_used or "unknown"
        brier: Optional[float] = None

        # Try category-specific Brier first
        if category and category_brier_scores:
            cat_scores = category_brier_scores.get(category, {})
            brier = cat_scores.get(model_name)

        # Fall back to global Brier
        if brier is None and brier_scores:
            brier = brier_scores.get(model_name)

        weights.append(ModelWeight(
            name=model_name,
            forecast=forecast,
            brier_score=brier,
        ))

    # Compute weights from Brier scores
    has_scores = [w for w in weights if w.brier_score is not None]

    if len(has_scores) >= 1:
        # Brier-weighted: w = (1 - brier) normalized.
        # Works with 1+ scored models (was >=2; lowered so a single well-
        # calibrated model still gets its earned weight advantage).
        raw = [(1.0 - max(0.0, min(1.0, w.brier_score))) for w in has_scores]
        total = sum(raw)
        if total > 0:
            for w, r in zip(has_scores, raw):
                w.weight = r / total
        else:
            # All models have worst-possible calibration (brier=1.0);
            # fall back to equal weighting rather than leaving weights at 1.0
            equal = 1.0 / len(has_scores)
            for w in has_scores:
                w.weight = equal
        # Models without scores get average of scored weights
        if has_scores:
            avg_weight = sum(w.weight for w in has_scores) / len(has_scores)
        else:
            avg_weight = 1.0 / len(weights) if weights else 1.0
        for w in weights:
            if w.brier_score is None:
                w.weight = avg_weight
    else:
        # Equal weights (no models have Brier scores yet)
        equal = 1.0 / len(weights) if weights else 1.0
        for w in weights:
            w.weight = equal

    # Apply confidence weighting: narrow CI → higher weight.
    # Multiplier ranges from 0.3 (very wide CI) to 1.0 (very narrow CI).
    for w in weights:
        ci_width = abs(w.forecast.confidence_high - w.forecast.confidence_low)
        confidence_factor = max(0.3, 1.0 - ci_width * 0.5)
        w.weight *= confidence_factor

    # Apply source strength weighting: high-participation sources → higher weight.
    # source_strength defaults to 1.0 for Claude forecasts, <1.0 for thin consensus.
    for w in weights:
        w.weight *= getattr(w.forecast, 'source_strength', 1.0)

    # Re-normalize weights
    total_weight = sum(w.weight for w in weights)
    if total_weight > 0:
        for w in weights:
            w.weight /= total_weight

    # Enforce 20% minimum weight floor for AI model diversity.
    # When exactly 2 AI forecasters are present (e.g., Claude + GPT-4o),
    # neither should drop below 20% weight to maintain ensemble diversity.
    MIN_AI_WEIGHT = 0.20
    ai_models = [w for w in weights if _is_ai_model(w.name)]
    if len(ai_models) == 2:
        for w in ai_models:
            if w.weight < MIN_AI_WEIGHT:
                deficit = MIN_AI_WEIGHT - w.weight
                w.weight = MIN_AI_WEIGHT
                # Take the deficit from the other AI model
                other = [m for m in ai_models if m is not w][0]
                other.weight = max(MIN_AI_WEIGHT, other.weight - deficit)
        # Re-normalize after floor enforcement
        total_weight = sum(w.weight for w in weights)
        if total_weight > 0:
            for w in weights:
                w.weight /= total_weight

    return weights


def _is_ai_model(model_name: str) -> bool:
    """Check if a model name corresponds to an AI forecaster (not community/market)."""
    ai_prefixes = ("claude", "gpt", "gemini", "openai")
    name_lower = model_name.lower()
    return any(name_lower.startswith(p) or p in name_lower for p in ai_prefixes)
