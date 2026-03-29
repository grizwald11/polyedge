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
        self._reduced_sizing = False
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
            # Auto-reset daily halt if enough time has passed (at least 6 hours
            # and a new calendar day in UTC). The 6-hour minimum prevents edge
            # cases where the halt triggers just before midnight UTC.
            if "Daily loss limit" in (self._halt_reason or "") and self._halt_time:
                now = datetime.now(timezone.utc)
                hours_since_halt = (now - self._halt_time).total_seconds() / 3600
                if now.date() > self._halt_time.date() and hours_since_halt >= 6:
                    self.reset_daily()
                else:
                    return False
            else:
                return False

        # Check daily loss limit (realized + discounted unrealized)
        # Unrealized losses are temporary — weight at 30% to avoid false halts
        daily_pnl = self.db.get_daily_pnl()
        daily_pnl += unrealized_pnl * 0.3
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

    def reset(self):
        """Manually reset the circuit breaker."""
        self._halted = False
        self._halt_reason = None
        self._halt_time = None
        self._consecutive_losing_days = 0
        self._reduced_sizing = False
        self._persist_state()
        logger.info("Circuit breaker reset")

    def reset_daily(self):
        """Reset daily halt (called at start of new trading day)."""
        if self._halted and "Daily loss limit" in (self._halt_reason or ""):
            self._halted = False
            self._halt_reason = None
            self._halt_time = None
            self._persist_state()
            logger.info("Circuit breaker: daily halt cleared for new day")

    def record_daily_result(self, pnl: float):
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
        self._persist_state()

    def _halt(self, reason: str):
        """Halt all trading."""
        self._halted = True
        self._halt_reason = reason
        self._halt_time = datetime.now(timezone.utc)
        self._persist_state()
        logger.warning(f"CIRCUIT BREAKER TRIGGERED: {reason}")

    def _load_state(self):
        """Load persisted state from database on startup."""
        state = self.db.load_circuit_breaker_state()
        if state is None:
            return
        self._consecutive_losing_days = state["consecutive_losing_days"]
        self._reduced_sizing = bool(state["reduced_sizing"])
        self._halted = bool(state["halted"])
        self._halt_reason = state["halt_reason"]
        if state["halt_time"]:
            self._halt_time = datetime.fromisoformat(state["halt_time"])
        if self._halted or self._consecutive_losing_days > 0 or self._reduced_sizing:
            logger.info(
                f"Loaded circuit breaker state: "
                f"halted={self._halted}, "
                f"consecutive_losing_days={self._consecutive_losing_days}, "
                f"reduced_sizing={self._reduced_sizing}"
            )

        # Auto-reset daily halt if we restarted on a new day
        if self._halted and self._halt_time:
            if datetime.now(timezone.utc).date() > self._halt_time.date():
                self.reset_daily()

    def _persist_state(self):
        """Save current state to database."""
        self.db.save_circuit_breaker_state(
            consecutive_losing_days=self._consecutive_losing_days,
            reduced_sizing=self._reduced_sizing,
            halted=self._halted,
            halt_reason=self._halt_reason,
            halt_time=self._halt_time.isoformat() if self._halt_time else None,
        )

    def _update_consecutive_losses(self):
        """Check consecutive losing day state (updated at day boundary via record_daily_result)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day_checked == today:
            return  # Already checked today

        self._last_day_checked = today
        # State is maintained via record_daily_result() called at day boundary.
        # If we missed a day boundary (e.g., restart), check yesterday's P&L.
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_pnl = self.db.get_daily_pnl(yesterday)
        if yesterday_pnl != 0.0:
            logger.info(f"Auto-recording missed day result: P&L=${yesterday_pnl:.2f}")
            self.record_daily_result(yesterday_pnl)
