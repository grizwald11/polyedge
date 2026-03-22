"""Ensemble forecasting — combines multiple probability estimates.

Supports two modes:
1. Single-model: Claude + market price extremal adjustment (original)
2. Multi-model: Multiple forecasts weighted by Brier score performance

The ensemble applies Brier-score-weighted averaging when historical accuracy
data is available, falling back to equal weights otherwise. Market price is
always included as an additional "forecast" with configurable weight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.core.models import ForecastResult, EnsembleForecast

logger = logging.getLogger(__name__)


@dataclass
class ModelWeight:
    """A forecast source with its weight."""

    name: str
    forecast: ForecastResult
    weight: float = 1.0
    brier_score: Optional[float] = None  # Historical performance (lower = better)


def ensemble_forecast(
    claude_forecast: ForecastResult,
    market_price: float,
    claude_weight: float = 0.85,
) -> EnsembleForecast:
    """Combine Claude's estimate with market price using extremal adjustment.

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
    # Adaptive weighting: when Claude's confidence interval is wide,
    # trust the market more. When Claude is very confident, trust Claude more.
    ci_width = abs(claude_forecast.confidence_high - claude_forecast.confidence_low)
    # Scale claude_weight down as CI widens: at CI=0 → full weight, at CI=1.0 → max reduction
    # Penalty scales linearly across the full 0-1 range (not capped at 0.5)
    ci_penalty = min(1.0, max(0.0, ci_width))  # 0 to 1 as CI goes from 0 to 1.0
    effective_claude_weight = claude_weight * (1.0 - ci_penalty * 0.5)  # At most 50% reduction
    market_weight = 1.0 - effective_claude_weight

    # Weighted average
    final_prob = (
        claude_forecast.probability * effective_claude_weight
        + market_price * market_weight
    )

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
    market_weight: float = 0.15,
    brier_scores: Optional[dict[str, float]] = None,
    category: str = "",
    category_brier_scores: Optional[dict[str, dict[str, float]]] = None,
) -> EnsembleForecast:
    """Combine multiple model forecasts with Brier-score-weighted averaging.

    When Brier scores are available, models with better historical accuracy
    get higher weights. The market price is included as an additional source
    with a fixed weight.

    Args:
        forecasts: List of ForecastResult from different models/approaches
        market_price: Current market YES price
        market_weight: Fixed weight for market price (0-1)
        brier_scores: Optional {model_name: brier_score} for global weighting
        category: Market category for category-specific weighting
        category_brier_scores: Optional {category: {model_name: brier_score}}

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

    confidence = max(0.1, min(0.95, 1.0 - avg_ci - disagreement))

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

    if len(has_scores) >= 2:
        # Brier-weighted: w = (1 - brier) normalized
        raw = [(1.0 - w.brier_score) for w in has_scores]
        total = sum(raw)
        if total > 0:
            for w, r in zip(has_scores, raw):
                w.weight = r / total
        # Models without scores get average of scored weights
        if has_scores:
            avg_weight = sum(w.weight for w in has_scores) / len(has_scores)
        else:
            avg_weight = 1.0 / len(weights) if weights else 1.0
        for w in weights:
            if w.brier_score is None:
                w.weight = avg_weight
    else:
        # Equal weights
        equal = 1.0 / len(weights) if weights else 1.0
        for w in weights:
            w.weight = equal

    return weights
