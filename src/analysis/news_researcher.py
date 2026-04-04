"""News researcher — enriches Claude's probability assessments with real-time context.

Uses DuckDuckGo search (free, no API key) via the duckduckgo-search library as the
primary backend, with Serper.dev as an optional paid fallback. Formats a concise
context block for injection into Claude prompts.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

from src.analysis.news_fetcher import (
    _normalize_url,
    enrich_with_article_text,
    fetch_article_text,
)
from src.analysis.news_search import (
    DDG_AVAILABLE,
    NewsResult,
    SERPER_SEARCH_URL,
    _SerperNonRetryable,
    search_ddg,
    search_serper,
    parse_serper_response,
)

logger = logging.getLogger(__name__)

MAX_QUERIES = 4
MAX_CONTEXT_CHARS = 4000  # ~1000 tokens — increased to reduce mid-article truncation
MAX_RELEVANT_RESULTS = 5
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
        staleness_thresholds: Optional[dict[str, int]] = None,
    ):
        self.serper_api_key = serper_api_key
        self.serper_url = serper_url
        self._staleness_thresholds = staleness_thresholds or {}
        # searxng_url kept for backward compatibility
        self.searxng_url = searxng_url
        self._serper_disabled = False  # Set True after credit/auth failures
        self._serper_disabled_at: float = 0.0  # Monotonic time of disable
        self._serper_cooldown_seconds: float = 3600.0  # Re-enable after 1 hour
        self._serper_auth_failure_count: int = 0  # Consecutive 4xx auth failures
        # M-12: Track the API key at time of permanent disable so we can
        # auto-recover if the key is rotated/changed.
        self._serper_key_at_disable: Optional[str] = None
        # M-6: Cache last successful context per market question for fallback
        # Bounded to prevent memory growth in 24/7 operation
        self._last_successful_context: dict[str, str] = {}
        self._last_successful_time: dict[str, float] = {}
        self._MAX_CONTEXT_CACHE = 500

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

        # H-3 FIX: Auto-recover permanently disabled Serper after 1 hour.
        # If 3+ auth failures were actually transient (network issue misclassified
        # as auth), we don't want to permanently lose the paid search backend.
        # After 1 hour, attempt a single probe query — if it succeeds, re-enable.
        SERPER_PERMANENT_PROBE_INTERVAL = 3600  # 1 hour
        if (
            self._serper_disabled
            and self._serper_disabled_at == float("inf")
            and self.serper_api_key
            and self._serper_key_at_disable == self.serper_api_key  # Key hasn't changed
        ):
            import time as _time
            last_probe = getattr(self, "_serper_last_probe_time", 0.0)
            if _time.monotonic() - last_probe >= SERPER_PERMANENT_PROBE_INTERVAL:
                self._serper_last_probe_time = _time.monotonic()
                logger.info("H-3: Probing Serper API after permanent disable (1h recovery attempt)")
                try:
                    probe_results = await self._search_serper("test probe query")
                    if probe_results is not None:  # Even empty list means API responded OK
                        logger.info("H-3: Serper probe succeeded — re-enabling API")
                        self._serper_disabled = False
                        self._serper_disabled_at = 0.0
                        self._serper_auth_failure_count = 0
                        self._serper_key_at_disable = None
                except Exception as probe_err:
                    logger.debug(f"H-3: Serper probe still failing: {probe_err}")

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
        """Search via DuckDuckGo — delegates to news_search module."""
        return await search_ddg(query)

    # Keep _SerperNonRetryable as a class attribute for backward compatibility
    _SerperNonRetryable = _SerperNonRetryable

    async def _search_serper(self, query: str) -> list[NewsResult]:
        """Search via Serper.dev (paid fallback).

        Wraps news_search.search_serper with the Serper state machine
        (auth failure tracking, cooldown, permanent disable).
        """
        try:
            results = await search_serper(
                query,
                serper_api_key=self.serper_api_key,
                serper_url=self.serper_url,
            )
        except _SerperNonRetryable as wrapper:
            # Unwrap the original HTTPStatusError for status-specific handling
            orig: httpx.HTTPStatusError = wrapper.__cause__  # type: ignore[assignment]
            status = orig.response.status_code
            if status == 429:
                logger.warning("Serper rate limited (429) — retries exhausted")
                return []
            # Auth errors (400/401/403): apply escalating disable logic
            try:
                detail = orig.response.json().get("message", str(status))
            except (ValueError, KeyError, AttributeError):
                detail = str(status)
            import time as _time
            self._serper_auth_failure_count += 1
            if self._serper_auth_failure_count >= 3:
                logger.critical(
                    f"Serper API PERMANENTLY DISABLED after "
                    f"{self._serper_auth_failure_count} consecutive auth failures "
                    f"({status}): {detail}. "
                    f"News quality degraded — using DuckDuckGo only. "
                    f"Fix: check SERPER_API_KEY, then call reset_serper() or restart."
                )
                self._serper_disabled = True
                self._serper_disabled_at = float("inf")
                self._serper_key_at_disable = self.serper_api_key
            elif self._serper_auth_failure_count >= 2:
                logger.warning(
                    f"Serper API auth failure #{self._serper_auth_failure_count} "
                    f"(1h cooldown): {detail}"
                )
                self._serper_disabled = True
                self._serper_disabled_at = _time.monotonic()
            else:
                logger.warning(
                    f"Serper API auth failure #{self._serper_auth_failure_count} "
                    f"(transient, not disabling yet): {detail}"
                )
            return []
        except (httpx.HTTPStatusError, httpx.HTTPError) as e:
            safe_err = str(e)
            if self.serper_api_key and self.serper_api_key in safe_err:
                safe_err = safe_err.replace(self.serper_api_key, "***REDACTED***")
            logger.warning(f"Serper search failed for '{query}': {safe_err}")
            return []

        # Success — reset auth failure counter
        if self._serper_auth_failure_count > 0:
            logger.info(
                f"Serper API call succeeded — resetting auth failure counter "
                f"(was {self._serper_auth_failure_count})"
            )
            self._serper_auth_failure_count = 0

        return results

    @staticmethod
    def _parse_serper_response(data: dict) -> list[NewsResult]:
        """Parse Serper API JSON response into NewsResult objects."""
        return parse_serper_response(data)

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
        # Use config thresholds if provided, fall back to built-in defaults
        default_thresholds = {
            "Fed": 5, "Fed_Macro": 5, "Fed/Macro": 5,
            "Geopolitics": 7,
            "Politics": 14,
            "Culture": 30,
            "Tech": 10, "Tech_AI": 10, "Tech/AI": 10,
        }
        # Config thresholds override defaults; also expand slash-form keys
        # (e.g., "Fed/Macro" → also sets "Fed" and "Fed_Macro")
        category_max_days = dict(default_thresholds)
        for key, val in self._staleness_thresholds.items():
            category_max_days[key] = val
            # Expand "Fed/Macro" → "Fed", "Fed_Macro" for legacy lookup
            if "/" in key:
                parts = key.split("/")
                category_max_days[parts[0]] = val
                category_max_days[key.replace("/", "_")] = val
        effective_max = category_max_days.get(category, max_age_days)
        if not result.date:
            return False  # No date — can't determine staleness, keep it
        date_lower = result.date.lower()
        # Check for obviously old relative dates
        weeks_match = re.search(r"(\d+)\s*week", date_lower)
        if weeks_match:
            weeks = int(weeks_match.group(1))
            if weeks * 7 > effective_max:
                return True
            return False  # Relative date parsed successfully and within threshold
        months_match = re.search(r"(\d+)\s*month", date_lower)
        if months_match:
            return True  # Any "X months ago" is too old
        days_match = re.search(r"(\d+)\s*day", date_lower)
        if days_match:
            days = int(days_match.group(1))
            if days > effective_max:
                return True
            return False  # Relative date parsed successfully and within threshold
        # Try parsing absolute dates (M-13: includes timezone-aware formats)
        from datetime import datetime, timezone
        date_str = result.date.strip()
        # Normalize 'Z' suffix to '+00:00' for strptime %z compatibility
        normalized = re.sub(r"Z$", "+00:00", date_str)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",     # ISO 8601: 2026-03-19T10:00:00+00:00
            "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO 8601 with fractional seconds
            "%Y-%m-%d %H:%M:%S%z",      # ISO-like with space separator
            "%Y-%m-%dT%H:%M:%S",        # ISO 8601 without timezone (assume UTC)
            "%Y-%m-%d %H:%M:%S",        # Datetime without timezone (assume UTC)
            "%b %d, %Y %H:%M:%S %z",    # e.g., "Mar 15, 2026 14:30:00 +0000"
            "%Y-%m-%d",
            "%b %d, %Y",
            "%B %d, %Y",
            "%m/%d/%Y",
        ):
            try:
                parsed = datetime.strptime(normalized, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
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
        # H-7 FIX: Articles with unparseable dates and no fetch_timestamp
        # are treated as stale by default. Previously they were kept indefinitely,
        # which risked injecting months-old context into Claude's assessments.
        logger.info(
            f"Could not parse date '{result.date}' for '{result.title[:50]}...' "
            f"— marking stale (H-7: no verifiable date, conservative policy)"
        )
        return True

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
        """Fetch and extract main text content from an article URL — delegates to news_fetcher."""
        return await fetch_article_text(url)

    async def _enrich_with_article_text(self, results: list[NewsResult]) -> list[NewsResult]:
        """Fetch full article text for top results and append to snippets."""
        return await enrich_with_article_text(results)

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
            # M-6: Fallback to cached context when all backends fail
            cache_key = market_question.strip().lower()
            if cache_key in self._last_successful_context:
                age = time.monotonic() - self._last_successful_time[cache_key]
                if age < 1800:  # 30 minutes
                    logger.warning(
                        f"Using cached news context ({age:.0f}s old) — "
                        f"all search backends unavailable"
                    )
                    return self._last_successful_context[cache_key]
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
        # M-6: Cache successful context for fallback (M-N4: capped at 1000 entries)
        cache_key = market_question.strip().lower()
        self._last_successful_context[cache_key] = context
        self._last_successful_time[cache_key] = time.monotonic()
        # Evict oldest entries if cache exceeds limit
        if len(self._last_successful_context) > self._MAX_CONTEXT_CACHE:
            oldest_key = min(self._last_successful_time, key=self._last_successful_time.get)  # type: ignore[arg-type]
            del self._last_successful_context[oldest_key]
            del self._last_successful_time[oldest_key]
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
