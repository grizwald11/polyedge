"""Data enricher — aggregates context from multiple data sources for Claude.

Central coordinator that fetches news, economic data, community forecasts,
and cross-platform prices in parallel, then assembles a single context
string for the Claude forecaster.

Category-aware routing ensures only relevant sources are queried:
- FED_MACRO: news + FRED + Cleveland Fed + FedWatch + Metaculus + Polymarket
- EARNINGS: news + FRED + Metaculus + Polymarket
- All others: news + Metaculus + Polymarket
"""

from __future__ import annotations

import asyncio
import logging

from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.config import Settings
from src.core.models import Market, MarketCategory
from src.data.cleveland_fed import ClevelandFedNowcast
from src.data.fedwatch import FedWatchClient
from src.data.fred_client import FREDClient
from src.data.manifold_client import ManifoldClient
from src.data.metaculus_client import MetaculusClient
from src.data.polymarket_cross_ref import PolymarketCrossRef

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 5000  # ~1250 tokens


class DataEnricher:
    """Aggregates context from multiple data sources for Claude assessments."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # Cache slow-changing data sources to avoid redundant API calls.
        # TTLs: news (2 min — M-3: reduced from 5 min for faster news-reactive
        # trading; breaking news can move markets within minutes),
        # economic data (60 min), community forecasts (30 min).
        from src.data.cache import TTLCache
        self._news_cache = TTLCache(ttl_seconds=60)        # M-11: 1 min (was 2 min) for faster news-reactive trading
        self._econ_cache = TTLCache(ttl_seconds=3600)       # 60 min
        self._community_cache = TTLCache(ttl_seconds=1800)  # 30 min
        self.news_researcher = NewsResearcher(
            serper_api_key=settings.serper_api_key,
            searxng_url=settings.searxng_url,
            serper_url=settings.news.serper_url,
            staleness_thresholds=settings.news.staleness_thresholds,
        )
        self.fred = FREDClient(api_key=settings.fred_api_key)
        self.cleveland_fed = ClevelandFedNowcast(api_key=settings.fred_api_key)
        self.fedwatch = FedWatchClient(api_key=settings.fred_api_key)
        self.metaculus = MetaculusClient(api_token=settings.metaculus_api_token)
        self.manifold = ManifoldClient()
        self.polymarket = PolymarketCrossRef()
        from src.data.event_calendar import EventCalendar
        self.event_calendar = EventCalendar()

    async def get_context(self, market: Market) -> str:
        """Fetch and assemble enriched context from all relevant data sources.

        Runs all sources concurrently via asyncio.gather. Each source that
        fails returns "" (never blocks other sources). Assembles sections
        in order: news, economic data, community forecasts, cross-refs.

        Args:
            market: The market to enrich context for

        Returns:
            Assembled context string, always non-empty (at minimum returns
            "No additional context available.")
        """
        category = classify_market(market)

        # Build list of coroutines based on category, checking caches first
        tasks: dict[str, asyncio.Task] = {}
        results: dict[str, str] = {}

        def _check_or_fetch(name: str, cache: 'TTLCache', cache_key: str, coro):
            """Use cache if available, otherwise schedule the coroutine."""
            cached = cache.get(cache_key)
            if cached is not None:
                results[name] = cached
            else:
                tasks[name] = coro

        # News — always (short cache)
        _check_or_fetch("news", self._news_cache, f"news:{market.question[:80]}",
                        self.news_researcher.get_context(market.question))

        # Economic data — FED_MACRO and EARNINGS (long cache)
        if category in (MarketCategory.FED_MACRO, MarketCategory.EARNINGS):
            _check_or_fetch("fred", self._econ_cache, "fred:macro",
                            self.fred.get_macro_summary())

        # Cleveland Fed + FedWatch — FED_MACRO only (long cache)
        if category == MarketCategory.FED_MACRO:
            _check_or_fetch("cleveland_fed", self._econ_cache, "cleveland_fed",
                            self.cleveland_fed.get_context())
            _check_or_fetch("fedwatch", self._econ_cache, "fedwatch",
                            self.fedwatch.get_context())

        # Community forecasts and cross-platform — all categories (medium cache)
        _check_or_fetch("manifold", self._community_cache, f"manifold:{market.question[:80]}",
                        self.manifold.get_context(market.question))
        _check_or_fetch("metaculus", self._community_cache, f"metaculus:{market.question[:80]}",
                        self.metaculus.get_context(market.question))
        _check_or_fetch("polymarket", self._community_cache,
                        f"polymarket:{market.question[:80]}",
                        self.polymarket.get_context(market.question, market.yes_price))

        # Run remaining (non-cached) tasks concurrently with per-source timeouts
        # to prevent priority inversion (C-12). Higher-priority sources get more time.
        if not tasks:
            # Everything was cached
            pass
        else:
            TIER_TIMEOUTS = {
                "news": 6.0,
                "fred": 5.0,
                "cleveland_fed": 4.0,
                "fedwatch": 4.0,
                "manifold": 4.0,
                "metaculus": 4.0,
                "polymarket": 3.0,
            }

            async def _timed_task(coro, name, timeout):
                try:
                    return await asyncio.wait_for(coro, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"Data source '{name}' timed out after {timeout}s")
                    return (name, None)

            wrapped_tasks = [
                asyncio.create_task(
                    _timed_task(
                        self._safe_fetch(name, coro),
                        name,
                        TIER_TIMEOUTS.get(name, 5.0),
                    )
                )
                for name, coro in tasks.items()
            ]
            done, pending = await asyncio.wait(wrapped_tasks, timeout=10)

        # Collect results from completed tasks and store in cache
        if tasks:
            # Cache key mapping for storing results
            _cache_map = {
                "news": (self._news_cache, f"news:{market.question[:80]}"),
                "fred": (self._econ_cache, "fred:macro"),
                "cleveland_fed": (self._econ_cache, "cleveland_fed"),
                "fedwatch": (self._econ_cache, "fedwatch"),
                "manifold": (self._community_cache, f"manifold:{market.question[:80]}"),
                "metaculus": (self._community_cache, f"metaculus:{market.question[:80]}"),
                "polymarket": (self._community_cache, f"polymarket:{market.question[:80]}"),
            }
            for task in done:
                try:
                    name, result = task.result()
                    results[name] = result
                    # Cache non-empty results for future calls
                    if result and name in _cache_map:
                        cache, key = _cache_map[name]
                        cache.set(key, result)
                except Exception as e:
                    logger.warning(f"Data enrichment source failed: {e}")

            # Cancel any still-pending tasks and await them to ensure cleanup
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning(
                    f"Data enrichment: {len(pending)} sources timed out after 10s, "
                    f"{len(done)} completed"
                )

        # Assemble in priority order
        sections: list[str] = []

        # 1. News (highest priority)
        if results.get("news"):
            sections.append(results["news"])

        # 2. Economic data
        if results.get("fred"):
            sections.append(results["fred"])
        if results.get("cleveland_fed"):
            sections.append(results["cleveland_fed"])
        if results.get("fedwatch"):
            sections.append(results["fedwatch"])

        # 2b. Event calendar (synchronous — no API call needed)
        try:
            event_context = self.event_calendar.get_context_string(market)
            if event_context:
                sections.append(event_context)
        except Exception as e:
            logger.debug(f"Event calendar context failed: {e}")

        # 3. Community forecasts
        if results.get("manifold"):
            sections.append(results["manifold"])
        if results.get("metaculus"):
            sections.append(results["metaculus"])

        # 4. Cross-platform (lowest priority — truncated first)
        if results.get("polymarket"):
            sections.append(results["polymarket"])

        if not sections:
            return "No additional context available."

        context = "\n\n".join(sections)

        # Truncate if over limit, removing lowest-priority sections first
        if len(context) > MAX_CONTEXT_CHARS:
            context = self._truncate(sections)

        source_names = [name for name, val in results.items() if val]
        logger.info(
            f"Data enrichment for '{market.question[:50]}...': "
            f"sources={source_names}, length={len(context)} chars"
        )

        return context

    def _truncate(self, sections: list[str]) -> str:
        """Truncate context by removing shortest (least informative) sections first.

        Previously removed from the end (lowest priority), but this could
        drop cross-platform pricing data that is the strongest mispricing evidence.
        Now removes the shortest section first to preserve the meatiest content.
        """
        result_sections = list(sections)
        while result_sections and len("\n\n".join(result_sections)) > MAX_CONTEXT_CHARS:
            # Remove shortest section (least informative)
            shortest_idx = min(range(len(result_sections)), key=lambda i: len(result_sections[i]))
            removed = result_sections.pop(shortest_idx)
            logger.debug(f"Truncated section ({len(removed)} chars) to fit context limit")

        if not result_sections:
            # Even a single section is too long — hard truncate
            return sections[0][:MAX_CONTEXT_CHARS]

        return "\n\n".join(result_sections)

    @staticmethod
    async def _safe_fetch(name: str, coro) -> tuple[str, str]:
        """Run a coroutine safely, returning ("", ) on any exception."""
        try:
            result = await coro
            return (name, result or "")
        except Exception as e:
            logger.warning(f"Data source '{name}' failed: {e}")
            return (name, "")
