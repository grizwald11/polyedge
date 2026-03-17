"""Market discovery — fetches and parses markets from Kalshi API.

Replaces the old Gamma API client. Uses Kalshi's GET /markets endpoint
for market discovery. No separate discovery API needed — Kalshi uses
a unified API.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from src.core.kalshi_client import KalshiClient
from src.core.models import Market, MarketCategory, MarketToken, cents_to_dollars

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
    """Parse a raw Kalshi API market response into a Market model.

    Kalshi market response fields:
    - ticker: unique market identifier
    - title/question: market question text
    - subtitle: additional context
    - yes_bid, yes_ask, no_bid, no_ask: prices in cents
    - volume, volume_24h: trading volume
    - open_interest: current open interest
    - close_time: when the market closes
    - status: "open", "closed", "settled"
    - result: "yes", "no", "" (empty if unresolved)
    - event_ticker: parent event
    - category: Kalshi's category string
    """
    try:
        ticker = raw.get("ticker", "")
        if not ticker:
            return None

        question = raw.get("title") or raw.get("question", "")
        subtitle = raw.get("subtitle", "")
        full_text = f"{question} {subtitle}".strip()

        # Use Kalshi's category + our keyword matching
        kalshi_category = raw.get("category", "")
        tags = [kalshi_category] if kalshi_category else []

        # Build tokens from Kalshi's price data
        # Kalshi prices are in cents (1-99)
        yes_bid = raw.get("yes_bid", 0) or 0
        yes_ask = raw.get("yes_ask", 0) or 0
        no_bid = raw.get("no_bid", 0) or 0
        no_ask = raw.get("no_ask", 0) or 0

        # Use midpoint of bid/ask, or last traded price
        last_price = raw.get("last_price", 0) or 0
        yes_price_cents = last_price if last_price > 0 else (
            (yes_bid + yes_ask) / 2 if (yes_bid and yes_ask) else yes_bid or yes_ask
        )
        no_price_cents = 100 - yes_price_cents if yes_price_cents > 0 else (
            (no_bid + no_ask) / 2 if (no_bid and no_ask) else no_bid or no_ask
        )

        tokens = [
            MarketToken(
                token_id=f"{ticker}_yes",
                outcome="Yes",
                price=cents_to_dollars(yes_price_cents),
            ),
            MarketToken(
                token_id=f"{ticker}_no",
                outcome="No",
                price=cents_to_dollars(no_price_cents),
            ),
        ]

        # Parse close time
        end_date = None
        close_time = raw.get("close_time") or raw.get("expiration_time")
        if close_time:
            try:
                end_date = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Volume
        volume_24h = 0.0
        try:
            volume_24h = float(raw.get("volume_24h") or raw.get("volume24hr") or 0)
        except (ValueError, TypeError):
            pass

        volume_total = 0.0
        try:
            volume_total = float(raw.get("volume") or 0)
        except (ValueError, TypeError):
            pass

        liquidity = 0.0
        try:
            liquidity = float(raw.get("open_interest") or raw.get("liquidity") or 0)
        except (ValueError, TypeError):
            pass

        # Spread
        spread = 0.0
        if yes_bid and yes_ask:
            spread = cents_to_dollars(yes_ask - yes_bid)

        # Status
        status_str = raw.get("status", "open")
        active = status_str == "open"
        closed = status_str in ("closed", "settled")

        category = classify_market_category(full_text, tags)

        return Market(
            ticker=ticker,
            question=question,
            description=raw.get("rules_primary", "") or raw.get("description", ""),
            category=category,
            tags=tags,
            tokens=tokens,
            end_date=end_date,
            volume_24h=volume_24h,
            volume_total=volume_total,
            liquidity=liquidity,
            spread=spread,
            active=active,
            closed=closed,
            resolution_source=raw.get("settlement_source_url", ""),
            slug=raw.get("ticker", ""),
            subtitle=subtitle,
            event_ticker=raw.get("event_ticker", ""),
            result=raw.get("result", ""),
            status=status_str,
        )
    except Exception as e:
        logger.warning(f"Failed to parse market: {e}")
        return None


class MarketDiscovery:
    """Client for Kalshi market discovery using the unified API."""

    def __init__(self, kalshi: KalshiClient):
        self.kalshi = kalshi

    async def get_all_active_markets(self, max_pages: int = 20) -> list[dict]:
        """Fetch ALL active markets with cursor-based pagination."""
        all_markets = []
        cursor = None

        for page in range(max_pages):
            data = await self.kalshi.get_markets(limit=200, cursor=cursor, status="open")
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break

        logger.info(f"Fetched {len(all_markets)} active markets across {page + 1} pages")
        return all_markets

    async def get_market_by_ticker(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker."""
        return await self.kalshi.get_market(ticker)

    async def get_events(self, limit: int = 100, cursor: Optional[str] = None) -> list[dict]:
        """Fetch events (which contain multiple related markets)."""
        data = await self.kalshi.get_events(limit=limit, cursor=cursor)
        return data.get("events", [])

    async def close(self):
        await self.kalshi.close()
