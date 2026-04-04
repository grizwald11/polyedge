"""Alert manager — central dispatch for trade notifications and error alerts.

Routes alerts to registered backends (iMessage, log-only) with error isolation.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class AlertBackend(Protocol):
    """Protocol for alert delivery backends."""

    async def send(self, title: str, body: str) -> bool: ...


class LogBackend:
    """Fallback backend that logs alerts."""

    async def send(self, title: str, body: str) -> bool:
        logger.info(f"[ALERT] {title}\n{body}")
        return True


class AlertManager:
    """Central alert dispatch — routes alerts to registered backends."""

    def __init__(self):
        self._backends: list[AlertBackend] = []

    def register(self, backend: AlertBackend):
        """Register an alert delivery backend."""
        self._backends.append(backend)

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
        title = "CIRCUIT BREAKER TRIGGERED"
        body = f"Reason: {reason}\nTrading has been halted."
        await self._dispatch(title, body)

    async def send_error_alert(self, error: str, context: str = ""):
        """Send alert on critical errors."""
        title = "PolyEdge Error"
        body = f"Error: {error}"
        if context:
            body += f"\nContext: {context}"
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
