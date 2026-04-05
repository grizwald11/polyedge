"""Market manipulation detector — flags suspicious price/volume activity.

Checks for:
- Rapid price moves without proportional volume (potential pump/dump)
- Extreme price volatility relative to historical norms
- Crossed or inverted order books (potential spoofing artifacts)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.core.models import Market

logger = logging.getLogger(__name__)

# Price move >20% in a single scan interval is suspicious
RAPID_MOVE_THRESHOLD = 0.20

# Minimum snapshots needed before we can detect manipulation
MIN_SNAPSHOTS_FOR_DETECTION = 2

# How long (seconds) to keep a market flagged after detection
# M-7: Reduced from 1800 (30 min) to 900 (15 min) — 30 min was overly
# conservative and blocked legitimate re-entry on markets that had
# already stabilized.
FLAG_EXPIRY_SECONDS = 900  # 15 minutes


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
        # M-15: Increased from 50 to 200 snapshots. Memory impact is small
        # (200 floats per market) and longer history improves drift detection.
        self._max_history = 200

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

        # If already flagged and not expired, check for volume-based early reset
        if market_id in self._flags:
            # M-7: High subsequent volume indicates organic price discovery —
            # clear the flag early instead of waiting for full expiry.
            volume_24h = getattr(market, 'volume_24h', 0.0) or 0.0
            flag = self._flags[market_id]
            if volume_24h > 50_000:
                elapsed = now - flag.detected_at
                # Require at least 60s since detection to avoid instant clears
                if elapsed > 60:
                    logger.info(
                        "M-7: Clearing manipulation flag for %s early — "
                        "high volume ($%.0f) indicates organic move",
                        market_id, volume_24h,
                    )
                    del self._flags[market_id]
                    # Fall through to normal checks below
                else:
                    return flag
            else:
                return self._flags[market_id]

        # Record current price
        current_price = market.yes_price
        if current_price <= 0:
            return None

        self.update_price(market_id, current_price)

        history = self._price_history.get(market_id, [])
        if len(history) < MIN_SNAPSHOTS_FOR_DETECTION:
            return None

        # Check for rapid price move (M-4: pass volume for weighted threshold)
        volume_24h = getattr(market, 'volume_24h', 0.0) or 0.0
        flag = self._check_rapid_move(market_id, history, now, volume_24h=volume_24h)
        if flag is not None:
            return flag

        # M-5: Check for slow manipulation (steady drift over 3+ snapshots)
        flag = self._check_slow_drift(market_id, history, now)
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
        volume_24h: float = 0.0,
    ) -> ManipulationFlag | None:
        """Detect if price moved more than threshold since previous observation.

        M-4: Volume-weighted threshold — high-volume markets (>$50K) need a
        larger move (30%) to flag since big moves on liquid markets are more
        likely organic. Low-volume markets (<$5K) use a tighter threshold (15%)
        since they are easier to manipulate.
        """
        if len(history) < 2:
            return None

        prev_time, prev_price = history[-2]
        curr_time, curr_price = history[-1]

        if prev_price <= 0:
            return None

        # M-4: Volume-weighted rapid move threshold
        if volume_24h > 50_000:
            threshold = max(self.rapid_move_threshold, 0.30)
        elif volume_24h < 5_000:
            threshold = min(self.rapid_move_threshold, 0.15)
        else:
            threshold = self.rapid_move_threshold

        # M-17: Use relative (percentage) price change instead of absolute.
        # This prevents false positives on low-priced markets (e.g., $0.05 → $0.10
        # is a 100% move but only $0.05 absolute) and false negatives on
        # high-priced markets (e.g., $0.80 → $0.95 is only 18.75% but $0.15 absolute).
        price_move = abs(curr_price - prev_price) / max(prev_price, 0.01)

        if price_move >= threshold:
            flag = ManipulationFlag(
                market_id=market_id,
                reason=(
                    f"Rapid price move: {prev_price:.2f} → {curr_price:.2f} "
                    f"({price_move:.0%} in {curr_time - prev_time:.0f}s) "
                    f"exceeds {threshold:.0%} threshold"
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

    def _check_slow_drift(
        self,
        market_id: str,
        history: list[tuple[float, float]],
        now: float,
    ) -> ManipulationFlag | None:
        """M-5: Detect slow manipulation — steady price drift over 3+ snapshots.

        A monotonic price drift of >15% cumulative over 3+ consecutive snapshots
        without any reversal suggests coordinated manipulation rather than
        organic price discovery (which tends to oscillate).
        """
        if len(history) < 4:
            return None

        # Check last 5 snapshots (or fewer if unavailable)
        # Require at least 4 points (3 moves) to distinguish slow drift from
        # normal two-step price adjustments.
        recent = history[-5:]
        if len(recent) < 4:
            return None

        # Check if all moves are in the same direction (monotonic)
        deltas = [recent[i][1] - recent[i - 1][1] for i in range(1, len(recent))]
        all_up = all(d > 0.001 for d in deltas)
        all_down = all(d < -0.001 for d in deltas)

        if not (all_up or all_down):
            return None

        cumulative_move = abs(recent[-1][1] - recent[0][1])
        time_span = recent[-1][0] - recent[0][0]

        # Only flag if cumulative drift exceeds 15% and happened within 30 minutes
        if cumulative_move >= 0.15 and time_span <= 1800:
            direction = "up" if all_up else "down"
            flag = ManipulationFlag(
                market_id=market_id,
                reason=(
                    f"Slow drift: {len(deltas)} consecutive {direction} moves, "
                    f"cumulative {cumulative_move:.0%} over {time_span:.0f}s "
                    f"({recent[0][1]:.2f} → {recent[-1][1]:.2f})"
                ),
                detected_at=now,
                price_move=cumulative_move,
                previous_price=recent[0][1],
                current_price=recent[-1][1],
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
        # M-6: Tightened from 8% to 5% — 8% allowed too much room for
        # manipulated books to slip through undetected.
        deviation = abs(price_sum - 1.0)
        if deviation > 0.05:
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
