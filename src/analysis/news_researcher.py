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
MAX_ARTICLE_FETCH = 3  # Fetch full text for top N results
MAX_ARTICLE_CHARS = 1500  # Max chars to extract per article
ARTICLE_FETCH_TIMEOUT = 5.0  # Seconds per article fetch
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
        serper_url: str = SERPER_SEARCH_URL,
    ):
        self.serper_api_key = serper_api_key
        self.serper_url = serper_url
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
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _do_search),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"DDG search timed out after 8s for '{query[:50]}'")
            return []

    async def _search_serper(self, query: str) -> list[NewsResult]:
        """Search via Serper.dev (paid fallback). Retries on 5xx errors."""
        import asyncio
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        self.serper_url,
                        json={"q": query, "num": MAX_RESULTS_PER_QUERY},
                        headers={
                            "X-API-KEY": self.serper_api_key,
                            "Content-Type": "application/json",
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                break  # Success
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # Rate limit — use exponential backoff, not 1h cooldown
                    wait = min(30, 2 ** (attempt + 2))
                    logger.warning(f"Serper rate limited (429), backing off {wait}s")
                    if attempt < max_retries:
                        await asyncio.sleep(wait)
                        continue
                    return []
                elif e.response.status_code in (400, 401, 403):
                    try:
                        detail = e.response.json().get("message", str(e.response.status_code))
                    except Exception:
                        detail = str(e.response.status_code)
                    import time as _time
                    logger.warning(f"Serper API disabled (1h cooldown): {detail}")
                    self._serper_disabled = True
                    self._serper_disabled_at = _time.monotonic()
                    return []
                elif e.response.status_code >= 500 and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.debug(f"Serper 5xx error, retrying in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    continue
                else:
                    logger.warning(f"Serper search failed for '{query}': {e}")
                    return []
            except httpx.HTTPError as e:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.debug(f"Serper network error, retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
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

    def _is_stale(self, result: NewsResult, max_age_days: int = 7, category: str = "") -> bool:
        """Check if a result's date indicates it is too old to be useful.

        Uses category-aware thresholds: Fed/macro news goes stale faster
        than culture/politics news.
        """
        # Category-specific staleness thresholds (more time-sensitive categories
        # get shorter windows to avoid injecting outdated context into Claude)
        category_max_days = {
            "Fed": 5, "Fed_Macro": 5,
            "Geopolitics": 7,
            "Politics": 14,
            "Culture": 30,
            "Tech": 10, "Tech_AI": 10,
        }
        effective_max = category_max_days.get(category, max_age_days)
        if not result.date:
            return False  # No date — can't determine staleness, keep it
        date_lower = result.date.lower()
        # Check for obviously old relative dates
        import re
        weeks_match = re.search(r"(\d+)\s*week", date_lower)
        if weeks_match:
            weeks = int(weeks_match.group(1))
            if weeks * 7 > effective_max:
                return True
        months_match = re.search(r"(\d+)\s*month", date_lower)
        if months_match:
            return True  # Any "X months ago" is too old
        days_match = re.search(r"(\d+)\s*day", date_lower)
        if days_match:
            days = int(days_match.group(1))
            if days > effective_max:
                return True
        # Try parsing absolute dates
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                from datetime import datetime, timezone
                parsed = datetime.strptime(result.date.strip()[:20], fmt).replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - parsed).days
                if age_days > effective_max:
                    return True
                return False
            except ValueError:
                continue
        # All date formats exhausted — keep article but log for visibility
        logger.info(
            f"Could not parse date '{result.date}' for '{result.title[:50]}...' "
            f"— keeping article (staleness unknown)"
        )
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

    async def _fetch_article_text(self, url: str) -> str:
        """Fetch and extract main text content from an article URL.

        Uses a lightweight approach: fetch HTML, strip tags, extract the
        largest text block. Returns empty string on failure.
        """
        if not url:
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=ARTICLE_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PolyEdge/1.0)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    return ""
                html = resp.text
        except Exception as e:
            logger.debug(f"Article fetch failed for {url}: {e}")
            return ""

        # Extract text: strip script/style tags, then HTML tags
        html = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # Extract the meatiest paragraph-like block (heuristic: longest run of sentences)
        # Split into chunks by double-space or period sequences
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 40]
        if not sentences:
            return ""

        # Take a contiguous block of sentences from the middle (skip boilerplate header/footer)
        start = min(3, len(sentences) // 4)  # Skip first few (often nav/header text)
        block = " ".join(sentences[start:])
        return block[:MAX_ARTICLE_CHARS]

    async def _enrich_with_article_text(self, results: list[NewsResult]) -> list[NewsResult]:
        """Fetch full article text for top results and append to snippets."""
        import asyncio

        to_fetch = results[:MAX_ARTICLE_FETCH]
        tasks = [self._fetch_article_text(r.url) for r in to_fetch]
        texts = await asyncio.gather(*tasks, return_exceptions=True)

        for i, text in enumerate(texts):
            if isinstance(text, str) and text and len(text) > len(to_fetch[i].snippet):
                to_fetch[i].snippet = text

        return results

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
            # Escalate to ERROR when ALL search backends fail — Claude will
            # assess this market with zero news context, increasing false-signal risk.
            if not DDG_AVAILABLE and (not self.serper_api_key or self._serper_disabled):
                logger.error(
                    f"ALL search backends unavailable — Claude assessment for "
                    f"'{market_question[:60]}' will have NO news context. "
                    f"DDG_AVAILABLE={DDG_AVAILABLE}, serper_disabled={self._serper_disabled}"
                )
            else:
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

        # Enrich top results with full article text (replaces snippet if richer)
        try:
            all_results = await self._enrich_with_article_text(all_results)
        except Exception as e:
            logger.debug(f"Article enrichment failed: {e}")

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


_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid",
    "mc_cid", "mc_eid", "ref", "source",
})


def _normalize_url(url: str) -> str:
    """Normalize a URL for deduplication — strip tracking params, fragments, www prefix.

    Keeps non-tracking query params so articles distinguished only by query
    (e.g., ?article=123 vs ?article=456) are not falsely deduplicated.
    """
    try:
        from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        # Strip only known tracking params; keep the rest
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
            clean_query = urlencode(filtered, doseq=True) if filtered else ""
        else:
            clean_query = ""
        return urlunparse((parsed.scheme, host, parsed.path.rstrip("/"), "", clean_query, ""))
    except Exception as e:
        logger.debug(f"URL normalization failed for {url[:80]}: {e}")
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
    except Exception as e:
        logger.debug(f"Source extraction failed for {url[:80]}: {e}")
        return url
