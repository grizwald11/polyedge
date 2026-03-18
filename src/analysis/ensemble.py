"""Ensemble forecasting — combines multiple probability estimates.

Phase 2: Simple extremal adjustment between Claude's estimate and market price.
Phase 8 upgrade: Add second model, Brier-score-weighted average.
"""

from __future__ import annotations

from src.core.models import ForecastResult, EnsembleForecast


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
    market_weight = 1.0 - claude_weight

    # Weighted average
    final_prob = (
        claude_forecast.probability * claude_weight
        + market_price * market_weight
    )

    # Clamp
    final_prob = max(0.01, min(0.99, final_prob))

    edge = final_prob - market_price

    # Confidence is reduced when Claude and market disagree significantly
    disagreement = abs(claude_forecast.probability - market_price)
    confidence = max(0.1, 1.0 - disagreement)

    return EnsembleForecast(
        final_probability=final_prob,
        individual_forecasts=[claude_forecast],
        market_price=market_price,
        edge=edge,
        confidence=confidence,
    )
