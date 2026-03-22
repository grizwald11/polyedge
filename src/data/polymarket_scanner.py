"""Polymarket market scanner — discovers, filters, ranks, and stores qualifying markets.

Mirrors the Kalshi MarketScanner but uses PolymarketDiscovery and
parse_polymarket_market for Polymarket-specific data formats.
Polymarket event markets have zero fees, so no fee penalty in ranking.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import Settings
from src.core.polymarket_discovery import PolymarketDiscovery, parse_polymarket_market
from src.core.models import Market, MarketSnapshot
from src.storage.database import Database

logger = logging.getLogger(__name__)


class PolymarketScanner:
    """Scans Polymarket for qualifying trading opportunities."""

    def __init__(self, discovery: PolymarketDiscovery, db: Database, settings: Settings):
        self.discovery = discovery
        self.db = db
        self.settings = settings

    async def scan_all_markets(self) -> list[Market]:
        """Fetch all active markets from Polymarket Gamma API and parse."""
        raw_markets = await self.discovery.get_all_active_markets()
        markets = []
        for raw in raw_markets:
            market = parse_polymarket_market(raw)
            if market is not None:
                markets.append(market)

        logger.info(f"Polymarket: parsed {len(markets)} markets from {len(raw_markets)} raw records")
        return markets

    def filter_markets(self, markets: list[Market]) -> list[Market]:
        """Apply volume, category, and price filters."""
        cfg = self.settings.scanning
        filtered = []
        reasons = {"inactive": 0, "volume": 0, "excluded_cat": 0, "not_binary": 0, "no_price": 0, "invalid_prices": 0}

        for m in markets:
            if not m.active or m.closed:
                reasons["inactive"] += 1
                continue

            # Volume filter — Polymarket volumes are in dollars
            effective_volume = m.volume_24h if m.volume_24h > 0 else m.volume_total
            # Polymarket volumes are typically larger, use same threshold as Kalshi
            if effective_volume < cfg.min_volume_24h:
                reasons["volume"] += 1
                continue

            # Category filter
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

            if not m.is_binary:
                reasons["not_binary"] += 1
                continue

            if not m.yes_token or not m.no_token:
                reasons["no_price"] += 1
                continue

            if m.yes_price <= 0 and m.no_price <= 0:
                reasons["no_price"] += 1
                continue

            # Polymarket price sums can deviate more than Kalshi
            price_sum = m.yes_price + m.no_price
            if price_sum < 0.90 or price_sum > 1.10:
                reasons["invalid_prices"] += 1
                continue

            filtered.append(m)

        logger.info(
            f"Polymarket: filtered {len(markets)} → {len(filtered)} markets "
            f"(rejected: {reasons})"
        )
        return filtered

    def rank_markets(self, markets: list[Market]) -> list[Market]:
        """Rank markets by opportunity score. No fee penalty for Polymarket."""
        scored = []
        target_cats = set(self.settings.scanning.target_categories)

        for m in markets:
            score = 0.0

            # Volume score (log-scaled, 0-40)
            vol = m.volume_24h if m.volume_24h > 0 else m.volume_total
            if vol > 0:
                score += min(40, math.log10(vol) * 10)

            # Spread score (0-20)
            price_sum = m.yes_price + m.no_price
            if price_sum > 0:
                deviation = abs(1.0 - price_sum)
                score += min(20, deviation * 200)

            # Time to resolution (0-20)
            days = m.days_to_resolution
            if days is not None:
                if 7 <= days <= 90:
                    score += 20
                elif 3 <= days < 7:
                    score += 10
                elif 90 < days <= 180:
                    score += 10
                elif days < 3:
                    score += 5
                else:
                    score += 5

            # Category boost (+10)
            for tag in m.tags:
                if tag in target_cats:
                    score += 10
                    break
            if m.category.value in target_cats:
                score += 10

            # Price extremity (0-10)
            mid_price = m.yes_price
            if 0.20 <= mid_price <= 0.80:
                score += 10
            elif 0.10 <= mid_price <= 0.90:
                score += 5

            # No fee penalty — Polymarket event markets are fee-free

            scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = [m for _, m in scored]

        if ranked:
            vol = ranked[0].volume_24h if ranked[0].volume_24h > 0 else ranked[0].volume_total
            logger.info(
                f"Polymarket top: [{ranked[0].category.value}] \"{ranked[0].question[:60]}\" "
                f"(vol={vol:,.0f}, yes={ranked[0].yes_price:.2f})"
            )

        return ranked[:self.settings.scanning.max_markets]

    def store_markets(self, markets: list[Market]):
        """Persist Polymarket markets and snapshots to database."""
        now = datetime.now(timezone.utc)
        stored = 0

        for m in markets:
            try:
                self.db.upsert_market(m)
                snapshot = MarketSnapshot(
                    market_id=m.ticker,
                    timestamp=now,
                    yes_price=m.yes_price,
                    no_price=m.no_price,
                    spread=m.spread,
                    volume_1h=0,
                    liquidity=m.liquidity,
                )
                self.db.log_snapshot(snapshot)
                stored += 1
            except Exception as e:
                logger.warning(f"Failed to store Polymarket market {m.ticker[:16]}: {e}")

        logger.info(f"Polymarket: stored {stored}/{len(markets)} markets with snapshots")

    async def run_scan_cycle(self) -> list[Market]:
        """Execute a full scan cycle: fetch → filter → rank → store."""
        logger.info("Starting Polymarket scan cycle...")
        all_markets = await self.scan_all_markets()
        filtered = self.filter_markets(all_markets)
        ranked = self.rank_markets(filtered)
        self.store_markets(ranked)

        logger.info(
            f"Polymarket scan complete: {len(all_markets)} total → "
            f"{len(filtered)} filtered → {len(ranked)} ranked and stored"
        )
        return ranked
