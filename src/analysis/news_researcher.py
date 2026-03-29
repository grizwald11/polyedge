"""News researcher — enriches Claude's probability assessments with real-time context.

Uses DuckDuckGo search (free, no API key) via the duckduckgo-search library as the
primary backend, with Serper.dev as an optional paid fallback. Formats a concise
context block for injection into Claude prompts.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"

MAX_RESULTS_PER_QUERY = 5
MAX_QUERIES = 4
MAX_CONTEXT_CHARS = 4000  # ~1000 tokens — increased to reduce mid-article truncation
MAX_RELEVANT_RESULTS = 5
DEDUP_SIMILARITY_THRESHOLD = 0.7

# Common abbreviation → expanded form for broader news coverage
_ENTITY_EXPANSIONS = [
    ("Fed ", "Federal Reserve "),
    ("DHS ", "Department of Homeland Security "),
    ("DOJ ", "Department of Justice "),
    ("GDP ", "gross domestic product "),
    ("CPI ", "consumer price index inflation "),
    ("SCOTUS ", "Supreme Court "),
    ("NATO ", "North Atlantic Treaty Organization "),
    ("EU ", "European Union "),
    ("UN ", "United Nations "),
    ("WHO ", "World Health Organization "),
    ("SEC ", "Securities and Exchange Commission "),
    ("EPA ", "Environmental Protection Agency "),
    ("FBI ", "Federal Bureau of Investigation "),
    ("CIA ", "Central Intelligence Agency "),
    ("DNI ", "Director of National Intelligence "),
]

# Check if ddgs (or legacy duckduckgo_search) is available
try:
    from ddgs import DDGS
    DDG_AVAILABLE = True
except ImportError:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            from duckduckgo_search import DDGS
        DDG_AVAILABLE = True
    except ImportError:
        DDG_AVAILABLE = False
        logger.info("ddgs not installed — DDG search disabled")


@dataclass
class NewsResult:
    """A single news search result."""
    title: str
    snippet: str
    source: str
    date: str
    url: str


class NewsResearcher:
    """Fetches recent news context for market probability assessments.

    Search priority:
    1. DuckDuckGo (free, no API key required) — via duckduckgo-search library
    2. Serper.dev (paid fallback) — if DDG fails and API key is configured
    """

    def __init__(
        self,
        serper_api_key: Optional[str] = None,
        searxng_url: Optional[str] = None,
    ):
        self.serper_api_key = serper_api_key
        # searxng_url kept for backward compatibility
        self.searxng_url = searxng_url
        self._serper_disabled = False  # Set True after credit/auth failures
        self._serper_disabled_at: float = 0.0  # Monotonic time of disable
        self._serper_cooldown_seconds: float = 3600.0  # Re-enable after 1 hour

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

        # Add a time-scoped query using current year
        from datetime import datetime, timezone
        current_year = datetime.now(timezone.utc).year
        queries.append(f"{cleaned} latest news {current_year}")

        # Add a more specific query focusing on key entities
        # Extract capitalized words as likely entities
        entities = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", market_question)
        if entities:
            entity_query = " ".join(entities[:3])
            if entity_query.lower() != cleaned.lower():
                queries.append(entity_query)

        # Add broader context query — expand key terms for wider coverage
        # e.g., "Fed cut rates" → "Federal Reserve interest rate decision"
        broad = cleaned
        for short, expanded in _ENTITY_EXPANSIONS:
            if short in broad:
                broad = broad.replace(short, expanded, 1)
                break
        if broad != cleaned:
            queries.append(f"{broad} {current_year}")

        return queries[:MAX_QUERIES]

    async def search(self, query: str) -> list[NewsResult]:
        """Search using DuckDuckGo first, fall back to Serper if needed."""
        # Try DuckDuckGo first (free, no key required)
        if DDG_AVAILABLE:
            results = await self._search_ddg(query)
            if results:
                return results

        # Re-enable Serper after cooldown
        if self._serper_disabled and self._serper_disabled_at > 0:
            import time as _time
            elapsed = _time.monotonic() - self._serper_disabled_at
            if elapsed >= self._serper_cooldown_seconds:
                logger.info("Serper API cooldown expired — re-enabling")
                self._serper_disabled = False
                self._serper_disabled_at = 0.0

        # Fall back to Serper if configured and not disabled
        if self.serper_api_key and not self._serper_disabled:
            return await self._search_serper(query)

        return []

    async def _search_ddg(self, query: str) -> list[NewsResult]:
        """Search via DuckDuckGo using duckduckgo-search library.

        Uses the news endpoint for recency, falls back to text search.
        Runs synchronous DDGS in a thread to avoid blocking the event loop.
        """
        import asyncio

        def _do_search() -> list[NewsResult]:
            results = []
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    with DDGS() as ddgs:
                        for item in ddgs.news(query, max_results=MAX_RESULTS_PER_QUERY):
                            results.append(NewsResult(
                                title=item.get("title", ""),
                                snippet=item.get("body", ""),
                                source=item.get("source", ""),
                                date=item.get("date", ""),
                                url=item.get("url", ""),
                            ))
            except Exception as e:
                logger.debug(f"DDG news search failed for '{query}': {e}")

            if not results:
                # Fall back to text search
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        with DDGS() as ddgs:
                            for item in ddgs.text(query, max_results=MAX_RESULTS_PER_QUERY):
                                results.append(NewsResult(
                                    title=item.get("title", ""),
                                    snippet=item.get("body", ""),
                                    source=_extract_source(item.get("href", "")),
                                    date="",
                                    url=item.get("href", ""),
                                ))
                except Exception as e:
                    logger.debug(f"DDG text search failed for '{query}': {e}")

            return results

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _do_search)

    async def _search_serper(self, query: str) -> list[NewsResult]:
        """Search via Serper.dev (paid fallback)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    SERPER_SEARCH_URL,
                    json={"q": query, "num": MAX_RESULTS_PER_QUERY},
                    headers={
                        "X-API-KEY": self.serper_api_key,
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 401, 403):
                try:
                    detail = e.response.json().get("message", str(e.response.status_code))
                except Exception:
                    detail = str(e.response.status_code)
                import time as _time
                logger.warning(f"Serper API disabled (1h cooldown): {detail}")
                self._serper_disabled = True
                self._serper_disabled_at = _time.monotonic()
            else:
                logger.warning(f"Serper search failed for '{query}': {e}")
            return []
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

    def _is_stale(self, result: NewsResult, max_age_days: int = 7) -> bool:
        """Check if a result's date indicates it is too old to be useful."""
        if not result.date:
            return False  # No date — can't determine staleness, keep it
        date_lower = result.date.lower()
        # Check for obviously old relative dates
        import re
        weeks_match = re.search(r"(\d+)\s*week", date_lower)
        if weeks_match:
            weeks = int(weeks_match.group(1))
            if weeks * 7 > max_age_days:
                return True
        months_match = re.search(r"(\d+)\s*month", date_lower)
        if months_match:
            return True  # Any "X months ago" is too old
        days_match = re.search(r"(\d+)\s*day", date_lower)
        if days_match:
            days = int(days_match.group(1))
            if days > max_age_days:
                return True
        # Try parsing absolute dates
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                from datetime import datetime, timezone
                parsed = datetime.strptime(result.date.strip()[:20], fmt).replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - parsed).days
                if age_days > max_age_days:
                    return True
                return False
            except ValueError:
                continue
        return False

    def _score_relevance(self, result: NewsResult, market_question: str) -> float:
        """Score a result's relevance to the market question.

        Uses keyword overlap plus recency bonus.
        """
        q_words = set(re.findall(r"\w{3,}", market_question.lower()))
        title_words = set(re.findall(r"\w{3,}", result.title.lower()))
        snippet_words = set(re.findall(r"\w{3,}", result.snippet.lower()))
        result_words = title_words | snippet_words

        if not q_words:
            return 0.0

        overlap = len(q_words & result_words) / len(q_words)

        # Recency bonus: results with recent dates score higher
        recency_bonus = 0.0
        if result.date:
            date_lower = result.date.lower()
            for recent_kw in ("hour", "minute", "today", "yesterday", "1 day"):
                if recent_kw in date_lower:
                    recency_bonus = 0.15
                    break
            else:
                for kw in ("2 day", "3 day", "week"):
                    if kw in date_lower:
                        recency_bonus = 0.05
                        break

        return min(1.0, overlap + recency_bonus)

    def _deduplicate(self, results: list[NewsResult]) -> list[NewsResult]:
        """Remove near-duplicate results based on title word overlap."""
        if not results:
            return results

        unique: list[NewsResult] = [results[0]]
        for r in results[1:]:
            r_words = set(re.findall(r"\w{3,}", r.title.lower()))
            is_dup = False
            for u in unique:
                u_words = set(re.findall(r"\w{3,}", u.title.lower()))
                if not r_words or not u_words:
                    continue
                union = r_words | u_words
                intersection = r_words & u_words
                similarity = len(intersection) / len(union) if union else 0.0
                if similarity > DEDUP_SIMILARITY_THRESHOLD:
                    is_dup = True
                    break
            if not is_dup:
                unique.append(r)
        return unique

    async def get_context(self, market_question: str) -> str:
        """Get formatted news context for a market question.

        Returns a formatted text block ready for prompt injection.
        Returns empty string if no results found.
        """
        queries = self.generate_queries(market_question)
        all_results: list[NewsResult] = []
        seen_urls: set[str] = set()

        for query in queries:
            results = await self.search(query)
            for r in results:
                normalized = _normalize_url(r.url) if r.url else r.url
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    all_results.append(r)

        if not all_results:
            logger.info(f"No news results for: {market_question[:60]}")
            return ""

        # Filter stale results, deduplicate, score by relevance, keep top results
        fresh_results = [r for r in all_results if not self._is_stale(r)]
        if fresh_results:
            all_results = fresh_results
        # else: keep all results if everything is stale (better than nothing)
        all_results = self._deduplicate(all_results)
        all_results.sort(
            key=lambda r: self._score_relevance(r, market_question), reverse=True
        )
        all_results = all_results[:MAX_RELEVANT_RESULTS]

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


def _normalize_url(url: str) -> str:
    """Normalize a URL for deduplication — strip query params, fragments, www prefix."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        # Keep scheme, normalized host, and path; drop query and fragment
        return urlunparse((parsed.scheme, host, parsed.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


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
