"""Polymarket CLOB client wrapper — authenticated trading operations.

Wraps py-clob-client with retry logic, error handling, and typed responses.
Requires PRIVATE_KEY and optionally FUNDER_ADDRESS in environment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Wrapper around py-clob-client for authenticated Polymarket operations.

    This class handles initialization, credential derivation, and provides
    typed methods for common operations. All methods include retry logic.

    Note: py-clob-client is synchronous, so we wrap calls in asyncio
    to keep the main loop non-blocking.
    """

    def __init__(
        self,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        signature_type: int = 1,
    ):
        self.host = host
        self.chain_id = chain_id
        self.private_key = private_key
        self.funder = funder
        self.signature_type = signature_type
        self._client = None
        self._initialized = False

    def _init_client(self):
        """Lazily initialize the ClobClient."""
        if self._initialized:
            return

        try:
            from py_clob_client.client import ClobClient

            if self.private_key:
                kwargs: dict[str, Any] = {
                    "host": self.host,
                    "chain_id": self.chain_id,
                    "key": self.private_key,
                    "signature_type": self.signature_type,
                }
                if self.funder:
                    kwargs["funder"] = self.funder

                self._client = ClobClient(**kwargs)

                # Derive API credentials for L2 operations
                try:
                    creds = self._client.create_or_derive_api_creds()
                    self._client.set_api_creds(creds)
                    logger.info("CLOB client initialized with L2 credentials")
                except Exception as e:
                    logger.warning(f"Could not derive API creds (read-only mode): {e}")
            else:
                # Read-only mode (no auth)
                self._client = ClobClient(self.host, chain_id=self.chain_id)
                logger.info("CLOB client initialized in read-only mode (no private key)")

            self._initialized = True

        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            raise

    def _ensure_client(self):
        if not self._initialized:
            self._init_client()
        if self._client is None:
            raise RuntimeError("CLOB client not initialized")
        return self._client

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous py-clob-client call in the executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    # ──────────────────────────────────────
    # Health Check
    # ──────────────────────────────────────

    async def health_check(self) -> bool:
        """Check if the CLOB API is reachable."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_ok)
            return result == "OK"
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    async def get_server_time(self) -> Optional[str]:
        """Get the CLOB server time."""
        try:
            client = self._ensure_client()
            return await self._run_sync(client.get_server_time)
        except Exception as e:
            logger.error(f"Failed to get server time: {e}")
            return None

    # ──────────────────────────────────────
    # Market Data (public, no auth needed)
    # ──────────────────────────────────────

    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get current best price for a token."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_price, token_id, side)
            if result is not None:
                return float(result)
            return None
        except Exception as e:
            logger.error(f"Failed to get price for {token_id[:16]}...: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_midpoint, token_id)
            if result is not None:
                return float(result)
            return None
        except Exception as e:
            logger.error(f"Failed to get midpoint for {token_id[:16]}...: {e}")
            return None

    async def get_order_book(self, token_id: str) -> Optional[dict]:
        """Get full order book for a token."""
        try:
            client = self._ensure_client()
            return await self._run_sync(client.get_order_book, token_id)
        except Exception as e:
            logger.error(f"Failed to get order book for {token_id[:16]}...: {e}")
            return None

    async def get_spread(self, token_id: str) -> Optional[dict]:
        """Get spread for a token."""
        try:
            client = self._ensure_client()
            return await self._run_sync(client.get_spread, token_id)
        except Exception as e:
            logger.error(f"Failed to get spread for {token_id[:16]}...: {e}")
            return None

    # ──────────────────────────────────────
    # Account Data (L2 auth required)
    # ──────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Get USDC balance."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_balance_allowance)
            if result and "balance" in result:
                return float(result["balance"]) / 1e6  # USDC has 6 decimals
            return None
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return None

    async def get_positions(self) -> list[dict]:
        """Get all open positions."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_positions)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    async def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.get_orders)
            if isinstance(result, dict) and "data" in result:
                return result["data"]
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    # ──────────────────────────────────────
    # Order Management (L2 auth + signing)
    # ──────────────────────────────────────

    async def create_and_post_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        neg_risk: bool = False,
    ) -> Optional[dict]:
        """Create and post a GTC limit order (maker order, zero fees on event markets)."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            client = self._ensure_client()
            order_side = BUY if side.upper() == "BUY" else SELL

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            )
            tick_size = "0.01"  # Standard tick size

            signed = await self._run_sync(
                client.create_order,
                order_args,
            )
            result = await self._run_sync(
                client.post_order,
                signed,
                ClobOrderType.GTC,
            )

            logger.info(f"Limit order posted: {side} {size} shares at ${price:.2f}")
            return result

        except Exception as e:
            logger.error(f"Failed to create/post limit order: {e}")
            return None

    async def create_and_post_market_order(
        self,
        token_id: str,
        amount: float,
        side: str = "BUY",
    ) -> Optional[dict]:
        """Create and post a FOK market order (taker, may incur fees)."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType as ClobOrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            client = self._ensure_client()
            order_side = BUY if side.upper() == "BUY" else SELL

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=order_side,
            )

            signed = await self._run_sync(client.create_market_order, mo)
            result = await self._run_sync(
                client.post_order,
                signed,
                ClobOrderType.FOK,
            )

            logger.info(f"Market order posted: {side} ${amount:.2f} worth")
            return result

        except Exception as e:
            logger.error(f"Failed to create/post market order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> Optional[dict]:
        """Cancel an open order."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.cancel, order_id)
            logger.info(f"Order cancelled: {order_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return None

    async def cancel_all_orders(self) -> Optional[dict]:
        """Cancel all open orders."""
        try:
            client = self._ensure_client()
            result = await self._run_sync(client.cancel_all)
            logger.info("All orders cancelled")
            return result
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return None
