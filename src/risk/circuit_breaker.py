"""Circuit breaker — halts trading on excessive losses.

Monitors daily P&L and consecutive losing days. Triggers automatic
trading halts to prevent catastrophic drawdowns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.storage.database import Database

logger = logging.getLogger(__name__)

MAX_UNREALIZED_LOSS_PCT = 0.15


class CircuitBreaker:
    """Daily loss limit and consecutive loss detection."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._halted = False
        self._halt_reason: Optional[str] = None
        self._halt_time: Optional[datetime] = None
        self._consecutive_losing_days = 0
        self._last_day_checked: Optional[str] = None
        self._last_recorded_day: Optional[str] = None
        self._reduced_sizing = False
        # H-3: Peak-to-trough drawdown tracking
        self._high_water_mark: float = settings.trading.bankroll
        self._load_state()

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    @property
    def is_reduced_sizing(self) -> bool:
        return self._reduced_sizing

    def check(self, bankroll: float, unrealized_pnl: float = 0.0) -> bool:
        """Run circuit breaker checks. Returns True if trading should continue.

        Args:
            bankroll: Current bankroll for loss limit calculation
            unrealized_pnl: Unrealized P&L from open positions (typically negative)

        Returns:
            True if trading is allowed, False if halted
        """
        if self._halted:
            # M-22: Auto-reset daily halt after 24 hours wall-clock time,
            # not calendar date. Calendar date resets can allow trading to
            # resume too quickly if the halt triggers near midnight UTC.
            if "Daily loss limit" in (self._halt_reason or "") and self._halt_time:
                now = datetime.now(timezone.utc)
                seconds_since_halt = (now - self._halt_time).total_seconds()
                if seconds_since_halt >= 86400:
                    self.reset_daily()
                else:
                    return False
            else:
                return False

        # H-3: Max drawdown check — halt if equity drops >20% from peak
        equity = bankroll + unrealized_pnl
        if equity > self._high_water_mark:
            self._high_water_mark = equity
        if self._high_water_mark > 0:
            drawdown = (self._high_water_mark - equity) / self._high_water_mark
            max_dd = self.settings.trading.max_drawdown_pct
            if drawdown >= max_dd:
                reason = (
                    f"Max drawdown hit: {drawdown:.1%} from peak "
                    f"${self._high_water_mark:.2f} (limit {max_dd:.0%})"
                )
                self._halt(reason)
                logger.critical(f"CIRCUIT BREAKER HALTED: {reason}")
                return False

        # M-3: Hard gate — halt if unrealized losses alone exceed 15% of bankroll.
        # This catches scenarios where realized P&L looks fine but open positions
        # are deeply underwater, indicating imminent large realized losses.
        if unrealized_pnl < 0 and bankroll > 0:
            unrealized_loss_pct = abs(unrealized_pnl) / bankroll
            if unrealized_loss_pct >= MAX_UNREALIZED_LOSS_PCT:
                reason = (
                    f"Unrealized loss gate: ${unrealized_pnl:.2f} = "
                    f"{unrealized_loss_pct:.1%} of bankroll "
                    f"(limit {MAX_UNREALIZED_LOSS_PCT:.0%})"
                )
                self._halt(reason)
                logger.critical(f"CIRCUIT BREAKER HALTED: {reason}")
                return False

        # Check daily loss limit (realized + discounted unrealized).
        # Weight unrealized losses at 75% — balances between being too aggressive
        # (100%, which would halt on normal intraday fluctuations) and too lenient
        # (50%, which delays halt when positions are deeply underwater).
        daily_pnl = self.db.get_daily_pnl()
        daily_pnl += unrealized_pnl * 0.75
        daily_limit = bankroll * self.settings.trading.daily_loss_limit_pct

        if daily_pnl < -daily_limit:
            reason = (
                f"Daily loss limit hit: ${daily_pnl:.2f} exceeds "
                f"-${daily_limit:.2f} ({self.settings.trading.daily_loss_limit_pct:.0%})"
            )
            self._halt(reason)
            logger.critical(f"CIRCUIT BREAKER HALTED: {reason}")
            return False

        # Consecutive losing day state machine:
        #   0-2 losing days → normal operation (full Kelly multiplier)
        #   3-4 losing days → reduced_sizing=True (quarter-Kelly via 0.5 multiplier)
        #   5+  losing days → full halt (manual review required)
        #
        # State transitions:
        #   record_daily_result(pnl<0) increments _consecutive_losing_days
        #   record_daily_result(pnl>=0) resets to 0 AND clears reduced_sizing
        #   _update_consecutive_losses() checks for missed day boundaries on restart
        self._update_consecutive_losses()
        if self._consecutive_losing_days >= 5:
            reason = "5 consecutive losing days — manual review required"
            self._halt(reason)
            logger.critical(f"CIRCUIT BREAKER HALTED: {reason}")
            return False

        if self._consecutive_losing_days >= 3:
            if not self._reduced_sizing:
                self._reduced_sizing = True
                self._persist_state()
                logger.error(
                    f"3 consecutive losing days — reducing to quarter-Kelly"
                )

        return True

    def is_halted(self) -> bool:
        """Check if trading is currently halted."""
        return self._halted

    def get_kelly_multiplier(self) -> float:
        """Get current Kelly multiplier (reduced after consecutive losses).

        Returns:
            1.0 for normal, 0.5 for reduced sizing
        """
        if self._reduced_sizing:
            return 0.5  # Quarter-Kelly (half of half-Kelly)
        return 1.0

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self._halted = False
        self._halt_reason = None
        self._halt_time = None
        self._consecutive_losing_days = 0
        self._reduced_sizing = False
        self._persist_state()
        logger.info("Circuit breaker reset")

    def reset_daily(self) -> None:
        """Reset daily halt (called at start of new trading day)."""
        if self._halted and "Daily loss limit" in (self._halt_reason or ""):
            self._halted = False
            self._halt_reason = None
            self._halt_time = None
            self._persist_state()
            logger.info("Circuit breaker: daily halt cleared for new day")

    def record_daily_result(self, pnl: float) -> None:
        """Record a day's P&L for consecutive loss tracking.

        Args:
            pnl: Day's total P&L in dollars
        """
        if pnl < 0:
            self._consecutive_losing_days += 1
            logger.info(
                f"Losing day #{self._consecutive_losing_days}: ${pnl:.2f}"
            )
        else:
            self._consecutive_losing_days = 0
            self._reduced_sizing = False
        # Track which day was last recorded to prevent double-counting on restart
        self._last_recorded_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._persist_state()

    def _halt(self, reason: str) -> None:
        """Halt all trading."""
        self._halted = True
        self._halt_reason = reason
        self._halt_time = datetime.now(timezone.utc)
        self._persist_state()
        logger.warning(f"CIRCUIT BREAKER TRIGGERED: {reason}")

    def _load_state(self) -> None:
        """Load persisted state from database on startup."""
        state = self.db.load_circuit_breaker_state()
        if state is None:
            return
        self._consecutive_losing_days = state["consecutive_losing_days"]
        self._reduced_sizing = bool(state["reduced_sizing"])
        self._halted = bool(state["halted"])
        self._halt_reason = state["halt_reason"]
        self._last_recorded_day = state.get("last_recorded_day")
        if state["halt_time"]:
            self._halt_time = datetime.fromisoformat(state["halt_time"])
        if self._halted or self._consecutive_losing_days > 0 or self._reduced_sizing:
            logger.info(
                f"Loaded circuit breaker state: "
                f"halted={self._halted}, "
                f"consecutive_losing_days={self._consecutive_losing_days}, "
                f"reduced_sizing={self._reduced_sizing}"
            )

        # M-22: Auto-reset daily halt if 24 hours have passed since halt
        if self._halted and self._halt_time:
            if (datetime.now(timezone.utc) - self._halt_time).total_seconds() >= 86400:
                self.reset_daily()

    def _persist_state(self) -> None:
        """Save current state to database."""
        self.db.save_circuit_breaker_state(
            consecutive_losing_days=self._consecutive_losing_days,
            reduced_sizing=self._reduced_sizing,
            halted=self._halted,
            halt_reason=self._halt_reason,
            halt_time=self._halt_time.isoformat() if self._halt_time else None,
            last_recorded_day=self._last_recorded_day,
        )

    def _update_consecutive_losses(self) -> None:
        """Check consecutive losing day state (updated at day boundary via record_daily_result)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day_checked == today:
            return  # Already checked today

        self._last_day_checked = today
        # State is maintained via record_daily_result() called at day boundary.
        # If we missed a day boundary (e.g., restart), check yesterday's P&L.
        # _last_recorded_day is persisted to DB to prevent double-counting on restart (C-1).
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        if yesterday != self._last_recorded_day:
            yesterday_pnl = self.db.get_daily_pnl(yesterday)
            if abs(yesterday_pnl) > 0.50:  # Ignore near-zero P&L (rounding noise)
                logger.info(f"Auto-recording missed day result: P&L=${yesterday_pnl:.2f}")
                self.record_daily_result(yesterday_pnl)
                self._last_recorded_day = yesterday
