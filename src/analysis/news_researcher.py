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
from html.parser import HTMLParser
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class _ArticleTextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping script/style/nav/header/footer."""

    _SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg"})

    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth: int = 0
        self._in_paragraph: bool = False

    def handle_starttag(self, tag: str, attrs):
        tag_lower = tag.lower()
        if tag_lower in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag_lower in ("p", "div", "article", "section", "h1", "h2", "h3", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_endtag(self, tag: str):
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag.lower() in ("p", "div", "article", "section", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        # Collapse runs of whitespace but preserve paragraph breaks
        lines = raw.split("\n")
        cleaned = []
        for line in lines:
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line:
                cleaned.append(line)
        return " ".join(cleaned)


def _extract_text_from_html(html: str) -> str:
    """Extract visible text from HTML using stdlib parser."""
    parser = _ArticleTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # Fallback: strip tags with regex if parser fails on malformed HTML
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()
    return parser.get_text()


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last sentence boundary before max_chars."""
    if len(text) <= max_chars:
        return text
    # Find last sentence-ending punctuation before limit
    truncated = text[:max_chars]
    for end_char in (".!?"):
        last_pos = truncated.rfind(end_char)
        if last_pos > max_chars * 0.5:  # Don't truncate too aggressively
            return truncated[: last_pos + 1]
    # No good sentence boundary found — cut at last space
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return truncated[:last_space] + "..."
    return truncated + "..."

SERPER_SEARCH_URL = "https://google.serper.dev/search"

MAX_RESULTS_PER_QUERY = 5
MAX_QUERIES = 4
MAX_CONTEXT_CHARS = 4000  # ~1000 tokens — increased to reduce mid-article truncation
MAX_RELEVANT_RESULTS = 5
MAX_ARTICLE_FETCH = 3  # Fetch full text for top N results
MAX_ARTICLE_CHARS = 3000  # Max chars to extract per article
ARTICLE_FETCH_TIMEOUT = 5.0  # Seconds per article fetch
DEDUP_SIMILARITY_THRESHOLD = 0.7

# L-7: Source trust multipliers — higher-trust sources get boosted relevance scores
SOURCE_TRUST_MULTIPLIERS: dict[str, float] = {
    "reuters.com": 1.3,
    "apnews.com": 1.3,
    "nytimes.com": 1.2,
    "washingtonpost.com": 1.2,
    "bbc.com": 1.2,
    "bbc.co.uk": 1.2,
    "bloomberg.com": 1.2,
    "ft.com": 1.15,
    "wsj.com": 1.15,
    "economist.com": 1.15,
    "npr.org": 1.1,
    "politico.com": 1.1,
}

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

# Check if ddgs (or legacy duckduckgo_search) is available.
# The duckduckgo_search package was renamed to ddgs — suppress the rename warning.
DDG_AVAILABLE = False
try:
    from ddgs import DDGS
    DDG_AVAILABLE = True
