"""Search backends for news research — DuckDuckGo, Serper, SearxNG.

Extracted from news_researcher.py — provides search functions that return
NewsResult objects from different search providers.

L-2 NOTE: DuckDuckGo search uses the unofficial `ddgs` library which has no
stable API contract. It may be rate-limited or blocked without warning.
Serper is the preferred backend for production reliability.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

from src.analysis.news_fetcher import _extract_source

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


SERPER_SEARCH_URL = "https://google.serper.dev/search"
MAX_RESULTS_PER_QUERY = 5


@dataclass
class NewsResult:
    """A single news search result."""
    title: str
    snippet: str
    source: str
    date: str
    url: str


async def search_ddg(query: str) -> list[NewsResult]:
    """Search via DuckDuckGo using duckduckgo-search library.

    Uses the news endpoint for recency, falls back to text search.
    Runs synchronous DDGS in a thread to avoid blocking the event loop.

    M-9: Retry logic — DDG calls are retried up to 2 times with backoff
    via retry_with_backoff. Serper calls (search_serper below) already have
    retry_with_backoff. The caller (NewsResearcher._search_serper) manages
    a 3-strike permanent disable state machine for auth failures.
    """
    import asyncio
    from src.core.retry_helper import retry_with_backoff

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

    async def _run_ddg_search() -> list[NewsResult]:
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _do_search),
            timeout=8.0,
        )

    def _on_ddg_retry(attempt: int, exc: BaseException) -> None:
        logger.debug(
            f"M-9: DDG search retry {attempt + 1}/2 for '{query[:50]}': "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        return await retry_with_backoff(
            _run_ddg_search,
            max_retries=2,
            base_delay=1.0,
            max_delay=5.0,
            retryable_exceptions=(asyncio.TimeoutError, OSError, RuntimeError),
            on_retry=_on_ddg_retry,
        )
    except (asyncio.TimeoutError, OSError, RuntimeError):
        # M-9: All retries exhausted — DDG is down, caller falls back to Serper
        logger.warning(
            f"M-9: DDG search retries exhausted for '{query[:50]}' — "
            "falling back to Serper if available"
        )
        return []


class _SerperNonRetryable(Exception):
    """Raised inside retry loop to signal a non-retryable Serper error."""


async def search_serper(
    query: str,
    serper_api_key: str,
    serper_url: str = SERPER_SEARCH_URL,
) -> list[NewsResult]:
    """Search via Serper.dev (paid fallback).

    Uses shared retry_with_backoff for 5xx / network errors (M-12).
    Auth (400/401/403) and rate-limit (429) errors are handled
    separately since they have distinct state-machine logic.

    Returns:
        List of NewsResult on success.

    Raises:
        _SerperNonRetryable: For auth/rate-limit errors (caller handles state machine).
        httpx.HTTPStatusError: For 5xx errors after retries exhausted.
        httpx.HTTPError: For network errors after retries exhausted.
    """
    from src.core.retry_helper import retry_with_backoff

    async def _do_serper_request() -> dict:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    serper_url,
                    json={"q": query, "num": MAX_RESULTS_PER_QUERY},
                    headers={
                        "X-API-KEY": serper_api_key,
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429 or status in (400, 401, 403):
                raise _SerperNonRetryable(str(status)) from e
            raise

    def _on_retry(attempt: int, exc: BaseException) -> None:
        safe_err = str(exc)
        if serper_api_key and serper_api_key in safe_err:
            safe_err = safe_err.replace(serper_api_key, "***REDACTED***")
        logger.debug(
            f"Serper error, retrying (attempt {attempt + 1}/2): {safe_err}"
        )

    data = await retry_with_backoff(
        _do_serper_request,
        max_retries=2,
        base_delay=1.0,
        max_delay=30.0,
        retryable_exceptions=(httpx.HTTPStatusError, httpx.HTTPError),
        on_retry=_on_retry,
    )

    return parse_serper_response(data)


def parse_serper_response(data: dict) -> list[NewsResult]:
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
