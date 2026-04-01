"""Late Resolution Strategy — trade near-resolution markets where public info is clear.

Targets markets resolving within 6 hours where public information makes the
outcome >90% certain but the market is still priced at <80%. This is a speed
advantage: processing publicly available evidence faster than the crowd reprices.

Example: A market asks "Will the Senate vote on X today?" and the vote already
happened 30 minutes ago with a YES outcome, but the market is still at 72%.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import (
    Direction,
    Market,
    Signal,
    StrategyName,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Strategy parameters
MAX_HOURS_TO_RESOLUTION = 6
MIN_EVIDENCE_PROBABILITY = 0.90  # Need >90% certainty from evidence
MAX_MARKET_PRICE = 0.80          # Market must still show uncertainty (<80%)
MIN_VOLUME = 10_000              # $10K minimum 24h volume
MAX_CONCURRENT = 5               # Max markets to assess per scan cycle
CONFIDENCE_BASE = 0.85           # Higher base confidence — speed advantage on public info


class LateResolutionStrategy:
    """Trade markets nearing resolution where public info has already decided the outcome."""

    def __init__(
        self,
        settings,
        db: Database,
        news_researcher=None,
    ):
        self.settings = settings
        self.db = db
        self.news_researcher = news_researcher

    async def generate_signals(self, markets: list[Market]) -> list[Signal]:
        """Scan markets for late-resolution opportunities.

        Filters markets by time-to-resolution, price uncertainty, and volume,
        then checks news evidence for each candidate. Returns signals where
        public information strongly supports one outcome but the market hasn't
        caught up.

        Args:
            markets: Pre-filtered active markets to scan.

        Returns:
            List of signals for markets with clear evidence-price gaps.
        """
        candidates = self._filter_candidates(markets)

        if not candidates:
            logger.debug("Late resolution: no candidate markets after filtering")
            return []

        # Cap concurrent assessments to control API costs
        candidates = candidates[:MAX_CONCURRENT]

        signals = []
        tasks = [self._assess_market(market) for market in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for market, result in zip(candidates, results):
            if isinstance(result, Exception):
                logger.warning(
                    f"Late resolution: error assessing '{market.question[:50]}': {result}"
                )
                continue
            if result is not None:
                signals.append(result)

        logger.info(
            f"Late resolution: scanned {len(markets)} markets, "
            f"{len(candidates)} candidates, {len(signals)} signals"
        )
        return signals

    def _filter_candidates(self, markets: list[Market]) -> list[Market]:
        """Filter markets to those meeting late-resolution criteria.

        A market qualifies if:
        - It has a known resolution date
        - Resolution is within MAX_HOURS_TO_RESOLUTION hours
        - Neither YES nor NO is priced above MAX_MARKET_PRICE (market is uncertain)
        - 24h volume meets MIN_VOLUME threshold
        """
        candidates = []
        for market in markets:
            days = market.days_to_resolution
            if days is None:
                continue

            hours_to_resolution = days * 24.0
            if hours_to_resolution <= 0 or hours_to_resolution > MAX_HOURS_TO_RESOLUTION:
                continue

            # Market must show uncertainty — neither side above 80%
            if market.yes_price >= MAX_MARKET_PRICE or market.no_price >= MAX_MARKET_PRICE:
                continue

            if market.volume_24h < MIN_VOLUME:
                continue

            candidates.append(market)

        return candidates

    async def _assess_market(self, market: Market) -> Optional[Signal]:
        """Assess a single market using news context.

        Fetches recent news, evaluates evidence strength, and generates a
        signal if the evidence clearly supports one side while the market
        lags behind.
        """
        news_context = ""
        if self.news_researcher is not None:
            try:
                news_context = await self.news_researcher.get_context(market.question)
            except Exception as e:
                logger.warning(
                    f"Late resolution: news fetch failed for '{market.question[:50]}': {e}"
                )

        if not news_context:
            logger.debug(
                f"Late resolution: no news context for '{market.question[:50]}', skipping"
            )
            return None

        # Analyze evidence to determine which side is supported
        return self._evaluate_evidence(market, news_context)

    def _evaluate_evidence(
        self, market: Market, news_context: str
    ) -> Optional[Signal]:
        """Evaluate news evidence against market prices.

        Uses simple heuristics on news context to estimate outcome probability.
        A more sophisticated version could use Claude for assessment, but for
        near-resolution markets we prioritize speed.

        The evidence assessment looks for strong affirmative/negative signals
        in the news context. If the evidence is ambiguous, no signal is generated.
        """
        news_lower = news_context.lower()
        question_lower = market.question.lower()

        # Count evidence signals for YES and NO outcomes
        yes_signals = _count_evidence_signals(news_lower, positive=True)
        no_signals = _count_evidence_signals(news_lower, positive=False)

        total_signals = yes_signals + no_signals
        if total_signals == 0:
            return None

        # Estimate probability from evidence balance
        yes_evidence_ratio = yes_signals / total_signals
        if yes_evidence_ratio >= MIN_EVIDENCE_PROBABILITY:
            return self._build_signal(
                market=market,
                direction=Direction.BUY_YES,
                estimated_probability=yes_evidence_ratio,
                market_price=market.yes_price,
                news_context=news_context,
            )
        elif (1.0 - yes_evidence_ratio) >= MIN_EVIDENCE_PROBABILITY:
            return self._build_signal(
                market=market,
                direction=Direction.BUY_NO,
                estimated_probability=1.0 - yes_evidence_ratio,
                market_price=market.no_price,
                news_context=news_context,
            )

        return None

    def _build_signal(
        self,
        market: Market,
        direction: Direction,
        estimated_probability: float,
        market_price: float,
        news_context: str,
    ) -> Optional[Signal]:
        """Build a Signal if the edge is positive.

        Edge = estimated_probability - market_price. Only generates signal
        if edge is positive (we believe the outcome is more likely than the
        market does).
        """
        edge = estimated_probability - market_price
        if edge <= 0:
            return None

        hours_left = (market.days_to_resolution or 0) * 24.0
        side_label = "YES" if direction == Direction.BUY_YES else "NO"

        # Higher confidence for near-resolution with strong evidence
        confidence = min(0.95, CONFIDENCE_BASE + edge * 0.5)

        return Signal(
            strategy=StrategyName.LATE_RESOLUTION,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=round(edge, 4),
            probability_estimate=round(estimated_probability, 4),
            market_price=round(market_price, 4),
            confidence=round(confidence, 4),
            reasoning=(
                f"Late resolution ({hours_left:.1f}h left): "
                f"evidence supports {side_label} at {estimated_probability:.0%} "
                f"but market at {market_price:.0%}. "
                f"Edge: {edge:.1%}."
            ),
        )


def _count_evidence_signals(text: str, *, positive: bool) -> int:
    """Count evidence keywords suggesting a positive or negative outcome.

    This is a lightweight heuristic. For production use, Claude assessment
    would provide better accuracy, but this keeps latency minimal for
    near-resolution speed trades.
    """
    if positive:
        keywords = [
            "confirmed", "approved", "passed", "signed", "announced",
            "agreed", "completed", "succeeded", "enacted", "ratified",
            "officially", "will proceed", "has been confirmed",
        ]
    else:
        keywords = [
            "rejected", "denied", "failed", "vetoed", "blocked",
            "cancelled", "postponed", "withdrawn", "defeated",
            "will not", "unlikely", "has been rejected",
        ]
    return sum(1 for kw in keywords if kw in text)
