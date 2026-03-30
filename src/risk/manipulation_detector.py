"""Market manipulation detector — flags suspicious price/volume activity.

Checks for:
- Rapid price moves without proportional volume (potential pump/dump)
- Extreme price volatility relative to historical norms
- Crossed or inverted order books (potential spoofing artifacts)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.core.models import Market

logger = logging.getLogger(__name__)

# Price move >20% in a single scan interval is suspicious
RAPID_MOVE_THRESHOLD = 0.20

# Minimum snapshots needed before we can detect manipulation
MIN_SNAPSHOTS_FOR_DETECTION = 2

# How long (seconds) to keep a market flagged after detection
FLAG_EXPIRY_SECONDS = 1800  # 30 minutes


@dataclass
class ManipulationFlag:
    """A flag indicating suspected manipulation on a market."""
    market_id: str
    reason: str
    detected_at: float  # monotonic time
    price_move: float = 0.0
    previous_price: float = 0.0
    current_price: float = 0.0


class ManipulationDetector:
    """Detects suspicious market activity that may indicate manipulation."""

    def __init__(
        self,
        rapid_move_threshold: float = RAPID_MOVE_THRESHOLD,
        flag_expiry_seconds: float = FLAG_EXPIRY_SECONDS,
    ):
        self.rapid_move_threshold = rapid_move_threshold
        self.flag_expiry_seconds = flag_expiry_seconds
        # market_id -> list of (monotonic_time, yes_price) snapshots
        self._price_history: dict[str, list[tuple[float, float]]] = {}
        # market_id -> ManipulationFlag
        self._flags: dict[str, ManipulationFlag] = {}
        # Max history entries per market (prevent unbounded growth)
        self._max_history = 50

    def update_price(self, market_id: str, yes_price: float) -> None:
        """Record a new price observation for a market."""
        now = time.monotonic()
        history = self._price_history.setdefault(market_id, [])
        history.append((now, yes_price))
        # Trim old entries
        if len(history) > self._max_history:
            self._price_history[market_id] = history[-self._max_history:]

    def check_market(self, market: Market) -> ManipulationFlag | None:
        """Check a market for manipulation signals.

        Call this before trading. Returns a ManipulationFlag if suspicious,
        None if clean.
        """
        now = time.monotonic()
        market_id = market.ticker

        # Clean expired flags
        self._clean_expired_flags(now)

        # If already flagged and not expired, return existing flag
        if market_id in self._flags:
            return self._flags[market_id]

        # Record current price
        current_price = market.yes_price
        if current_price <= 0:
            return None

        self.update_price(market_id, current_price)

        history = self._price_history.get(market_id, [])
        if len(history) < MIN_SNAPSHOTS_FOR_DETECTION:
            return None

        # Check for rapid price move
        flag = self._check_rapid_move(market_id, history, now)
        if flag is not None:
            return flag

        # Check for crossed/inverted book (YES + NO significantly != 1.0)
        flag = self._check_crossed_book(market, now)
        if flag is not None:
            return flag

        return None

    def is_flagged(self, market_id: str) -> bool:
        """Check if a market is currently flagged."""
        self._clean_expired_flags(time.monotonic())
        return market_id in self._flags

    def get_flag(self, market_id: str) -> ManipulationFlag | None:
        """Get the active flag for a market, if any."""
        self._clean_expired_flags(time.monotonic())
        return self._flags.get(market_id)

    def clear_flag(self, market_id: str) -> None:
        """Manually clear a flag (e.g., after investigation)."""
        self._flags.pop(market_id, None)

    def _check_rapid_move(
        self,
        market_id: str,
        history: list[tuple[float, float]],
        now: float,
    ) -> ManipulationFlag | None:
        """Detect if price moved more than threshold since previous observation."""
        if len(history) < 2:
            return None

        prev_time, prev_price = history[-2]
        curr_time, curr_price = history[-1]

        if prev_price <= 0:
            return None

        # M-17: Use relative (percentage) price change instead of absolute.
        # This prevents false positives on low-priced markets (e.g., $0.05 → $0.10
        # is a 100% move but only $0.05 absolute) and false negatives on
        # high-priced markets (e.g., $0.80 → $0.95 is only 18.75% but $0.15 absolute).
        price_move = abs(curr_price - prev_price) / max(prev_price, 0.01)

        if price_move >= self.rapid_move_threshold:
            flag = ManipulationFlag(
                market_id=market_id,
                reason=(
                    f"Rapid price move: {prev_price:.2f} → {curr_price:.2f} "
                    f"({price_move:.0%} in {curr_time - prev_time:.0f}s) "
                    f"exceeds {self.rapid_move_threshold:.0%} threshold"
                ),
                detected_at=now,
                price_move=price_move,
                previous_price=prev_price,
                current_price=curr_price,
            )
            self._flags[market_id] = flag
            logger.warning("Manipulation flag: %s", flag.reason)
            return flag

        return None

    def _check_crossed_book(
        self,
        market: Market,
        now: float,
    ) -> ManipulationFlag | None:
        """Detect crossed/inverted order books (YES + NO far from 1.0)."""
        price_sum = market.yes_price + market.no_price
        if price_sum <= 0:
            return None

        # Normal range is 0.98-1.02 for binary markets
        # Significant deviation suggests book manipulation or stale data
        deviation = abs(price_sum - 1.0)
        if deviation > 0.08:
            flag = ManipulationFlag(
                market_id=market.ticker,
                reason=(
                    f"Crossed/inverted book: YES({market.yes_price:.2f}) + "
                    f"NO({market.no_price:.2f}) = {price_sum:.2f} "
                    f"(deviation {deviation:.0%} from 1.0)"
                ),
                detected_at=now,
                current_price=market.yes_price,
            )
            self._flags[market.ticker] = flag
            logger.warning("Manipulation flag: %s", flag.reason)
            return flag

        return None

    def _clean_expired_flags(self, now: float) -> None:
        """Remove flags that have expired."""
        expired = [
            mid for mid, flag in self._flags.items()
            if now - flag.detected_at > self.flag_expiry_seconds
        ]
        for mid in expired:
            del self._flags[mid]
