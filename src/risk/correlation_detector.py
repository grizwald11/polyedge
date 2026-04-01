"""Correlation detector — checks if a proposed trade would exceed correlated exposure limits.

Goes beyond simple event-ticker matching (which portfolio_risk.py handles) to also
detect keyword-based correlation between markets in the same category.

Correlation levels:
  - Same event_ticker: 100% correlated (positions fully count)
  - Same category + >30% keyword overlap: 50% correlated (half of cost_basis counts)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from src.execution.position_manager import PositionManager
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Words that appear in many market questions but carry no topical signal.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "will", "would", "could", "should", "shall", "may", "might", "can",
    "do", "does", "did", "has", "have", "had", "having",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "and", "or", "but", "not", "no", "nor", "if", "then", "than",
    "this", "that", "these", "those", "it", "its",
    "what", "which", "who", "whom", "when", "where", "how", "why",
    "all", "each", "every", "any", "some", "other", "another",
    "before", "after", "above", "below", "between", "during", "about",
    "up", "down", "out", "off", "over", "under", "again", "further",
    "there", "here", "so", "very", "just", "also", "still", "already",
    "yes", "win", "happen", "occur", "become", "make", "get",
})

# Minimum keyword overlap ratio (Jaccard) to consider category-correlated.
_DEFAULT_KEYWORD_OVERLAP_THRESHOLD = 0.30

# Correlation weight for keyword-matched markets (same category, overlapping keywords).
_KEYWORD_CORRELATION_WEIGHT = 0.50


@dataclass
class CorrelationCheckResult:
    """Result of a correlation check for a proposed trade."""

    allowed: bool
    correlated_exposure: float  # Total correlated $ exposure (including proposed trade)
    max_allowed: float  # Max correlated $ allowed (bankroll * max_correlated_exposure_pct)
    correlations: list[dict] = field(default_factory=list)
    # Each dict: {"market_id": str, "reason": str, "weight": float, "exposure": float}


class CorrelationDetector:
    """Detects correlated positions and enforces exposure limits.

    Used by the risk engine before allowing a new trade.
    """

    def __init__(
        self,
        position_manager: PositionManager,
        db: Database,
        settings,
    ):
        self.positions = position_manager
        self.db = db
        self.max_correlated_exposure_pct: float = settings.trading.max_correlated_exposure_pct
        self._market_cache: dict[str, Optional[dict]] = {}

    def check_correlation(
        self,
        market_id: str,
        proposed_size_dollars: float,
        bankroll: float,
    ) -> CorrelationCheckResult:
        """Check whether a proposed trade would breach correlated exposure limits.

        Args:
            market_id: The market ticker for the proposed trade.
            proposed_size_dollars: Dollar cost of the proposed position.
            bankroll: Current total bankroll for limit calculation.

        Returns:
            CorrelationCheckResult with allowed flag and details.
        """
        max_allowed = bankroll * self.max_correlated_exposure_pct
        if max_allowed <= 0:
            return CorrelationCheckResult(
                allowed=False,
                correlated_exposure=proposed_size_dollars,
                max_allowed=0.0,
                correlations=[],
            )

        proposed_market = self._get_market_data(market_id)
        proposed_event = proposed_market.get("event_ticker", "") if proposed_market else ""
        proposed_category = proposed_market.get("category", "") if proposed_market else ""
        proposed_question = proposed_market.get("question", "") if proposed_market else ""

        correlations: list[dict] = []
        correlated_dollars = 0.0

        for pos in self.positions.get_all_positions():
            # Skip if this is the same market (we're adding to an existing position,
            # not creating a new correlated one — the position limit check handles that).
            if pos.market_id == market_id:
                continue

            pos_market = self._get_market_data(pos.market_id)
            if not pos_market:
                continue

            pos_event = pos_market.get("event_ticker", "")
            pos_category = pos_market.get("category", "")
            pos_question = pos_market.get("question", "")
            pos_cost = pos.cost_basis

            # Check 1: Same event ticker → 100% correlated
            if proposed_event and pos_event and self._events_match(proposed_event, pos_event):
                contribution = pos_cost  # 100% weight
                correlated_dollars += contribution
                correlations.append({
                    "market_id": pos.market_id,
                    "reason": f"same_event_ticker:{pos_event}",
                    "weight": 1.0,
                    "exposure": contribution,
                })
                continue  # Don't double-count via keyword overlap

            # Check 2: Same category + keyword overlap → 50% correlated
            if (
                proposed_category
                and pos_category
                and proposed_category == pos_category
                and proposed_question
                and pos_question
            ):
                overlap = self._keyword_overlap(proposed_question, pos_question)
                if overlap >= _DEFAULT_KEYWORD_OVERLAP_THRESHOLD:
                    contribution = pos_cost * _KEYWORD_CORRELATION_WEIGHT
                    correlated_dollars += contribution
                    correlations.append({
                        "market_id": pos.market_id,
                        "reason": f"keyword_overlap:{overlap:.2f}_category:{pos_category}",
                        "weight": _KEYWORD_CORRELATION_WEIGHT,
                        "exposure": contribution,
                    })

        total_correlated = correlated_dollars + proposed_size_dollars
        allowed = total_correlated <= max_allowed

        if not allowed:
            logger.warning(
                "Correlation limit breached for %s: $%.2f correlated (limit $%.2f)",
                market_id,
                total_correlated,
                max_allowed,
            )

        return CorrelationCheckResult(
            allowed=allowed,
            correlated_exposure=total_correlated,
            max_allowed=max_allowed,
            correlations=correlations,
        )

    def _get_market_data(self, market_id: str) -> Optional[dict]:
        """Look up market data from the database (cached)."""
        if market_id in self._market_cache:
            return self._market_cache[market_id]
        data = self.db.get_market(market_id)
        self._market_cache[market_id] = data
        return data

    def _get_event_ticker(self, market_id: str) -> Optional[str]:
        """Look up the event_ticker for a market."""
        data = self._get_market_data(market_id)
        if data:
            return data.get("event_ticker") or None
        return None

    @staticmethod
    def _events_match(event_a: str, event_b: str) -> bool:
        """Check if two event tickers match (with platform-prefix normalization)."""
        def normalize(et: str) -> str:
            return et.split(":", 1)[1] if ":" in et else et
        return normalize(event_a) == normalize(event_b)

    @staticmethod
    def _keyword_overlap(question_a: str, question_b: str) -> float:
        """Compute Jaccard similarity of meaningful keywords between two questions.

        Returns a float in [0.0, 1.0]. Higher means more overlap.
        """
        keywords_a = CorrelationDetector._extract_keywords(question_a)
        keywords_b = CorrelationDetector._extract_keywords(question_b)

        if not keywords_a or not keywords_b:
            return 0.0

        intersection = keywords_a & keywords_b
        union = keywords_a | keywords_b

        if not union:
            return 0.0

        return len(intersection) / len(union)

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        """Extract meaningful lowercase keywords from a market question.

        Strips punctuation, removes stop words, and lowercases everything.
        """
        # Remove punctuation, keep alphanumeric and spaces
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
        words = cleaned.lower().split()
        return {w for w in words if w not in _STOP_WORDS and len(w) > 1}

    def clear_cache(self) -> None:
        """Clear the market data cache. Call at the start of each scan cycle."""
        self._market_cache.clear()
