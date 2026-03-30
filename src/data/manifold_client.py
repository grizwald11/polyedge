"""Manifold Markets client — fetches community forecasts for cross-reference.

Searches the Manifold Markets API for questions similar to active markets
and returns community probability estimates. Manifold is a prediction market
with a free, open API and no authentication required.

Replaces Metaculus as community forecast source since Metaculus restricted
their API predictions endpoint.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

MANIFOLD_SEARCH_URL = "https://api.manifold.markets/v0/search-markets"
MIN_SIMILARITY_THRESHOLD = 0.50  # Increased from 0.35 to reduce false positive matches
MIN_BETTORS = 5  # Minimum unique bettors for a credible signal


class ManifoldClient:
    """Fetches community forecasts from the Manifold Markets API."""

    def __init__(self, ttl_seconds: int = 1800, base_url: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._base_url = base_url or MANIFOLD_SEARCH_URL

    async def search_markets(self, query: str) -> list[dict]:
        """Search Manifold for binary markets matching a query.

        Returns list of market dicts with 'title', 'community_prediction',
        'forecasters_count', and 'url' keys (compatible with MetaculusClient).
        """
        cache_key = f"manifold_search_{query[:80]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self._base_url,
                    params={
                        "term": query,
                        "limit": 5,
                        "filter": "open",
                        "sort": "score",
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.warning(f"Manifold API request failed: {e}")
            return []

        results = []
        for m in data:
            # Only use binary markets with community predictions
            prob = m.get("probability")
            if prob is None:
                continue

            outcome_type = m.get("outcomeType", "")
            if outcome_type != "BINARY":
                continue

            bettors = m.get("uniqueBettorCount", 0)
            if bettors < MIN_BETTORS:
                continue

            slug = m.get("slug", "")
            results.append({
                "title": m.get("question", ""),
                "community_prediction": float(prob),
                "forecasters_count": bettors,
                "url": f"https://manifold.markets/{m.get('creatorUsername', '')}/{slug}",
                "id": m.get("id"),
                "volume": m.get("volume", 0),
            })

        self._cache.set(cache_key, results)
        return results

    def _calculate_similarity(self, market_question: str, manifold_title: str) -> float:
        """Calculate keyword overlap similarity between two questions."""
        q_words = set(re.findall(r"\w{3,}", market_question.lower()))
        m_words = set(re.findall(r"\w{3,}", manifold_title.lower()))

        if not q_words or not m_words:
            return 0.0

        intersection = q_words & m_words
        union = q_words | m_words
        return len(intersection) / len(union) if union else 0.0

    async def get_best_match(self, market_question: str) -> Optional[dict]:
        """Find the best matching Manifold market for a Kalshi market.

        Returns the best match dict with an added 'similarity' key,
        or None if no match exceeds the similarity threshold.
        """
        # Extract core search terms
        cleaned = re.sub(
            r"^(Will|Is|Does|Do|Has|Have|Can|Could|Would|Should)\s+",
            "",
            market_question.strip().rstrip("?"),
            flags=re.IGNORECASE,
        )

        results = await self.search_markets(cleaned)
        if not results:
            return None

        best_match = None
        best_similarity = 0.0

        for result in results:
            similarity = self._calculate_similarity(market_question, result["title"])
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = result

        if best_match and best_similarity >= MIN_SIMILARITY_THRESHOLD:
            best_match["similarity"] = best_similarity
            return best_match

        return None

    async def get_context(self, market_question: str) -> str:
        """Get formatted context string for Claude prompts.

        Returns empty string if no good match is found.
        """
        match = await self.get_best_match(market_question)
        if match is None:
            return ""

        pred_pct = match["community_prediction"] * 100
        similarity_pct = match["similarity"] * 100
        bettors = match["forecasters_count"]

        lines = [
            "MANIFOLD MARKETS COMMUNITY FORECAST:",
            f"- Matched: \"{match['title']}\" ({similarity_pct:.0f}% similarity)",
            f"- Community prediction: {pred_pct:.0f}% YES ({bettors} bettors)",
            "- Manifold Markets uses play-money but predictions are historically well-calibrated.",
        ]
        return "\n".join(lines)
