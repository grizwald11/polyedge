"""News-reactive strategy — maps breaking news to market impact signals.

Detects breaking news, matches to affected markets, uses Claude for rapid
impact assessment, generates signals when probability shift exceeds threshold.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.prompt_templates import NEWS_IMPACT_TEMPLATE
from src.config import Settings
from src.core.models import (
    Direction, Market, Signal, StrategyName,
)
from src.data.news_ingestion import NewsIngestion, NewsItem
from src.storage.database import Database

logger = logging.getLogger(__name__)


class NewsReactiveStrategy:
    """Generates trading signals from breaking news impact assessments."""

    def __init__(
        self,
        forecaster: ClaudeForecaster,
        news_ingestion: NewsIngestion,
        settings: Settings,
        db: Database,
    ):
        self.forecaster = forecaster
        self.news = news_ingestion
        self.settings = settings
        self.db = db
        self.min_edge = settings.trading.min_edge_ai

    async def scan_for_opportunities(
        self, markets: list[Market]
    ) -> list[Signal]:
        """Poll news feeds, match to markets, assess impact.

        Returns signals for markets where news shifts probability beyond threshold.
        """
        # Poll for new articles
        items = await self.news.poll_feeds()
        if not items:
            return []

        # Filter for relevant breaking news
        relevant = self.news.filter_relevant(items, markets)
        if not relevant:
            return []

        logger.info(f"News reactive: {len(relevant)} breaking items matched to markets")

        signals: list[Signal] = []
        market_lookup = {m.ticker: m for m in markets}

        for item, market_id in relevant[:5]:  # Limit Claude calls per cycle
            market = market_lookup.get(market_id)
            if market is None:
                continue

            signal = await self._assess_impact(item, market)
            if signal:
                signals.append(signal)

        return signals

    async def _assess_impact(
        self, item: NewsItem, market: Market
    ) -> Optional[Signal]:
        """Use Claude to assess a news item's impact on a market.

        Returns a Signal if the impact exceeds minimum edge, else None.
        """
        prompt = NEWS_IMPACT_TEMPLATE.format(
            headline=item.title,
            summary=item.summary,
            source=item.source,
            question=market.question,
            resolution_criteria=market.resolution_source or "Standard resolution rules apply.",
            market_price=market.yes_price,
        )

        try:
            result = await self.forecaster.assess_market_with_prompt(
                market=market,
                custom_prompt=prompt,
            )
        except Exception as e:
            logger.error(f"News impact assessment failed for {market.ticker}: {e}", exc_info=True)
            return None

        if result is None:
            return None

        # Calculate edge
        edge = result.probability - market.yes_price
        abs_edge = abs(edge)

        if abs_edge < self.min_edge:
            return None

        # Determine direction
        if edge > 0:
            direction = Direction.BUY_YES
            probability_estimate = result.probability
        else:
            direction = Direction.BUY_NO
            probability_estimate = 1.0 - result.probability

        # Use CI midpoint for confidence (higher CI width = lower confidence)
        ci_width = result.confidence_high - result.confidence_low
        confidence = max(0.1, min(0.95, 1.0 - ci_width))

        signal = Signal(
            strategy=StrategyName.NEWS_REACTIVE,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=abs_edge,
            probability_estimate=probability_estimate,
            market_price=market.yes_price if edge > 0 else market.no_price,
            confidence=confidence,
            reasoning=f"News: {item.title[:100]} | {result.reasoning[:200]}",
        )

        logger.info(
            f"News signal: {direction.value} {market.ticker} "
            f"edge={abs_edge:.1%} (news: {item.title[:50]}...)"
        )
        return signal
