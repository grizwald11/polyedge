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
            return False

        # Check daily loss limit (realized + unrealized)
        daily_pnl = self.db.get_daily_pnl()
        daily_pnl += unrealized_pnl  # Include open position losses
        daily_limit = bankroll * self.settings.trading.daily_loss_limit_pct

        if daily_pnl < -daily_limit:
            self._halt(
                f"Daily loss limit hit: ${daily_pnl:.2f} exceeds "
                f"-${daily_limit:.2f} ({self.settings.trading.daily_loss_limit_pct:.0%})"
            )
            return False

        # Check consecutive losing days
        self._update_consecutive_losses()
        if self._consecutive_losing_days >= 5:
            self._halt(
                f"5 consecutive losing days — manual review required"
            )
            return False

        if self._consecutive_losing_days >= 3:
            if not self._reduced_sizing:
                self._reduced_sizing = True
                self._persist_state()
                logger.warning(
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
        """Update consecutive losing days from database."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day_checked == today:
            return  # Already checked today

        self._last_day_checked = today
        # Consecutive loss tracking is maintained via record_daily_result()
        # called at end of each trading day by the orchestrator
