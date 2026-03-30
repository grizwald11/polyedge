"""Market discovery — fetches and parses markets from Kalshi API.

Uses the /events endpoint with with_nested_markets=true to get both
events and their markets in a single bulk fetch. This avoids the
/markets endpoint which only returns KXMVE parlay markets.

Kalshi API field format (as of March 2026):
- Prices: *_dollars fields as string ("0.4300")
- Volume: volume_fp, volume_24h_fp as string ("55642.00")
- Status: "active", "closed", "settled"
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from src.core.kalshi_client import KalshiClient
from src.core.models import Market, MarketCategory, MarketToken

logger = logging.getLogger(__name__)

# Kalshi event categories → our MarketCategory
KALSHI_CATEGORY_MAP: dict[str, MarketCategory] = {
    "Politics": MarketCategory.POLITICS,
    "Elections": MarketCategory.POLITICS,
    "Economics": MarketCategory.FED_MACRO,
    "Financials": MarketCategory.FED_MACRO,
    "World": MarketCategory.GEOPOLITICS,
    "Science and Technology": MarketCategory.TECH_AI,
    "Companies": MarketCategory.TECH_AI,
    "Entertainment": MarketCategory.CULTURE,
    "Social": MarketCategory.CULTURE,
    "Health": MarketCategory.OTHER,
    "Climate and Weather": MarketCategory.OTHER,
    "Crypto": MarketCategory.CRYPTO,
    "Sports": MarketCategory.SPORTS,
    "Transportation": MarketCategory.OTHER,
}

# Category keyword mapping for classification fallback
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


def _parse_dollar_str(value: Any) -> float:
    """Parse a Kalshi dollar string like '0.4300' to float."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _parse_prices(raw: dict[str, Any]) -> tuple[float, float, float]:
    """Extract (yes_price, no_price, spread) from a raw Kalshi market dict.

    Uses *_dollars fields (dollar strings) with midpoint fallback to last_price.
    Returns prices clamped to [0, 1] and spread >= 0.
    """
    yes_bid = _parse_dollar_str(raw.get("yes_bid_dollars") or raw.get("yes_bid"))
    yes_ask = _parse_dollar_str(raw.get("yes_ask_dollars") or raw.get("yes_ask"))
    no_bid = _parse_dollar_str(raw.get("no_bid_dollars") or raw.get("no_bid"))
    no_ask = _parse_dollar_str(raw.get("no_ask_dollars") or raw.get("no_ask"))
    last_price = _parse_dollar_str(raw.get("last_price_dollars") or raw.get("last_price"))

    # Determine YES price: prefer midpoint of bid/ask, fall back to last_price
    if yes_bid > 0 and yes_ask > 0:
        yes_price = (yes_bid + yes_ask) / 2
    elif last_price > 0:
        yes_price = last_price
    else:
        yes_price = yes_bid or yes_ask

    # NO price: complement of YES, or from bid/ask
    if yes_price > 0:
        no_price = 1.0 - yes_price
    elif no_bid > 0 and no_ask > 0:
        no_price = (no_bid + no_ask) / 2
    else:
        no_price = no_bid or no_ask

    # Clamp to valid range
    yes_price = max(0.0, min(1.0, round(yes_price, 4)))
    no_price = max(0.0, min(1.0, round(no_price, 4)))

    # Spread — negative spread (bid > ask) indicates stale/crossed orderbook
    spread = 0.0
    if yes_bid > 0 and yes_ask > 0:
        spread = round(yes_ask - yes_bid, 4)
        if spread < 0:
            ticker = raw.get("ticker", "?")
            logger.debug(f"Negative spread for {ticker}: bid={yes_bid}, ask={yes_ask} — orderbook crossed")
            spread = 0.0

    return yes_price, no_price, spread


def _parse_status(raw: dict[str, Any]) -> tuple[str, bool, bool]:
    """Normalize Kalshi market status to (status_str, active, closed).

    Known statuses: open, active, closed, halted, settled, finalized, determined.
    """
    status_str = raw.get("status", "active")
    known_statuses = {"open", "active", "closed", "halted", "settled", "finalized", "determined"}
    if status_str not in known_statuses:
        ticker = raw.get("ticker", "?")
        logger.debug(f"Unknown market status '{status_str}' for {ticker} — treating as inactive")
    # "halted" markets are non-tradeable (treated same as closed)
    active = status_str in ("open", "active")
    closed = status_str in ("closed", "halted", "settled", "finalized", "determined")
    return status_str, active, closed


