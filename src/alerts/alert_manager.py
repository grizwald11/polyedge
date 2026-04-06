"""Alert manager — central dispatch for trade notifications and error alerts.

Routes alerts to registered backends (iMessage, log-only) with error isolation.
Includes cooldown-based deduplication to prevent alert spam.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

logger = logging.getLogger(__name__)

# Default cooldown periods (seconds) per alert type
DEFAULT_COOLDOWNS = {
    "trade": 0,              # Trade alerts are always sent (each is unique)
    "circuit_breaker": 3600, # 1 hour
    "error": 300,            # 5 minutes for same error
    "low_balance": 1800,     # 30 minutes
    "loss_velocity": 900,    # 15 minutes
    "daily_summary": 0,      # Always sent (once per day by design)
}


class AlertBackend(Protocol):
    """Protocol for alert delivery backends."""

    async def send(self, title: str, body: str) -> bool: ...


class LogBackend:
    """Fallback backend that logs alerts."""

    async def send(self, title: str, body: str) -> bool:
        logger.info(f"[ALERT] {title}\n{body}")
        return True


class AlertManager:
    """Central alert dispatch — routes alerts to registered backends.

    Includes cooldown-based dedup: repeated alerts with the same key
    are suppressed for a configurable period per alert type.
    """

    def __init__(self, cooldowns: dict[str, int] | None = None):
        self._backends: list[AlertBackend] = []
        self._cooldowns = cooldowns or dict(DEFAULT_COOLDOWNS)
        self._last_sent: dict[str, float] = {}  # alert_key -> monotonic timestamp

    def register(self, backend: AlertBackend):
        """Register an alert delivery backend."""
        self._backends.append(backend)

    def _should_send(self, alert_type: str, dedup_key: str = "") -> bool:
        """Check if an alert should be sent based on cooldown.

        Returns True if the alert should be sent, False if suppressed.
        """
        cooldown = self._cooldowns.get(alert_type, 0)
        if cooldown <= 0:
            return True

        key = f"{alert_type}:{dedup_key}"
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < cooldown:
            logger.debug(
                f"Alert suppressed (cooldown): type={alert_type}, "
                f"remaining={cooldown - (now - last):.0f}s"
            )
            return False

        self._last_sent[key] = now
        return True

    async def send_trade_alert(
        self,
        market_id: str,
        direction: str,
        size: int,
        price: float,
        cost: float,
        strategy: str,
        edge: float,
    ):
        """Send alert when a trade is executed."""
        title = f"Trade: {direction} {size}x {market_id}"
        body = (
            f"Strategy: {strategy}\n"
            f"Price: ${price:.2f}\n"
            f"Cost: ${cost:.2f}\n"
            f"Edge: {edge:.1%}"
        )
        await self._dispatch(title, body)

    async def send_circuit_breaker_alert(self, reason: str):
        """Send alert when circuit breaker triggers."""
        if not self._should_send("circuit_breaker", reason):
            return
        title = "CIRCUIT BREAKER TRIGGERED"
        body = f"Reason: {reason}\nTrading has been halted."
        await self._dispatch(title, body)

    async def send_error_alert(self, error: str, context: str = ""):
        """Send alert on critical errors."""
        if not self._should_send("error", error[:80]):
            return
        title = "PolyEdge Error"
        body = f"Error: {error}"
        if context:
            body += f"\nContext: {context}"
        await self._dispatch(title, body)

    async def send_low_balance_alert(self, bankroll: float, initial_bankroll: float):
        """Send alert when bankroll drops below warning threshold."""
        if not self._should_send("low_balance"):
            return
        pct = bankroll / initial_bankroll * 100 if initial_bankroll > 0 else 0
        title = "LOW BALANCE WARNING"
        body = (
            f"Bankroll: ${bankroll:.2f} ({pct:.0f}% of initial ${initial_bankroll:.2f})\n"
            f"Consider reviewing open positions and risk parameters."
        )
        await self._dispatch(title, body)

    async def send_loss_velocity_alert(self, daily_pnl: float, daily_limit: float, pct_of_limit: float):
        """Send alert when daily losses approach the circuit breaker threshold."""
        if not self._should_send("loss_velocity"):
            return
        title = "LOSS VELOCITY WARNING"
        body = (
            f"Daily P&L: ${daily_pnl:.2f}\n"
            f"Daily loss limit: -${daily_limit:.2f}\n"
            f"Currently at {pct_of_limit:.0%} of halt threshold.\n"
            f"Trading will halt if losses reach -${daily_limit:.2f}."
        )
        await self._dispatch(title, body)

    async def send_daily_summary(self, summary: str):
        """Send the daily P&L report."""
        title = "PolyEdge Daily Report"
        await self._dispatch(title, summary)

    async def _dispatch(self, title: str, body: str):
        """Send alert to all registered backends with error isolation.

        Each backend gets a 10-second timeout to prevent a slow/dead endpoint
        from blocking the trading loop.
        """
        import asyncio

        if not self._backends:
            logger.debug(f"No alert backends registered, logging: {title}")
            return

        for backend in self._backends:
            try:
                await asyncio.wait_for(backend.send(title, body), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(f"Alert backend {type(backend).__name__} timed out (>10s)")
            except Exception as e:
                logger.warning(f"Alert backend {type(backend).__name__} failed: {e}")
