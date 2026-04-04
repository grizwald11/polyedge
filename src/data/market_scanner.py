"""Market scanner — discovers, filters, ranks, and stores qualifying markets.

Runs on a configurable interval (default 5 minutes). Fetches all active markets
from Kalshi via event-based discovery, applies volume/liquidity/category filters,
ranks by opportunity score, and persists to the database.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import Settings
from src.core.market_discovery import MarketDiscovery, parse_market
from src.core.models import (
    Market,
    MarketSnapshot,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)


class MarketScanner:
    """Scans Kalshi for qualifying trading opportunities."""

    def __init__(self, discovery: MarketDiscovery, db: Database, settings: Settings):
        self.discovery = discovery
        self.db = db
        self.settings = settings

    async def scan_all_markets(self) -> list[Market]:
        """Fetch all active markets from Kalshi API and parse into models."""
        raw_markets = await self.discovery.get_all_active_markets()
        markets = []
        for raw in raw_markets:
            event_category = raw.get("_event_category", "")
            market = parse_market(raw, event_category=event_category)
            if market is not None:
                markets.append(market)

        logger.info(f"Parsed {len(markets)} markets from {len(raw_markets)} raw records")
        return markets

    def filter_markets(self, markets: list[Market]) -> list[Market]:
        """Apply all configured filters to raw market list."""
        cfg = self.settings.scanning
        filtered = []
        reasons = {"inactive": 0, "volume": 0, "excluded_cat": 0, "not_binary": 0, "no_price": 0, "invalid_prices": 0}

        for m in markets:
            # Skip inactive or closed
            if not m.active or m.closed:
                reasons["inactive"] += 1
                continue

            # Volume filter — use volume_total as fallback since Kalshi
            # doesn't reliably populate volume_24h
            effective_volume = m.volume_24h if m.volume_24h > 0 else m.volume_total
            if effective_volume < cfg.min_volume_24h:
                reasons["volume"] += 1
                continue

            # Category filter — exclude blacklisted categories.
            # M-16: Also check question text and description, not just
            # category/tags, to catch markets that are miscategorized.
            excluded = False
            question_lower = m.question.lower()
            description_lower = (m.description or "").lower()
            for exc_cat in cfg.exclude_categories:
                exc_lower = exc_cat.lower()
                if exc_lower in m.category.value.lower():
                    excluded = True
                    break
                for tag in m.tags:
                    if exc_lower in tag.lower():
                        excluded = True
                        break
                if excluded:
                    break
                if exc_lower in question_lower or exc_lower in description_lower:
                    excluded = True
                    break
            if excluded:
                reasons["excluded_cat"] += 1
                continue

            # Must be binary (YES/NO) for our strategies
            if not m.is_binary:
                reasons["not_binary"] += 1
                continue

            # Must have tokens with prices
            if not m.yes_token or not m.no_token:
                reasons["no_price"] += 1
                continue

            # Must have some price data
            if m.yes_price <= 0 and m.no_price <= 0:
                reasons["no_price"] += 1
                continue

            # Price sum must be roughly 1.0 (reject corrupt/stale data).
            # Kalshi binary markets should always have YES + NO = 1.0.
            # Allow small tolerance for floating point and API rounding.
            price_sum = m.yes_price + m.no_price
            if price_sum < 0.95 or price_sum > 1.05:
                reasons["invalid_prices"] += 1
                continue

            filtered.append(m)

        logger.info(
            f"Filtered {len(markets)} → {len(filtered)} markets "
            f"(rejected: {reasons})"
        )
        return filtered

    def rank_markets(self, markets: list[Market]) -> list[Market]:
        """Rank markets by opportunity score (higher = more interesting).

        Score factors:
        - Volume: higher volume = more liquid, easier to trade
        - Spread: wider spread = more potential edge (but also more risk)
        - Time to resolution: moderate time horizons preferred (7-90 days)
        - Category priority: target categories get a boost
        - Fee-adjusted: factor in Kalshi fees when scoring
        """
        scored = []
        target_cats = set(self.settings.scanning.target_categories)

        for m in markets:
            score = 0.0

            # Volume score (log-scaled, normalized to 0-50 range)
            # Use effective volume (24h or total)
            vol = m.volume_24h if m.volume_24h > 0 else m.volume_total
            if vol > 0:
                score += min(50, math.log10(vol) * 12.5)

            # Time to resolution score (sweet spot: 7-90 days, 0-30 range)
            days = m.days_to_resolution
            if days is not None:
                if 7 <= days <= 90:
                    score += 30
                elif 3 <= days < 7:
                    score += 15
                elif 90 < days <= 180:
                    score += 15
                elif days < 3:
                    score += 5  # Too close to resolution, less time for edge
                else:
                    score += 5  # Very long-dated, capital locked up

            # Category boost (target categories get +10, no double-counting)
            has_target_cat = m.category.value in target_cats or any(
                tag in target_cats for tag in m.tags
            )
            if has_target_cat:
                score += 10

            # Price extremity score (extreme prices have more mispricing potential, 0-20)
            # Markets at <10% or >90% are where the biggest relative mispricings
            # occur — a $0.05 market moving to $0.08 is a 60% return, while a
            # $0.50 market moving to $0.53 is only 6%. Previous scoring (max 10)
            # under-weighted these opportunities vs volume.
            mid_price = m.yes_price
            if mid_price < 0.10 or mid_price > 0.90:
                score += 20  # Very extreme: highest mispricing potential
            elif mid_price < 0.15 or mid_price > 0.85:
                score += 12  # Extreme
            elif mid_price < 0.30 or mid_price > 0.70:
                score += 5  # Moderate extremity

            # Fee penalty: higher fees at mid-prices reduce attractiveness
            if mid_price > 0:
                fee_rate = 0.0175 if self.settings.trading.prefer_maker else 0.07
                fee_per_contract = fee_rate * mid_price * (1 - mid_price)
                score -= fee_per_contract * 20  # Small penalty

            scored.append((score, m))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        ranked = [m for _, m in scored]
        if ranked:
            vol = ranked[0].volume_24h if ranked[0].volume_24h > 0 else ranked[0].volume_total
            logger.info(
                f"Top market: [{ranked[0].category.value}] \"{ranked[0].question[:60]}\" "
                f"(vol={vol:,.0f}, yes={ranked[0].yes_price:.2f})"
            )

        return ranked[:self.settings.scanning.max_markets]

    def store_markets(self, markets: list[Market]):
        """Persist markets and snapshots to database."""
        now = datetime.now(timezone.utc)
        stored = 0

        for m in markets:
            try:
                # Upsert market
                self.db.upsert_market(m)

                # Log snapshot
                snapshot = MarketSnapshot(
                    market_id=m.ticker,
                    timestamp=now,
                    yes_price=m.yes_price,
                    no_price=m.no_price,
                    spread=m.spread,
                    volume_1h=0,  # Would need historical comparison
                    liquidity=m.liquidity,
                )
                self.db.log_snapshot(snapshot)
                stored += 1
            except Exception as e:
                logger.warning(f"Failed to store market {m.ticker}: {e}")

        logger.info(f"Stored {stored}/{len(markets)} markets with snapshots")

    async def run_scan_cycle(self) -> list[Market]:
        """Execute a full scan cycle: fetch → filter → rank → store."""
        logger.info("Starting market scan cycle...")

        # Fetch
        all_markets = await self.scan_all_markets()

        # Filter
        filtered = self.filter_markets(all_markets)

        # Rank
        ranked = self.rank_markets(filtered)

        # Store
        self.store_markets(ranked)

        logger.info(
            f"Scan cycle complete: {len(all_markets)} total → "
            f"{len(filtered)} filtered → {len(ranked)} ranked and stored"
        )

        return ranked

    def get_top_opportunities(self, n: int = 50) -> list[dict]:
        """Get top N markets from database by recent volume."""
        return self.db.get_active_markets()[:n]
