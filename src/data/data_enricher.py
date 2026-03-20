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
from typing import Optional

from src.config import Settings
from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.core.models import Market, MarketCategory
from src.data.fred_client import FREDClient
from src.data.cleveland_fed import ClevelandFedNowcast
from src.data.fedwatch import FedWatchClient
from src.data.manifold_client import ManifoldClient
from src.data.metaculus_client import MetaculusClient
from src.data.polymarket_cross_ref import PolymarketCrossRef

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 5000  # ~1250 tokens


class DataEnricher:
    """Aggregates context from multiple data sources for Claude assessments."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.news_researcher = NewsResearcher(
            serper_api_key=settings.serper_api_key,
            searxng_url=settings.searxng_url,
        )
        self.fred = FREDClient(api_key=settings.fred_api_key)
        self.cleveland_fed = ClevelandFedNowcast()
        self.fedwatch = FedWatchClient()
        self.metaculus = MetaculusClient(api_token=settings.metaculus_api_token)
        self.manifold = ManifoldClient()
        self.polymarket = PolymarketCrossRef()

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

        # Build list of coroutines based on category
        tasks: dict[str, asyncio.Task] = {}

        # News — always
        tasks["news"] = self.news_researcher.get_context(market.question)

        # Economic data — FED_MACRO and EARNINGS
        if category in (MarketCategory.FED_MACRO, MarketCategory.EARNINGS):
            tasks["fred"] = self.fred.get_macro_summary()

        # Cleveland Fed + FedWatch — FED_MACRO only
        if category == MarketCategory.FED_MACRO:
            tasks["cleveland_fed"] = self.cleveland_fed.get_context()
            tasks["fedwatch"] = self.fedwatch.get_context()

        # Community forecasts and cross-platform — all categories
        tasks["manifold"] = self.manifold.get_context(market.question)
        tasks["metaculus"] = self.metaculus.get_context(market.question)
        tasks["polymarket"] = self.polymarket.get_context(
            market.question, market.yes_price
        )

        # Run all concurrently with a hard timeout, preserving partial results
        results: dict[str, str] = {}
        wrapped_tasks = [
            asyncio.create_task(self._safe_fetch(name, coro))
            for name, coro in tasks.items()
        ]
        done, pending = await asyncio.wait(wrapped_tasks, timeout=30)

        # Collect results from completed tasks
        for task in done:
            try:
                name, result = task.result()
                results[name] = result
            except Exception:
                pass  # _safe_fetch already handles exceptions

        # Cancel any still-pending tasks and await them to ensure cleanup
        if pending:
            for task in pending:
                task.cancel()
            # Gather with return_exceptions to suppress CancelledError and
            # ensure underlying HTTP connections are properly released.
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning(
                f"Data enrichment: {len(pending)} sources timed out after 30s, "
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
        """Truncate context by removing lowest-priority sections first.

        Priority (highest to lowest): news, fred, cleveland_fed, fedwatch,
        metaculus, polymarket. Removes from the end until under limit.
        """
        result_sections = list(sections)
        while result_sections and len("\n\n".join(result_sections)) > MAX_CONTEXT_CHARS:
            result_sections.pop()  # Remove lowest priority

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
