"""Market classifier — categorizes markets by type using keyword matching.

Uses the same keyword matching logic as market_discovery but operates
on Market model objects directly.
"""

from __future__ import annotations

from src.core.models import Market, MarketCategory
from src.core.market_discovery import classify_market_category


def classify_market(market: Market) -> MarketCategory:
    """Classify a market into a category.

    If the market already has a non-OTHER category, use it.
    Otherwise, run keyword classification on the question and tags.
    """
    if market.category != MarketCategory.OTHER:
        return market.category

    text_parts = [market.question]
    if market.subtitle:
        text_parts.append(market.subtitle)
    full_text = " ".join(text_parts)

    return classify_market_category(full_text, market.tags)