except ImportError:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*renamed.*ddgs.*", category=RuntimeWarning)
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        DDG_AVAILABLE = True
    except ImportError:
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
        self._serper_auth_failure_count: int = 0  # Consecutive 4xx auth failures
        # M-12: Track the API key at time of permanent disable so we can
        # auto-recover if the key is rotated/changed.
        self._serper_key_at_disable: Optional[str] = None

    def reset_serper(self) -> None:
        """Manually re-enable Serper after permanent disable.

        Call this after fixing SERPER_API_KEY or rotating the key.
        """
        was_disabled = self._serper_disabled
        self._serper_disabled = False
        self._serper_disabled_at = 0.0
        self._serper_auth_failure_count = 0
        if was_disabled:
            logger.info("Serper API manually re-enabled")

    @property
    def serper_permanently_disabled(self) -> bool:
        """True if Serper hit 3 consecutive auth failures and is permanently off."""
        return self._serper_disabled and self._serper_disabled_at == float("inf")

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

        # M-12: Auto-recover if Serper API key has changed since permanent disable
        if (
            self._serper_disabled
            and self._serper_disabled_at == float("inf")
            and self._serper_key_at_disable is not None
            and self.serper_api_key != self._serper_key_at_disable
        ):
            logger.info(
                "Serper API key changed since permanent disable — auto-resetting"
            )
            self._serper_disabled = False
            self._serper_disabled_at = 0.0
            self._serper_auth_failure_count = 0
            self._serper_key_at_disable = None

        # Re-enable Serper after cooldown — but not if permanently disabled
        # (3+ consecutive auth failures sets _serper_disabled_at to float("inf"))
        if self._serper_disabled and 0 < self._serper_disabled_at < float("inf"):
            import time as _time
            elapsed = _time.monotonic() - self._serper_disabled_at
            if elapsed >= self._serper_cooldown_seconds:
                logger.info("Serper API cooldown expired — re-enabling")
                self._serper_disabled = False
                self._serper_disabled_at = 0.0
                self._serper_auth_failure_count = 0  # Reset counter after successful cooldown

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
                # Reset auth failure counter on any successful call
                if self._serper_auth_failure_count > 0:
                    logger.info(
                        f"Serper API call succeeded — resetting auth failure counter "
                        f"(was {self._serper_auth_failure_count})"
                    )
                    self._serper_auth_failure_count = 0
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
                    except (ValueError, KeyError, AttributeError):
                        detail = str(e.response.status_code)
                    import time as _time
                    self._serper_auth_failure_count += 1
                    if self._serper_auth_failure_count >= 3:
                        # 3 consecutive auth failures → permanently disable Serper.
                        # This prevents indefinite hourly retry storms on invalid keys.
                        logger.critical(
                            f"Serper API PERMANENTLY DISABLED after "
                            f"{self._serper_auth_failure_count} consecutive auth failures "
                            f"({e.response.status_code}): {detail}. "
                            f"News quality degraded — using DuckDuckGo only. "
                            f"Fix: check SERPER_API_KEY, then call reset_serper() or restart."
                        )
                        self._serper_disabled = True
                        self._serper_disabled_at = float("inf")  # Never re-enable via cooldown
                        self._serper_key_at_disable = self.serper_api_key  # M-12
                    else:
                        logger.warning(
                            f"Serper API auth failure #{self._serper_auth_failure_count} "
                            f"(1h cooldown): {detail}"
                        )
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
                # Sanitize error to avoid leaking API keys in logs
                safe_err = str(e)
                if self.serper_api_key and self.serper_api_key in safe_err:
                    safe_err = safe_err.replace(self.serper_api_key, "***REDACTED***")
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.debug(f"Serper network error, retrying in {wait}s: {safe_err}")
                    await asyncio.sleep(wait)
                    continue
                logger.warning(f"Serper search failed for '{query}': {safe_err}")
                return []

        return self._parse_serper_response(data)

    @staticmethod
    def _parse_serper_response(data: dict) -> list[NewsResult]:
        """Parse Serper API JSON response into NewsResult objects."""
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

    def _is_stale(
        self,
        result: NewsResult,
        max_age_days: int = 7,
        category: str = "",
        fetch_timestamp: Optional[float] = None,
    ) -> bool:
        """Check if a result's date indicates it is too old to be useful.

        Uses category-aware thresholds: Fed/macro news goes stale faster
        than culture/politics news.

        Args:
            result: The news result to check
            max_age_days: Default maximum age in days
            category: Market category for threshold override
            fetch_timestamp: Monotonic timestamp when this article was first seen.
                When the article date cannot be parsed, this is used as a fallback:
                if fetch_timestamp is provided and the article has been in the system
                for >7 days, it is considered stale (conservative default).
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
        # Try parsing absolute dates (M-13: includes timezone-aware formats)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",     # ISO 8601 with timezone offset
            "%Y-%m-%d %H:%M:%S%z",      # ISO-like with space separator
            "%Y-%m-%d %H:%M:%S %Z",     # With timezone name (e.g., UTC)
            "%b %d, %Y %H:%M:%S %z",    # e.g., "Mar 15, 2026 14:30:00 +0000"
            "%Y-%m-%d",
            "%b %d, %Y",
            "%B %d, %Y",
            "%m/%d/%Y",
        ):
            try:
                from datetime import datetime, timezone
                parsed = datetime.strptime(result.date.strip()[:20], fmt).replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - parsed).days
                if age_days > effective_max:
                    return True
                return False
            except ValueError:
                continue
        # All date formats exhausted — apply conservative fallback.
        # If the article has been in the system for more than 7 days (determined
        # via fetch_timestamp), treat it as stale rather than risking injecting
        # outdated context into Claude's probability assessments.
        UNPARSEABLE_DATE_MAX_AGE_DAYS = 7
        if fetch_timestamp is not None:
            import time as _time
            age_in_system_days = (_time.monotonic() - fetch_timestamp) / 86400
            if age_in_system_days > UNPARSEABLE_DATE_MAX_AGE_DAYS:
                logger.info(
                    f"Could not parse date '{result.date}' for '{result.title[:50]}...' "
                    f"and article has been in system {age_in_system_days:.1f} days "
                    f"— marking stale (conservative policy)"
                )
                return True
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

        # L-7: Apply source trust multiplier
        source_lower = result.source.lower()
        trust_multiplier = SOURCE_TRUST_MULTIPLIERS.get(source_lower, 1.0)

        return min(1.0, (overlap + recency_bonus) * trust_multiplier)

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
                if not any(ct in content_type for ct in ("text/html", "application/xhtml", "application/json")):
                    return ""
                raw_text = resp.text
                content_type_lower = content_type.lower()
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
            logger.debug(f"Article fetch failed for {url}: {e}", exc_info=True)
            return ""
        except Exception as e:
            logger.warning(f"Unexpected error fetching article {url}: {e}", exc_info=True)
            return ""

        # Try JSON-LD articleBody extraction first (AMP / structured data)
        if "application/json" in content_type_lower:
            try:
                import json
                data = json.loads(raw_text)
                article_body = data.get("articleBody", "")
                if article_body:
                    return _truncate_at_sentence(article_body, MAX_ARTICLE_CHARS)
            except (json.JSONDecodeError, AttributeError):
                pass
            return ""

        html = raw_text

        # Extract text using stdlib HTML parser (robust, not regex)
        text = _extract_text_from_html(html)
        if not text:
            return ""

        # M-12: Reject articles with fewer than 50 words (likely nav/ad fragments)
        if len(text.split()) < 50:
            logger.debug(f"Article too short ({len(text.split())} words): {url[:80]}")
            return ""

        # Extract sentences (>40 chars) for quality content
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 40]
        if not sentences:
            return ""

        # Include first 2 sentences (headline/lede) + body
        first_part = " ".join(sentences[:2])
        start = min(3, len(sentences) // 4)
        middle_part = " ".join(sentences[start:])
        combined = first_part + " " + middle_part
        # Truncate at sentence boundary rather than mid-sentence
        return _truncate_at_sentence(combined, MAX_ARTICLE_CHARS)

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
            logger.debug(f"Article enrichment failed: {e}", exc_info=True)

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
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
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
