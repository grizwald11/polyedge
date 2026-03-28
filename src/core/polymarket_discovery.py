"""Polymarket market discovery via Gamma API.

Fetches active markets from the Gamma API, parses them into the generic
Market model, and tags them with Platform.POLYMARKET.

No authentication needed for read-only market data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from src.core.models import (
    Market, MarketToken, MarketCategory, Platform, TokenOutcome,
)
from src.core.market_discovery import classify_market_category

logger = logging.getLogger(__name__)

# Polymarket tags → our MarketCategory
POLYMARKET_TAG_MAP: dict[str, MarketCategory] = {
    "Politics": MarketCategory.POLITICS,
    "Elections": MarketCategory.POLITICS,
    "US Politics": MarketCategory.POLITICS,
    "World": MarketCategory.GEOPOLITICS,
    "Geopolitics": MarketCategory.GEOPOLITICS,
    "Economics": MarketCategory.FED_MACRO,
    "Fed": MarketCategory.FED_MACRO,
    "Crypto": MarketCategory.CRYPTO,
    "Sports": MarketCategory.SPORTS,
    "Science & Tech": MarketCategory.TECH_AI,
    "AI": MarketCategory.TECH_AI,
    "Pop Culture": MarketCategory.CULTURE,
    "Entertainment": MarketCategory.CULTURE,
}


def _parse_outcome_prices(raw: dict) -> tuple[float, float]:
    """Extract YES and NO prices from Gamma API response.

    outcomePrices can be a JSON string like '["0.65","0.35"]' or a list.
    Falls back to bestBid/lastTradePrice if unavailable.
    """
    outcome_prices = raw.get("outcomePrices")
    yes_price = 0.0
    no_price = 0.0

    if isinstance(outcome_prices, str):
        try:
            prices = json.loads(outcome_prices)
            if len(prices) >= 2:
                yes_price = float(prices[0])
                no_price = float(prices[1])
            elif len(prices) == 1:
                yes_price = float(prices[0])
                no_price = 1.0 - yes_price
        except (ValueError, IndexError, json.JSONDecodeError):
            pass
    elif isinstance(outcome_prices, list) and outcome_prices:
        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 1.0 - yes_price

    if yes_price == 0.0:
        # Fallback to other price fields
        best_bid = raw.get("bestBid")
        if best_bid is not None:
            yes_price = float(best_bid)
            no_price = 1.0 - yes_price
        else:
            last_trade = raw.get("lastTradePrice")
            if last_trade is not None:
                yes_price = float(last_trade)
                no_price = 1.0 - yes_price

    # Bounds-check: prices must be in [0, 1]
    yes_price = max(0.0, min(1.0, yes_price))
    no_price = max(0.0, min(1.0, no_price))
    return yes_price, no_price


def parse_polymarket_market(raw: dict[str, Any]) -> Optional[Market]:
    """Parse a raw Gamma API market response into a Market model."""
    try:
        condition_id = raw.get("conditionId", "")
        if not condition_id:
            return None

        question = raw.get("question", "")
        if not question:
            return None

        yes_price, no_price = _parse_outcome_prices(raw)
        if yes_price <= 0 and no_price <= 0:
            return None

        # Token IDs from clobTokenIds (JSON string or list)
        clob_token_ids = raw.get("clobTokenIds")
        yes_token_id = ""
        no_token_id = ""
        if isinstance(clob_token_ids, str):
            try:
                ids = json.loads(clob_token_ids)
                yes_token_id = ids[0] if len(ids) > 0 else ""
                no_token_id = ids[1] if len(ids) > 1 else ""
            except (ValueError, IndexError, json.JSONDecodeError):
                pass
        elif isinstance(clob_token_ids, list):
            yes_token_id = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
            no_token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else ""

        tokens = [
            MarketToken(token_id=yes_token_id, outcome=TokenOutcome.YES, price=yes_price),
            MarketToken(token_id=no_token_id, outcome=TokenOutcome.NO, price=no_price),
        ]

        # Volume (Polymarket reports in dollars)
        volume = float(raw.get("volume", 0) or 0)
        volume_24h = float(raw.get("volume24hr", 0) or 0)

        # Liquidity
        liquidity = float(raw.get("liquidity", 0) or 0)

        # Spread
        spread = abs(yes_price + no_price - 1.0) if yes_price > 0 else 0.0

        # End date
        end_date = None
        end_date_str = raw.get("endDate") or raw.get("end_date_iso")
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Category classification
        tags_raw = raw.get("tags", [])
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except (ValueError, json.JSONDecodeError):
                tags_raw = [tags_raw]
        tags = tags_raw if isinstance(tags_raw, list) else []

        # Try tag mapping first, then keyword classification
        category = MarketCategory.OTHER
        for tag in tags:
            if tag in POLYMARKET_TAG_MAP:
                category = POLYMARKET_TAG_MAP[tag]
                break
        if category == MarketCategory.OTHER:
            category = classify_market_category(question, tags)

        # Status
        closed = raw.get("closed", False)
        active = raw.get("active", True) and not closed

        # Event grouping
        event_slug = raw.get("groupItemTitle", "") or raw.get("slug", "")

        return Market(
            ticker=condition_id,
            platform=Platform.POLYMARKET,
            question=question,
            description=raw.get("description", ""),
            category=category,
            tags=tags,
            tokens=tokens,
            end_date=end_date,
            volume_24h=volume_24h if volume_24h > 0 else volume,
            volume_total=volume,
            liquidity=liquidity,
            spread=spread,
            active=active,
            closed=closed,
            resolution_source=raw.get("resolutionSource", ""),
            slug=raw.get("slug", ""),
            event_ticker=event_slug,
        )

    except Exception as e:
        logger.debug(f"Failed to parse Polymarket market: {e}")
        return None


class PolymarketDiscovery:
    """Fetches active markets from Polymarket's Gamma API."""

    def __init__(self, gamma_host: str = "https://gamma-api.polymarket.com"):
        self.gamma_host = gamma_host.rstrip("/")

    async def get_all_active_markets(self, max_pages: int = 10) -> list[dict]:
        """Fetch all active markets via paginated Gamma API calls.

        Returns raw market dicts tagged for parsing.
        """
        all_markets: list[dict] = []
        limit = 100

        async with httpx.AsyncClient(timeout=30.0) as client:
            for page in range(max_pages):
                offset = page * limit
                try:
                    response = await client.get(
                        f"{self.gamma_host}/markets",
                        params={
                            "closed": "false",
                            "active": "true",
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPError as e:
                    logger.warning(f"Polymarket Gamma API page {page} failed: {e}")
                    break

                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    break

                all_markets.extend(markets)
                logger.debug(f"Polymarket page {page}: fetched {len(markets)} markets")

                if len(markets) < limit:
                    break  # Last page

        logger.info(
            f"Fetched {len(all_markets)} markets from Polymarket Gamma API "
            f"across {min(max_pages, (len(all_markets) // limit) + 1)} pages"
        )
        return all_markets

    async def get_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by condition ID."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.gamma_host}/markets",
                    params={"condition_id": condition_id},
                )
                response.raise_for_status()
                data = response.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                return markets[0] if markets else None
        except httpx.HTTPError as e:
            logger.warning(f"Polymarket market lookup failed for {condition_id}: {e}")
            return None
