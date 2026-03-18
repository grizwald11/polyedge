"""Whale tracker strategy — generates signals from whale consensus.

Monitors a basket of proven profitable traders and generates signals
when a supermajority (≥80%) agree on a market direction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import Settings
from src.core.models import (
    Direction, Market, Signal, StrategyName,
)
from src.data.whale_monitor import WhaleMonitor
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Don't follow whales into positions older than 48 hours
STALE_POSITION_HOURS = 48


class WhaleTrackerStrategy:
    """Generates trading signals from whale basket consensus."""

    def __init__(
        self,
        whale_monitor: WhaleMonitor,
        settings: Settings,
        db: Database,
    ):
        self.whale_monitor = whale_monitor
        self.settings = settings
        self.db = db
        # Whale consensus uses arb-level edge threshold since the signal
        # comes from proven trader agreement, not probability estimation
        self.min_edge = settings.trading.min_edge_arb

    def scan_for_opportunities(
        self, markets: list[Market]
    ) -> list[Signal]:
        """Check whale consensus on all tracked markets.

        Returns signals where whale basket has strong consensus.
        """
        if self.whale_monitor.basket_size == 0:
            return []

        signals: list[Signal] = []
        market_lookup = {m.ticker: m for m in markets}
        whale_markets = self.whale_monitor.get_all_markets_with_positions()

        for market_id in whale_markets:
            market = market_lookup.get(market_id)
            if market is None:
                continue

            consensus = self.whale_monitor.get_consensus(market_id)
            if consensus is None:
                continue

            # Skip stale positions (>48h old)
            if self._is_stale(consensus.earliest_entry):
                logger.debug(
                    f"Whale signal stale for {market_id}: "
                    f"earliest entry {consensus.earliest_entry}"
                )
                continue

            # Calculate edge based on whale entry vs current price
            signal = self._build_signal(consensus, market)
            if signal:
                signals.append(signal)

        if signals:
            logger.info(f"Whale tracker: {len(signals)} consensus signals")

        return signals

    def _build_signal(
        self, consensus, market: Market
    ) -> Optional[Signal]:
        """Build a Signal from a whale consensus.

        Edge is derived from whale avg entry price vs current market price.
        If whales bought YES at $0.55 and market is now $0.50, edge = $0.05
        (we can buy cheaper than proven profitable traders did).
        """
        if consensus.direction == Direction.BUY_YES:
            current_price = market.yes_price
            avg_entry = consensus.avg_entry_price
        else:
            current_price = market.no_price
            avg_entry = consensus.avg_entry_price

        # Edge is derived from the price difference between whale entry and
        # current market. If whales bought at $0.55 and market is now $0.50,
        # that's a $0.05 edge — we can enter cheaper than proven traders did.
        # If there's no price edge (we can't buy cheaper), there's no signal.
        if avg_entry <= 0:
            return None
        price_edge = avg_entry - current_price
        if price_edge <= 0:
            # We can't buy cheaper than the whales — no edge
            return None

        edge = price_edge
        # Whale entry price is our best estimate of true probability
        # (proven traders paid this price, implying they believe prob >= avg_entry)
        probability_estimate = min(0.99, avg_entry)

        if edge < self.min_edge:
            return None

        # Weight by timing: early entries get higher confidence
        confidence = self._timing_weight(consensus.earliest_entry) * consensus.consensus_pct

        return Signal(
            strategy=StrategyName.WHALE_TRACKER,
            market_id=market.ticker,
            market_question=market.question,
            direction=consensus.direction,
            edge=edge,
            probability_estimate=probability_estimate,
            market_price=current_price,
            confidence=min(1.0, confidence),
            reasoning=(
                f"Whale consensus: {consensus.whale_count}/{consensus.basket_size} "
                f"({consensus.consensus_pct:.0%}) agree on {consensus.direction.value}"
                f"{f', avg entry ${avg_entry:.2f} vs market ${current_price:.2f}' if avg_entry > 0 else ''}"
            ),
        )

    def _is_stale(self, earliest_entry: Optional[datetime]) -> bool:
        """Check if the whale positions are too old to follow."""
        if earliest_entry is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_POSITION_HOURS)
        return earliest_entry < cutoff

    def _timing_weight(self, earliest_entry: Optional[datetime]) -> float:
        """Weight signal by how early whales entered.

        Early entries (>24h before now) get higher weight.
        """
        if earliest_entry is None:
            return 0.5

        hours_ago = (datetime.now(timezone.utc) - earliest_entry).total_seconds() / 3600
        if hours_ago > 24:
            return 0.9  # Early entry — high confidence
        elif hours_ago > 12:
            return 0.7
        elif hours_ago > 6:
            return 0.5
        else:
            return 0.3  # Very recent — might be too late
