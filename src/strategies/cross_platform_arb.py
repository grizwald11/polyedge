"""Cross-platform arbitrage — detects pricing discrepancies between Kalshi and Polymarket.

When the same event is priced differently on both platforms, we can buy the
underpriced side for a risk-free (or low-risk) edge. Uses the existing
PolymarketCrossRef for market matching and caches validated pairs in the database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    Platform,
    Signal,
    StrategyName,
)
from src.data.polymarket_cross_ref import PolymarketCrossRef
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Minimum similarity score to consider markets as a pair
MIN_PAIR_SIMILARITY = 0.55


class CrossPlatformArbStrategy:
    """Detects cross-platform pricing discrepancies between Kalshi and Polymarket."""

    def __init__(self, settings: Settings, db: Database, cross_ref: Optional[PolymarketCrossRef] = None):
        self.settings = settings
        self.db = db
        self.cross_ref = cross_ref or PolymarketCrossRef(ttl_seconds=300)
        self._pair_cache: dict[str, dict] = {}  # kalshi_ticker -> poly match
        self._load_cached_pairs()

    def _load_cached_pairs(self):
        """Load previously discovered pairs from DB."""
        try:
            pairs = self.db.get_cross_platform_pairs()
            for p in pairs:
                self._pair_cache[p["kalshi_ticker"]] = {
                    "condition_id": p["poly_condition_id"],
                    "question": p["poly_question"],
                    "similarity": p["similarity"],
                }
            if pairs:
                logger.info(f"Cross-platform arb: loaded {len(pairs)} cached market pairs")
        except Exception as e:
            logger.debug(f"Could not load cached pairs: {e}")

    async def scan_for_opportunities(
        self,
        kalshi_markets: list[Market],
        poly_markets: list[Market],
    ) -> list[Signal]:
        """Scan for cross-platform arbitrage opportunities.

        Matches Kalshi markets to Polymarket markets and generates signals
        when the price discrepancy exceeds the minimum arbitrage edge.

        Args:
            kalshi_markets: Markets scanned from Kalshi
            poly_markets: Markets scanned from Polymarket

        Returns:
            List of arbitrage signals (one per underpriced side)
        """
        min_edge = self.settings.trading.min_edge_arb
        signals: list[Signal] = []

        # Build a lookup by condition_id for quick matching
        poly_by_id: dict[str, Market] = {m.ticker: m for m in poly_markets}

        # Check cached pairs first
        for kalshi_market in kalshi_markets:
            cached = self._pair_cache.get(kalshi_market.ticker)
            if cached and cached["condition_id"] in poly_by_id:
                poly_market = poly_by_id[cached["condition_id"]]
                arb_signals = self._check_price_discrepancy(
                    kalshi_market, poly_market, cached["similarity"], min_edge
                )
                signals.extend(arb_signals)

        # Try to discover new pairs for un-cached Kalshi markets
        uncached = [m for m in kalshi_markets if m.ticker not in self._pair_cache]
        # Limit discovery to top 20 markets per cycle to avoid API spam
        for kalshi_market in uncached[:20]:
            try:
                match = await self.cross_ref.get_best_match(kalshi_market.question)
                if match and match.get("condition_id"):
                    condition_id = match["condition_id"]
                    similarity = match.get("similarity", 0.0)

                    # Cache the pair
                    self._pair_cache[kalshi_market.ticker] = {
                        "condition_id": condition_id,
                        "question": match.get("question", ""),
                        "similarity": similarity,
                    }
                    self.db.upsert_cross_platform_pair(
                        kalshi_ticker=kalshi_market.ticker,
                        poly_condition_id=condition_id,
                        kalshi_question=kalshi_market.question,
                        poly_question=match.get("question", ""),
                        similarity=similarity,
                    )

                    # Check for arb if we have live Polymarket data
                    if condition_id in poly_by_id:
                        arb_signals = self._check_price_discrepancy(
                            kalshi_market, poly_by_id[condition_id],
                            similarity, min_edge
                        )
                        signals.extend(arb_signals)
                    elif match.get("yes_price") is not None:
                        # Use Gamma API price even without full scan data
                        arb_signals = self._check_price_discrepancy_from_ref(
                            kalshi_market, match, similarity, min_edge
                        )
                        signals.extend(arb_signals)

            except Exception as e:
                logger.debug(f"Cross-platform pair discovery failed for {kalshi_market.ticker}: {e}")

        if signals:
            logger.info(
                f"Cross-platform arb: {len(signals)} signals from "
                f"{len(self._pair_cache)} tracked pairs"
            )

        return signals

    def _check_price_discrepancy(
        self,
        kalshi: Market,
        poly: Market,
        similarity: float,
        min_edge: float,
    ) -> list[Signal]:
        """Check if a matched pair has an exploitable price discrepancy."""
        signals = []

        kalshi_yes = kalshi.yes_price
        poly_yes = poly.yes_price

        if kalshi_yes <= 0 or poly_yes <= 0:
            return signals

        edge = poly_yes - kalshi_yes  # Positive = Kalshi is cheaper for YES

        if abs(edge) < min_edge:
            return signals

        if edge > 0:
            # Kalshi YES is cheaper — buy YES on Kalshi
            signals.append(Signal(
                strategy=StrategyName.CROSS_PLATFORM_ARB,
                market_id=kalshi.ticker,
                platform=Platform.KALSHI,
                market_question=kalshi.question,
                direction=Direction.BUY_YES,
                edge=edge,
                probability_estimate=poly_yes,  # Use the other platform's price as estimate
                market_price=kalshi_yes,
                confidence=min(0.9, similarity),
                reasoning=(
                    f"Cross-platform arb: Kalshi YES={kalshi_yes:.2f} vs "
                    f"Polymarket YES={poly_yes:.2f} (edge={edge:.2f}, "
                    f"similarity={similarity:.2f})"
                ),
            ))
        else:
            # Polymarket YES is cheaper — buy YES on Polymarket
            signals.append(Signal(
                strategy=StrategyName.CROSS_PLATFORM_ARB,
                market_id=poly.ticker,
                platform=Platform.POLYMARKET,
                market_question=poly.question,
                direction=Direction.BUY_YES,
                edge=abs(edge),
                probability_estimate=kalshi_yes,
                market_price=poly_yes,
                confidence=min(0.9, similarity),
                reasoning=(
                    f"Cross-platform arb: Polymarket YES={poly_yes:.2f} vs "
                    f"Kalshi YES={kalshi_yes:.2f} (edge={abs(edge):.2f}, "
                    f"similarity={similarity:.2f})"
                ),
            ))

        return signals

    def _check_price_discrepancy_from_ref(
        self,
        kalshi: Market,
        poly_ref: dict,
        similarity: float,
        min_edge: float,
    ) -> list[Signal]:
        """Check price discrepancy using Gamma API reference data (no full Market)."""
        signals = []

        kalshi_yes = kalshi.yes_price
        poly_yes = poly_ref.get("yes_price")

        if not poly_yes or kalshi_yes <= 0 or poly_yes <= 0:
            return signals

        edge = poly_yes - kalshi_yes

        if abs(edge) < min_edge:
            return signals

        if edge > 0:
            # Kalshi is cheaper
            signals.append(Signal(
                strategy=StrategyName.CROSS_PLATFORM_ARB,
                market_id=kalshi.ticker,
                platform=Platform.KALSHI,
                market_question=kalshi.question,
                direction=Direction.BUY_YES,
                edge=edge,
                probability_estimate=poly_yes,
                market_price=kalshi_yes,
                confidence=min(0.8, similarity),  # Lower confidence without full scan data
                reasoning=(
                    f"Cross-platform arb (ref): Kalshi YES={kalshi_yes:.2f} vs "
                    f"Polymarket YES={poly_yes:.2f} (edge={edge:.2f})"
                ),
            ))

        return signals