def parse_market(raw: dict[str, Any], event_category: str = "") -> Optional[Market]:
    """Parse a raw Kalshi API market response into a Market model.

    Kalshi API returns prices as dollar strings (*_dollars fields)
    and volume as contract counts (*_fp fields).
    Status is "active" (not "open").
    """
    try:
        ticker = raw.get("ticker", "")
        if not ticker:
            return None

        question = raw.get("title") or raw.get("question", "")
        subtitle = raw.get("subtitle", "")
        full_text = f"{question} {subtitle}".strip()

        # Category: use event category mapping, fall back to keyword classification
        tags = [event_category] if event_category else []
        kalshi_category = event_category or raw.get("category", "")
        mapped_category = KALSHI_CATEGORY_MAP.get(kalshi_category)
        if mapped_category:
            category = mapped_category
        else:
            category = classify_market_category(full_text, tags)

        # Prices — delegated to helper
        yes_price, no_price, spread = _parse_prices(raw)

        tokens = [
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ]

        # Parse close time
        end_date = None
        close_time = raw.get("close_time") or raw.get("expiration_time")
        if close_time:
            try:
                end_date = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed to parse close_time '{close_time}': {e}")

        # Volume — Kalshi uses *_fp fields (contract counts as strings)
        volume_24h = _parse_dollar_str(raw.get("volume_24h_fp") or raw.get("volume_24h"))
        volume_total = _parse_dollar_str(raw.get("volume_fp") or raw.get("volume"))

        # Liquidity / open interest
        open_interest = _parse_dollar_str(raw.get("open_interest_fp") or raw.get("open_interest"))
        liquidity_dollars = _parse_dollar_str(raw.get("liquidity_dollars") or raw.get("liquidity"))
        liquidity = max(open_interest, liquidity_dollars)

        # Status — delegated to helper
        status_str, active, closed = _parse_status(raw)

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


# Kalshi event categories we want to trade
TARGET_EVENT_CATEGORIES = {
    "Politics", "Elections", "Economics", "Financials",
    "World", "Science and Technology", "Companies",
    "Entertainment", "Social",
}


class MarketDiscovery:
    """Client for Kalshi market discovery.

    Uses /events?with_nested_markets=true to fetch events with their
    markets embedded. This is the only reliable way to get non-parlay
    markets (the /markets endpoint only returns KXMVE parlays).
    """

    def __init__(self, kalshi: KalshiClient):
        self.kalshi = kalshi

    async def get_all_active_markets(self, max_pages: int = 10) -> list[dict]:
        """Fetch all active markets via events with nested markets.

        1. Paginate through /events?with_nested_markets=true
        2. Filter events to target categories
        3. Extract and return market dicts tagged with event category
        """
        all_markets = []
        events_seen = 0
        events_targeted = 0
        cursor = None
        page = -1

        for page in range(max_pages):
            params: dict[str, Any] = {
                "limit": 200,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor

            data = await self.kalshi._request("GET", "/events", params=params)
            if data is None:
                break

            events = data.get("events", [])
            if not events:
                break

            for event in events:
                events_seen += 1
                event_category = event.get("category", "")

                # Filter to target categories
                if event_category not in TARGET_EVENT_CATEGORIES:
                    continue

                events_targeted += 1
                nested_markets = event.get("markets", [])
                for m in nested_markets:
                    m["_event_category"] = event_category
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        if page >= max_pages - 1 and cursor:
            logger.warning(
                f"Market discovery hit page limit ({max_pages} pages, {len(all_markets)} markets). "
                f"Some markets may be missing. Consider increasing max_pages."
            )

        logger.info(
            f"Fetched {len(all_markets)} markets from "
            f"{events_targeted}/{events_seen} target events "
            f"across {page + 1} pages"
        )
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
