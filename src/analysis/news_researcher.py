"""News researcher — enriches Claude's probability assessments with real-time context.

Uses Serper.dev (Google Search API) to find recent news relevant to a market
question, then formats a concise context block for injection into Claude prompts.
Gracefully degrades if no API key is configured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
MAX_RESULTS_PER_QUERY = 5
MAX_QUERIES = 3
MAX_CONTEXT_CHARS = 3200  # ~800 tokens


@dataclass
class NewsResult:
    """A single news search result."""
    title: str
    snippet: str
    source: str
    date: str
    url: str


class NewsResearcher:
    """Fetches recent news context for market probability assessments."""

    def __init__(self, serper_api_key: Optional[str] = None):
        self.serper_api_key = serper_api_key

    def generate_queries(self, market_question: str) -> list[str]:
        """Generate 2-3 targeted search queries from a market question.

        Strips common prediction-market phrasing to extract the core topic,
        then creates queries with different angles (recent news, timeline).
        """
        # Strip prediction market framing
        cleaned = market_question.strip().rstrip("?")
        cleaned = re.sub(
            r"^(Will|Is|Does|Do|Has|Have|Can|Could|Would|Should)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        # Build queries with different angles
        queries = [cleaned]

        # Add a time-scoped query
        queries.append(f"{cleaned} latest news 2026")

        # Add a more specific query focusing on key entities
        # Extract capitalized words as likely entities
        entities = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", market_question)
        if entities:
            entity_query = " ".join(entities[:3])
            if entity_query.lower() != cleaned.lower():
                queries.append(entity_query)

        return queries[:MAX_QUERIES]

    async def search(self, query: str) -> list[NewsResult]:
        """Search Serper.dev for a single query. Returns top results."""
        if not self.serper_api_key:
            return []

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    SERPER_SEARCH_URL,
                    json={"q": query, "num": MAX_RESULTS_PER_QUERY},
                    headers={"X-API-KEY": self.serper_api_key},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.warning(f"Serper search failed for '{query}': {e}")
            return []

        results = []
        for item in data.get("organic", [])[:MAX_RESULTS_PER_QUERY]:
            results.append(NewsResult(
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                source=_extract_source(item.get("link", "")),
                date=item.get("date", ""),
                url=item.get("link", ""),
            ))
        return results

    async def get_context(self, market_question: str) -> str:
        """Get formatted news context for a market question.

        Returns a formatted text block ready for prompt injection.
        Returns empty string if no API key or no results found.
        """
        if not self.serper_api_key:
            logger.debug("No Serper API key configured, skipping news research")
            return ""

        queries = self.generate_queries(market_question)
        all_results: list[NewsResult] = []
        seen_urls: set[str] = set()

        for query in queries:
            results = await self.search(query)
            for r in results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    all_results.append(r)

        if not all_results:
            logger.info(f"No news results for: {market_question[:60]}")
            return ""

        context = self._format_context(all_results)
        logger.info(
            f"News research: {len(all_results)} results for '{market_question[:50]}...'"
        )
        return context

    def _format_context(self, results: list[NewsResult]) -> str:
        """Format news results into a concise context block."""
        lines = ["RECENT NEWS CONTEXT:"]
        for i, r in enumerate(results, 1):
            source_date = f"({r.source}"
            if r.date:
                source_date += f", {r.date}"
            source_date += ")"

            entry = f'[{i}] "{r.title}" {source_date}\n{r.snippet}'
            lines.append(entry)

        context = "\n\n".join(lines)

        # Truncate if too long
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS].rsplit("\n", 1)[0] + "\n..."

        return context


def _extract_source(url: str) -> str:
    """Extract a readable source name from a URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Strip www. prefix
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return url
