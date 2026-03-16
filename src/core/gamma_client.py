"""Gamma API client — market discovery and metadata (no auth required).

The Gamma API at gamma-api.polymarket.com provides:
- Active market listings with metadata
- Event groupings (events contain multiple markets)
- Market search and filtering
- No authentication needed — fully public
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from src.core.models import Market, MarketCategory, MarketToken

logger = logging.getLogger(__name__)

# Category keyword mapping for classification
CATEGORY_KEYWORDS: dict[MarketCategory, list[str]] = {
    MarketCategory.POLITICS: [
        "election", "president", "senate", "congress", "governor", "democrat",
        "republican", "gop", "vote", "primary", "nominee", "cabinet",
        "impeach", "attorney general", "secretary", "speaker", "midterm",
    ],
    MarketCategory.GEOPOLITICS: [
        "war", "invasion", "sanctions", "nato", "china", "russia", "iran",
        "ukraine", "israel", "ceasefire", "treaty", "military", "strike",
        "nuclear", "tariff", "trade war", "venezuela", "lebanon",
    ],
    MarketCategory.FED_MACRO: [
        "fed", "federal reserve", "interest rate", "inflation", "cpi",
        "gdp", "unemployment", "recession", "treasury", "yield", "rate cut",
        "rate hike", "fomc", "jobs report", "nonfarm", "pce",
    ],
    MarketCategory.TECH_AI: [
        "ai", "artificial intelligence", "openai", "gpt", "google", "apple",
        "microsoft", "meta", "tesla", "spacex", "ipo", "launch", "chatbot",
        "model", "benchmark", "chip", "semiconductor", "nvidia",
    ],
    MarketCategory.CULTURE: [
        "oscar", "grammy", "emmy", "award", "movie", "album", "viral",
        "celebrity", "elon", "musk", "tweet", "tiktok", "super bowl",
        "netflix", "spotify", "youtube",
    ],
    MarketCategory.CRYPTO: [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
        "token", "blockchain", "defi", "nft", "memecoin", "xrp",
    ],
    MarketCategory.SPORTS: [
        "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "hockey", "ufc", "boxing", "tennis", "golf",
        "champions league", "premier league", "ncaa", "serie a",
    ],
    MarketCategory.EARNINGS: [
        "earnings", "revenue", "eps", "quarterly", "annual report",
        "guidance", "profit", "beats", "misses",
    ],
}


def classify_market_category(question: str, tags: list[str]) -> MarketCategory:
    """Classify a market into a category based on question text and tags."""
    text = (question + " " + " ".join(tags)).lower()

    scores: dict[MarketCategory, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score

    if not scores:
        return MarketCategory.OTHER

    return max(scores, key=scores.get)


def parse_market(raw: dict[str, Any]) -> Optional[Market]:
    """Parse a raw Gamma API market response into a Market model."""
    try:
        condition_id = raw.get("conditionId") or raw.get("condition_id", "")
        if not condition_id:
            return None

        question = raw.get("question", "")
        tags = raw.get("tags", []) or []
        if isinstance(tags, str):
            tags = [tags]

        # Parse tokens
        tokens = []
        clob_token_ids = raw.get("clobTokenIds")
        outcomes = raw.get("outcomes")
        outcome_prices = raw.get("outcomePrices")

        if clob_token_ids and outcomes:
            if isinstance(clob_token_ids, str):
                # Sometimes returned as JSON string
                import json
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except (json.JSONDecodeError, TypeError):
                    clob_token_ids = []
            if isinstance(outcomes, str):
                import json
                try:
                    outcomes = json.loads(outcomes)
                except (json.JSONDecodeError, TypeError):
                    outcomes = []
            if isinstance(outcome_prices, str):
                import json
                try:
                    outcome_prices = json.loads(outcome_prices)
                except (json.JSONDecodeError, TypeError):
                    outcome_prices = []

            for i, (token_id, outcome) in enumerate(zip(clob_token_ids, outcomes)):
                price = 0.0
                if outcome_prices and i < len(outcome_prices):
                    try:
                        price = float(outcome_prices[i])
                    except (ValueError, TypeError):
                        pass
                tokens.append(MarketToken(
                    token_id=str(token_id),
                    outcome=str(outcome),
                    price=price,
                ))

        # Parse end date
        end_date = None
        end_str = raw.get("endDate") or raw.get("end_date_iso")
        if end_str:
            try:
                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Parse volume
        volume_24h = 0.0
        try:
            volume_24h = float(raw.get("volume24hr") or raw.get("volume_24hr") or 0)
        except (ValueError, TypeError):
            pass

        volume_total = 0.0
        try:
            volume_total = float(raw.get("volume") or 0)
        except (ValueError, TypeError):
            pass

        liquidity = 0.0
        try:
            liquidity = float(raw.get("liquidity") or 0)
        except (ValueError, TypeError):
            pass

        spread = 0.0
        try:
            spread = float(raw.get("spread") or 0)
        except (ValueError, TypeError):
            pass

        category = classify_market_category(question, tags)

        return Market(
            condition_id=condition_id,
            question=question,
            description=raw.get("description", ""),
            category=category,
            tags=tags,
            tokens=tokens,
            end_date=end_date,
            volume_24h=volume_24h,
            volume_total=volume_total,
            liquidity=liquidity,
            spread=spread,
            active=raw.get("active", True),
            closed=raw.get("closed", False),
            resolution_source=raw.get("resolutionSource", ""),
            slug=raw.get("slug", ""),
            neg_risk=raw.get("negRisk", False),
            event_slug=raw.get("eventSlug", ""),
        )
    except Exception as e:
        logger.warning(f"Failed to parse market: {e}")
        return None


class GammaClient:
    """Client for the Polymarket Gamma API (market discovery, no auth)."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com"):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """Make a GET request with retry logic."""
        client = await self._get_client()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await client.get(path, params=params)
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Rate limited on {path}, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Server error {e.response.status_code} on {path}, retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise
            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Request error on {path}: {e}, retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise
        return None

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        tag: Optional[str] = None,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict]:
        """Fetch markets from Gamma API."""
        params: dict[str, Any] = {
            "limit": min(limit, 100),  # API max is 100 per page
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag:
            params["tag"] = tag

        data = await self._get("/markets", params=params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        # Some endpoints return paginated {data: [...], next_cursor: ...}
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    async def get_all_active_markets(self, max_pages: int = 20) -> list[dict]:
        """Fetch ALL active markets with pagination."""
        all_markets = []
        offset = 0
        page_size = 100

        for page in range(max_pages):
            batch = await self.get_markets(limit=page_size, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < page_size:
                break  # Last page
            offset += page_size
            await asyncio.sleep(0.2)  # Be polite with rate limits

        logger.info(f"Fetched {len(all_markets)} active markets across {page + 1} pages")
        return all_markets

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        order: str = "volume",
        ascending: bool = False,
    ) -> list[dict]:
        """Fetch events (which contain multiple related markets)."""
        params: dict[str, Any] = {
            "limit": min(limit, 100),
            "offset": offset,
            "active": str(active).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        data = await self._get("/events", params=params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    async def get_market_by_condition(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by condition ID."""
        params = {"id": condition_id}
        data = await self._get("/markets", params=params)
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None

    async def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search markets by keyword."""
        params = {"query": query, "limit": limit}
        data = await self._get("/search", params=params)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return []
