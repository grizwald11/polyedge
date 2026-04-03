"""Prompt construction for Claude forecaster.

Handles model/temperature selection, news enrichment, resolution criteria
validation, and final prompt assembly. Extracted from claude_forecaster.py
for modularity (M-1 audit item).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.analysis.prompt_ab_testing import PromptVariantManager
from src.analysis.prompt_templates import build_prompt
from src.core.models import Market, MarketCategory

logger = logging.getLogger(__name__)


def validate_resolution_criteria(description: str) -> str:
    """Validate and enhance resolution criteria if missing or too short."""
    if not description or len(description.strip()) < 20:
        logger.warning("Resolution criteria missing or too short — adding caution")
        caution = (
            "WARNING: No detailed resolution criteria available for this market. "
            "Resolution rules may be ambiguous. Widen your confidence interval "
            "to account for possible resolution surprises."
        )
        if description and description.strip():
            return f"{description.strip()}\n\n{caution}"
        return caution
    return description


def select_model(
    position_value: float,
    edge: float,
    *,
    highstakes_threshold: float,
    model_highstakes: str,
    model_primary: str,
    edge_highstakes_threshold: float = 0.15,
) -> str:
    """Select model based on position value or edge size.

    Uses opus for high-stakes positions or large detected edges,
    since opus catches more nuances in resolution criteria and temporal reasoning.
    """
    if position_value > highstakes_threshold:
        return model_highstakes
    if abs(edge) > edge_highstakes_threshold:
        return model_highstakes
    return model_primary


def select_temperature(
    category: MarketCategory,
    category_temperatures: dict,
    default_temperature: float,
) -> float:
    """Select temperature based on market category, falling back to default."""
    temp = category_temperatures.get(category.value)
    if temp is None:
        logger.debug(f"Using default temperature for unmapped category {category.value}")
        return default_temperature
    return temp


async def build_forecaster_prompt(
    market: Market,
    news_context: str,
    base_rate_context: str,
    position_value: float,
    news_researcher: NewsResearcher,
    variant_manager: PromptVariantManager,
    *,
    highstakes_threshold: float,
    model_highstakes: str,
    model_primary: str,
    edge_highstakes_threshold: float,
    category_temperatures: dict,
    default_temperature: float,
    accuracy_context: str = "",
) -> tuple[str, str, MarketCategory, float, str]:
    """Build the Claude prompt with news enrichment and context.

    Returns:
        Tuple of (prompt, model, category, temperature, variant_name)
    """
    model = select_model(
        position_value,
        0.0,
        highstakes_threshold=highstakes_threshold,
        model_highstakes=model_highstakes,
        model_primary=model_primary,
        edge_highstakes_threshold=edge_highstakes_threshold,
    )
    category = classify_market(market)
    temperature = select_temperature(category, category_temperatures, default_temperature)

    # Enrich with news research if no context was provided
    if not news_context:
        news_context = await news_researcher.get_context(market.question)
        if news_context:
            logger.info(
                f"News research found context for '{market.question[:50]}...'"
            )
        else:
            logger.debug(f"No news context for '{market.question[:50]}...'")

    # Build the prompt
    close_date = ""
    if market.end_date:
        close_date = market.end_date.strftime("%Y-%m-%d %H:%M UTC")

    resolution_criteria = validate_resolution_criteria(market.description)

    # Compute temporal context
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days_str = str(int(market.days_to_resolution)) if market.days_to_resolution is not None else "Unknown"

    prompt = build_prompt(
        question=market.question,
        resolution_criteria=resolution_criteria,
        market_price=market.yes_price,
        close_date=close_date,
        category=category,
        news_context=news_context or "No additional context available.",
        base_rate_context=base_rate_context,
        accuracy_context=accuracy_context,
        current_date=today,
        days_to_resolution=days_str,
    )

    # Apply A/B testing variant modifier to the prompt
    variant_name, prompt = variant_manager.select_variant(category, prompt)

    return prompt, model, category, temperature, variant_name
