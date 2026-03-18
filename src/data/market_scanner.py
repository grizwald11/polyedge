"""Market scanner — discovers, filters, ranks, and stores qualifying markets.

Runs on a configurable interval (default 5 minutes). Fetches all active markets
from Kalshi via event-based discovery, applies volume/liquidity/category filters,
ranks by opportunity score, and persists to the database.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.market_discovery import MarketDiscovery, parse_market
from src.core.models import Market, MarketCategory, MarketSnapshot, kalshi_taker_fee, dollars_to_cents
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

            # Category filter — exclude blacklisted categories
            excluded = False
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

            # Price sum must be roughly 1.0 (reject corrupt/stale data)
            price_sum = m.yes_price + m.no_price
            if price_sum < 0.90 or price_sum > 1.10:
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

            # Volume score (log-scaled, normalized to 0-40 range)
            # Use effective volume (24h or total)
            vol = m.volume_24h if m.volume_24h > 0 else m.volume_total
            if vol > 0:
                score += min(40, math.log10(vol) * 10)

            # Spread score (wider spread = more potential mispricing, 0-20 range)
            price_sum = m.yes_price + m.no_price
            if price_sum > 0:
                deviation_from_one = abs(1.0 - price_sum)
                score += min(20, deviation_from_one * 200)

            # Time to resolution score (sweet spot: 7-90 days, 0-20 range)
            days = m.days_to_resolution
            if days is not None:
                if 7 <= days <= 90:
                    score += 20
                elif 3 <= days < 7:
                    score += 10
                elif 90 < days <= 180:
                    score += 10
                elif days < 3:
                    score += 5  # Too close to resolution, less time for edge
                else:
                    score += 5  # Very long-dated, capital locked up

            # Category boost (target categories get +10)
            for tag in m.tags:
                if tag in target_cats:
                    score += 10
                    break
            if m.category.value in target_cats:
                score += 10

            # Price extremity score (markets near 50% have more edge potential, 0-10)
            mid_price = m.yes_price
            if 0.20 <= mid_price <= 0.80:
                score += 10  # Most edge potential
            elif 0.10 <= mid_price <= 0.90:
                score += 5  # Some edge potential

            # Fee penalty: higher fees at mid-prices reduce attractiveness
            if mid_price > 0:
                fee_per_contract = 0.07 * mid_price * (1 - mid_price)
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
