"""Polymarket cross-reference — checks prices on related Polymarket markets.

Searches the Gamma API for similar markets to detect cross-platform
price discrepancies. Useful for validating Kalshi-based assessments
against the larger Polymarket liquidity pool.

No authentication needed.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
MIN_SIMILARITY_THRESHOLD = 0.5


class PolymarketCrossRef:
    """Cross-references market prices with Polymarket."""

    def __init__(self, ttl_seconds: int = 600):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)

    async def search_markets(self, query: str) -> list[dict]:
        """Search Polymarket for open markets matching a query.

        Returns list of market dicts with 'question', 'yes_price',
        'volume', and 'url' keys.
        """
        cache_key = f"polymarket_search_{query[:80]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    GAMMA_API_URL,
                    params={
                        "closed": "false",
                        "limit": 5,
                        "search": query,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.warning(f"Polymarket Gamma API request failed: {e}")
            return []

        results = []
        markets = data if isinstance(data, list) else data.get("markets", [])
        for m in markets:
            question = m.get("question", "")
            if not question:
                continue

            # Extract YES price — Gamma API returns outcomePrices as JSON string or list
            outcome_prices = m.get("outcomePrices")
            yes_price = None
            if isinstance(outcome_prices, str):
                try:
                    import json
                    prices = json.loads(outcome_prices)
                    yes_price = float(prices[0]) if prices else None
                except (ValueError, IndexError) as e:
                    logger.debug(f"Failed to parse outcomePrices: {e}")
            elif isinstance(outcome_prices, list) and outcome_prices:
                yes_price = float(outcome_prices[0])

            if yes_price is None:
                # Try bestBid/bestAsk or other price fields
                yes_price = m.get("bestBid") or m.get("lastTradePrice")
                if yes_price is not None:
                    yes_price = float(yes_price)

            volume = float(m.get("volume", 0) or 0)

            results.append({
                "question": question,
                "yes_price": yes_price,
                "volume": volume,
                "condition_id": m.get("conditionId", ""),
            })

        self._cache.set(cache_key, results)
        return results

    def _calculate_similarity(self, question_a: str, question_b: str) -> float:
        """Calculate keyword overlap similarity between two market questions."""
        a_words = set(re.findall(r"\w{3,}", question_a.lower()))
        b_words = set(re.findall(r"\w{3,}", question_b.lower()))

        if not a_words or not b_words:
            return 0.0

        intersection = a_words & b_words
        union = a_words | b_words
        return len(intersection) / len(union) if union else 0.0

    async def get_best_match(self, market_question: str) -> Optional[dict]:
        """Find the best matching Polymarket market.

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
            similarity = self._calculate_similarity(market_question, result["question"])
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = result

        if best_match and best_similarity >= MIN_SIMILARITY_THRESHOLD:
            best_match["similarity"] = best_similarity
            return best_match

        return None

    async def get_context(self, market_question: str, kalshi_yes_price: float) -> str:
        """Get formatted context string for Claude prompts.

        Args:
            market_question: The market question to cross-reference
            kalshi_yes_price: Current YES price on Kalshi (0.0-1.0)

        Returns empty string if no good match is found.
        """
        match = await self.get_best_match(market_question)
        if match is None:
            return ""

        poly_price = match.get("yes_price")
        if poly_price is None:
            return ""

        discrepancy = poly_price - kalshi_yes_price
        disc_pct = discrepancy * 100
        volume = match.get("volume", 0)

        lines = [
            "CROSS-PLATFORM PRICE CHECK (Polymarket):",
            f"- Matched: \"{match['question']}\"",
            f"- Polymarket YES: {poly_price:.0%} vs Kalshi YES: {kalshi_yes_price:.0%} "
            f"({disc_pct:+.0f}% discrepancy)",
        ]
        if volume > 0:
            if volume >= 1_000_000:
                lines.append(f"- Polymarket volume: ${volume / 1_000_000:.1f}M")
            elif volume >= 1_000:
                lines.append(f"- Polymarket volume: ${volume / 1_000:.0f}K")
            else:
                lines.append(f"- Polymarket volume: ${volume:,.0f}")

        return "\n".join(lines)
