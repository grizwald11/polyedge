"""Mean reversion strategy — fade sharp price moves.

Detects markets that moved >10% in the last 2 hours and takes the opposite
side with a small position (1-2% bankroll). Auto-closes within 4 hours.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.core.models import (
    Direction,
    Market,
    Signal,
    StrategyName,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Strategy parameters
MIN_PRICE_MOVE_PCT = 0.10       # 10% minimum move to trigger
LOOKBACK_HOURS = 2              # Hours to measure move
MAX_HOLD_HOURS = 4              # Auto-close after this
MAX_POSITION_PCT = 0.02         # 2% of bankroll per position
MIN_SNAPSHOTS = 3               # Minimum snapshots in window to trust signal
MIN_VOLUME_24H = 20000          # Minimum 24h volume
MIN_PRICE = 0.10                # Minimum price (avoid penny markets)
MAX_PRICE = 0.90                # Maximum price (avoid near-certain markets)
MAX_CONCURRENT_POSITIONS = 3    # Cap on simultaneous mean-reversion positions


class MeanReversionStrategy:
    """Fade sharp intraday price moves."""

    def __init__(self, settings, db: Database):
        self.settings = settings
        self.db = db
        self._active_entries: dict[str, datetime] = {}  # market_id -> entry_time

    def generate_signals(self, markets: list[Market]) -> list[Signal]:
        """Scan markets for mean reversion opportunities.

        Args:
            markets: Pre-filtered active markets with snapshots in DB.

        Returns:
            List of signals for markets with sharp recent moves.
        """
        signals = []

        if len(self._active_entries) >= MAX_CONCURRENT_POSITIONS:
            logger.debug(
                f"Mean reversion: at max concurrent positions ({MAX_CONCURRENT_POSITIONS})"
            )
            return signals

        now = datetime.now(timezone.utc)
        lookback_start = (now - timedelta(hours=LOOKBACK_HOURS)).isoformat()

        for market in markets:
            if market.ticker in self._active_entries:
                continue  # Already in this market

            signal = self._check_market(market, lookback_start, now)
            if signal:
                signals.append(signal)

                if len(signals) + len(self._active_entries) >= MAX_CONCURRENT_POSITIONS:
                    break

        logger.info(
            f"Mean reversion: scanned {len(markets)} markets, "
            f"found {len(signals)} signals"
        )
        return signals

    def _check_market(
        self,
        market: Market,
        lookback_start: str,
        now: datetime,
    ) -> Optional[Signal]:
        """Check if a market qualifies for mean reversion."""
        # Basic filters
        yes_price = market.yes_price
        if not (MIN_PRICE <= yes_price <= MAX_PRICE):
            return None
        if market.volume_24h < MIN_VOLUME_24H:
            return None

        # Get snapshots from lookback window
        snapshots = self.db.get_snapshots_for_market(
            market.ticker,
            start=lookback_start,
        )

        if len(snapshots) < MIN_SNAPSHOTS:
            return None

        # Calculate price move
        oldest_price = snapshots[0].get("yes_price", 0.0)
        newest_price = snapshots[-1].get("yes_price", 0.0)

        if oldest_price <= 0:
            return None

        price_move = newest_price - oldest_price
        price_move_pct = abs(price_move) / oldest_price

        if price_move_pct < MIN_PRICE_MOVE_PCT:
            return None

        # Verify the move is consistent (not just noise between snapshots)
        # Check that the oldest and newest are at opposite extremes
        all_prices = [s.get("yes_price", 0.0) for s in snapshots if s.get("yes_price", 0.0) > 0]
        if not all_prices:
            return None

        price_range = max(all_prices) - min(all_prices)
        if price_range < MIN_PRICE_MOVE_PCT * oldest_price:
            return None

        # Direction: fade the move (take opposite side)
        if price_move > 0:
            # Price went UP → buy NO (expect reversion down)
            direction = Direction.BUY_NO
            edge = price_move_pct * 0.5  # Estimate half the move reverts
        else:
            # Price went DOWN → buy YES (expect reversion up)
            direction = Direction.BUY_YES
            edge = price_move_pct * 0.5

        # Estimate probability of reversion
        # Higher moves have higher reversion probability up to a point
        reversion_confidence = min(0.7, 0.4 + price_move_pct)

        logger.info(
            f"Mean reversion signal: {market.ticker} moved {price_move_pct:.1%} "
            f"in {LOOKBACK_HOURS}h ({oldest_price:.2f}→{newest_price:.2f}), "
            f"direction={direction.value}, edge={edge:.3f}"
        )

        return Signal(
            strategy=StrategyName.MEAN_REVERSION,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=round(edge, 4),
            probability_estimate=round(
                1.0 - newest_price if direction == Direction.BUY_NO else newest_price + edge,
                4,
            ),
            market_price=newest_price,
            confidence=round(reversion_confidence, 2),
            reasoning=(
                f"Mean reversion: price moved {price_move_pct:.1%} in {LOOKBACK_HOURS}h "
                f"({oldest_price:.2f}→{newest_price:.2f}). "
                f"Fading with {direction.value}."
            ),
        )

    def record_entry(self, market_id: str):
        """Record that we entered a mean reversion position."""
        self._active_entries[market_id] = datetime.now(timezone.utc)

    def get_exit_candidates(self) -> list[str]:
        """Return market_ids of positions that should be auto-closed (>4h old)."""
        now = datetime.now(timezone.utc)
        max_hold = timedelta(hours=MAX_HOLD_HOURS)
        expired = [
            mid for mid, entry_time in self._active_entries.items()
            if now - entry_time > max_hold
        ]
        return expired

    def record_exit(self, market_id: str):
        """Record that we exited a mean reversion position."""
        self._active_entries.pop(market_id, None)

    @property
    def active_count(self) -> int:
        return len(self._active_entries)
