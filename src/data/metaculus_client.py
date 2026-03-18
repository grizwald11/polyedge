"""Metaculus client — fetches community forecasts for cross-reference.

Searches the Metaculus API for questions similar to active markets
and returns community probability estimates. Metaculus forecasters
are historically well-calibrated, making these valuable reference points.

No authentication needed (public read API).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

METACULUS_API_URL = "https://www.metaculus.com/api2/questions/"
MIN_SIMILARITY_THRESHOLD = 0.4


class MetaculusClient:
    """Fetches community forecasts from the Metaculus API."""

    def __init__(self, ttl_seconds: int = 1800):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)

    async def search_questions(self, query: str) -> list[dict]:
        """Search Metaculus for open forecast questions matching a query.

        Returns list of question dicts with 'title', 'community_prediction',
        'forecasters_count', and 'url' keys.
        """
        cache_key = f"metaculus_search_{query[:80]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    METACULUS_API_URL,
                    params={
                        "search": query,
                        "status": "open",
                        "type": "forecast",
                        "limit": 5,
                    },
                    headers={
                        "User-Agent": "PolyEdge/1.0",
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.warning(f"Metaculus API request failed: {e}")
            return []

        results = []
        questions = data.get("results", [])
        for q in questions:
            prediction = q.get("community_prediction")
            # community_prediction can be a dict with 'full' key or a float
            if isinstance(prediction, dict):
                pred_value = prediction.get("full", {}).get("q2")
            elif isinstance(prediction, (int, float)):
                pred_value = prediction
            else:
                pred_value = None

            if pred_value is None:
                continue

            forecasters = q.get("number_of_forecasters", 0)
            results.append({
                "title": q.get("title", ""),
                "community_prediction": float(pred_value),
                "forecasters_count": forecasters,
                "url": q.get("url", ""),
                "id": q.get("id"),
            })

        self._cache.set(cache_key, results)
        return results

    def _calculate_similarity(self, market_question: str, metaculus_title: str) -> float:
        """Calculate keyword overlap similarity between two questions."""
        q_words = set(re.findall(r"\w{3,}", market_question.lower()))
        m_words = set(re.findall(r"\w{3,}", metaculus_title.lower()))

        if not q_words or not m_words:
            return 0.0

        intersection = q_words & m_words
        union = q_words | m_words
        return len(intersection) / len(union) if union else 0.0

    async def get_best_match(self, market_question: str) -> Optional[dict]:
        """Find the best matching Metaculus question for a market.

        Returns the best match dict with an added 'similarity' key,
        or None if no match exceeds the similarity threshold.
        """
        # Extract core search terms from the market question
        cleaned = re.sub(
            r"^(Will|Is|Does|Do|Has|Have|Can|Could|Would|Should)\s+",
            "",
            market_question.strip().rstrip("?"),
            flags=re.IGNORECASE,
        )

        results = await self.search_questions(cleaned)
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
        forecasters = match["forecasters_count"]

        lines = [
            "METACULUS COMMUNITY FORECAST:",
            f"- Matched: \"{match['title']}\" ({similarity_pct:.0f}% similarity)",
            f"- Community prediction: {pred_pct:.0f}% YES ({forecasters} forecasters)",
            "- Metaculus forecasters are historically well-calibrated.",
        ]
        return "\n".join(lines)
