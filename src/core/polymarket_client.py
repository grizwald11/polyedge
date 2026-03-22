"""Polymarket CLOB API client — wraps py-clob-client for async trading.

py-clob-client is synchronous, so all methods use run_in_executor()
to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType as PolyOrderType

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Async wrapper around py-clob-client for Polymarket trading."""

    def __init__(
        self,
        host: str = "https://clob.polymarket.com",
        private_key: str = "",
        chain_id: int = 137,
        signature_type: int = 1,
    ):
        self.host = host
        self._private_key = private_key
        self._chain_id = chain_id
        self._signature_type = signature_type
        self._client: Optional[ClobClient] = None
        self._initialized = False

    async def initialize(self):
        """Initialize the ClobClient and derive API credentials.

        Must be called before any trading operations.
        """
        if self._initialized:
            return

        loop = asyncio.get_event_loop()
        self._client = await loop.run_in_executor(
            None,
            partial(
                ClobClient,
                self.host,
                key=self._private_key,
                chain_id=self._chain_id,
                signature_type=self._signature_type,
            ),
        )

        # Derive or load API credentials
        try:
            creds = await loop.run_in_executor(
                None, self._client.create_or_derive_api_creds
            )
            await loop.run_in_executor(
                None, self._client.set_api_creds, creds
            )
            self._initialized = True
            logger.info("Polymarket client initialized and API credentials derived")
        except Exception as e:
            logger.error(f"Failed to derive Polymarket API credentials: {e}")
            raise

    async def _run(self, func, *args, **kwargs):
        """Run a synchronous ClobClient method in the executor."""
        if not self._initialized:
            raise RuntimeError("PolymarketClient not initialized. Call initialize() first.")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    async def health_check(self) -> bool:
        """Check if the CLOB API is reachable."""
        try:
            if not self._client:
                return False
            result = await self._run(self._client.get_ok)
            return result == "OK"
        except Exception as e:
            logger.warning(f"Polymarket health check failed: {e}")
            return False

    async def get_server_time(self) -> str:
        """Get the server time."""
        return await self._run(self._client.get_server_time)

    async def get_balance(self) -> float:
        """Get USDC balance and allowance.

        Returns balance in dollars.
        """
        result = await self._run(self._client.get_balance_allowance)
        # Result has 'balance' in wei-like format; convert
        balance = float(result.get("balance", 0))
        # py-clob-client returns balance in USDC units (6 decimals)
        return balance / 1e6 if balance > 1000 else balance

    async def get_order_book(self, token_id: str) -> dict:
        """Get the order book for a token."""
        return await self._run(self._client.get_order_book, token_id)

    async def get_midpoint(self, token_id: str) -> float:
        """Get the midpoint price for a token."""
        result = await self._run(self._client.get_midpoint, token_id)
        return float(result.get("mid", 0.0)) if isinstance(result, dict) else float(result)

    async def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get the best price for a token on a given side."""
        result = await self._run(self._client.get_price, token_id, side)
        return float(result.get("price", 0.0)) if isinstance(result, dict) else float(result)

    async def create_and_post_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict:
        """Create, sign, and post an order to the CLOB.

        Args:
            token_id: The conditional token ID (YES or NO token).
            side: "BUY" or "SELL".
            price: Price in dollars (0.0-1.0).
            size: Number of shares.
            order_type: "GTC" (limit) or "FOK" (market).

        Returns:
            Order response dict from the API.
        """
        poly_order_type = (
            PolyOrderType.GTC if order_type == "GTC" else PolyOrderType.FOK
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )

        # Build the order (signs it)
        signed_order = await self._run(
            self._client.create_order, order_args, poly_order_type
        )

        # Post it
        result = await self._run(self._client.post_order, signed_order)
        logger.info(
            f"Polymarket order posted: {side} {size}x {token_id[:16]}... @ ${price:.2f}"
        )
        return result

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        result = await self._run(self._client.cancel, order_id)
        logger.info(f"Polymarket order cancelled: {order_id}")
        return result

    async def get_order(self, order_id: str) -> dict:
        """Get order status."""
        return await self._run(self._client.get_order, order_id)

    async def get_orders(self) -> list[dict]:
        """Get all open orders."""
        result = await self._run(self._client.get_orders)
        return result if isinstance(result, list) else result.get("orders", [])

    async def get_trades(self) -> list[dict]:
        """Get recent trades/fills."""
        result = await self._run(self._client.get_trades)
        return result if isinstance(result, list) else result.get("trades", [])
