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

    def __init__(self, ttl_seconds: int = 1800, api_token: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._api_token = api_token
        self._disabled = False  # Set True after persistent auth failures

    async def search_questions(self, query: str) -> list[dict]:
        """Search Metaculus for open forecast questions matching a query.

        Returns list of question dicts with 'title', 'community_prediction',
        'forecasters_count', and 'url' keys.
        """
        if self._disabled:
            return []

        cache_key = f"metaculus_search_{query[:80]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        if self._api_token:
            headers["Authorization"] = f"Token {self._api_token}"

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
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                logger.warning("Metaculus API requires authentication — disabling for this session")
                self._disabled = True
            else:
                logger.warning(f"Metaculus API request failed: {e}")
            return []
        except httpx.HTTPError as e:
            logger.warning(f"Metaculus API request failed: {e}")
            return []

        results = []
        questions = data.get("results", [])
        for q in questions:
            pred_value = self._extract_prediction(q)

            if pred_value is None:
                continue

            forecasters = q.get("nr_forecasters", q.get("number_of_forecasters", 0))
            results.append({
                "title": q.get("title", ""),
                "community_prediction": float(pred_value),
                "forecasters_count": forecasters,
                "url": q.get("url", ""),
                "id": q.get("id"),
            })

        # If API returned questions but none had predictions, the API
        # is likely hiding prediction data — disable to avoid noise
        if questions and not results:
            logger.info("Metaculus API returned questions but no predictions — disabling for this session")
            self._disabled = True

        self._cache.set(cache_key, results)
        return results

    @staticmethod
    def _extract_prediction(q: dict) -> float | None:
        """Extract community prediction from a question dict.

        Handles multiple API response formats:
        - Legacy: q["community_prediction"] as float or dict
        - New: q["question"]["aggregations"]["recency_weighted"]["latest"]["centers"]
        """
        # Legacy format
        prediction = q.get("community_prediction")
        if isinstance(prediction, dict):
            val = prediction.get("full", {}).get("q2")
            if val is not None:
                return float(val)
        elif isinstance(prediction, (int, float)):
            return float(prediction)

        # New API format: nested under question.aggregations
        qobj = q.get("question", {})
        agg = qobj.get("aggregations", {}).get("recency_weighted", {})
        latest = agg.get("latest")
        if latest:
            # Binary questions: centers is a list with one element (the probability)
            centers = latest.get("centers")
            if centers and isinstance(centers, list) and len(centers) > 0:
                return float(centers[0])
            means = latest.get("means")
            if means and isinstance(means, list) and len(means) > 0:
                return float(means[0])

        return None

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
