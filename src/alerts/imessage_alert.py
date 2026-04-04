"""iMessage alert backend — sends alerts via VAYU iMessage infrastructure.

Falls back to logging if the iMessage endpoint is unreachable.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class IMessageBackend:
    """Sends alerts via HTTP POST to VAYU iMessage endpoint."""

    def __init__(self, endpoint: str, timeout: float = 10.0):
        self.endpoint = endpoint
        self.timeout = timeout

    async def send(self, title: str, body: str) -> bool:
        """Send an alert via iMessage.

        Returns True if sent successfully, False on failure.
        """
        message = f"{title}\n\n{body}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    json={"message": message},
                )
                response.raise_for_status()
                logger.debug(f"iMessage sent: {title}")
                return True
        except Exception as e:
            logger.warning(f"iMessage delivery failed: {e}")
            return False
