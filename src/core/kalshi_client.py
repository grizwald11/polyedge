"""Kalshi API client wrapper — authenticated trading operations.

Wraps kalshi-python SDK with retry logic, error handling, and typed responses.
Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in environment.
Uses RSA private key signing for authentication.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class KalshiRateLimitError(Exception):
    """Raised when Kalshi API rate limits are exhausted after retries."""
    pass


class KalshiClient:
    """Wrapper around Kalshi's REST API for trading operations.

    Uses RSA key-based authentication. All methods include retry logic.
    Synchronous SDK calls are wrapped in asyncio for non-blocking operation.
    """

    def __init__(
        self,
        host: str = "https://demo-api.kalshi.co/trade-api/v2",
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
        max_concurrent: int = 5,
        min_request_interval: float = 0.1,
    ):
        self.host = host.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self._client: Optional[httpx.AsyncClient] = None
        self._private_key = None
        self._key_load_attempted: bool = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_request_interval = min_request_interval
        self._last_request_time: float = 0.0
        self._consecutive_timeouts: int = 0

    def _load_private_key(self):
        """Load the RSA private key for API signing."""
        if self._private_key is not None:
            return self._private_key
        if self._key_load_attempted:
            return None
        if not self.private_key_path:
            return None
        self._key_load_attempted = True
        try:
            import os
            import stat
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            # Check file permissions — private key should be owner-only (0o600)
            key_stat = os.stat(self.private_key_path)
            mode = key_stat.st_mode & 0o777
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                logger.warning(
                    f"Private key file {self.private_key_path} has permissive mode "
                    f"{oct(mode)} — should be 0o600. Fixing permissions."
                )
                os.chmod(self.private_key_path, 0o600)
            with open(self.private_key_path, "rb") as f:
                self._private_key = load_pem_private_key(f.read(), password=None)
            logger.info("Loaded RSA private key for Kalshi auth")
            return self._private_key
        except Exception as e:
            logger.error(f"Failed to load private key: {e}", exc_info=True)
            return None

    def _full_path(self, path: str) -> str:
        """Get the full URL path for signing (e.g. /trade-api/v2/portfolio/balance).

        The httpx base_url handles routing, but signing must use the full path
        as seen by the Kalshi server.
        """
        from urllib.parse import urlparse
        parsed = urlparse(self.host)
        base_path = parsed.path.rstrip("/")
        return base_path + path

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Sign a request using RSA-PSS."""
        key = self._load_private_key()
        if not key:
            raise RuntimeError("No private key available for signing")
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        message = f"{timestamp}{method}{path}".encode()
        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        import base64
        return base64.b64encode(signature).decode()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.host,
                timeout=30.0,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                verify=ssl.create_default_context(),
            )
        return self._client

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate auth headers for a signed request."""
        if not self.api_key_id or not self.private_key_path:
            return {}
        # Kalshi expects timestamp in milliseconds
        timestamp = str(int(time.time() * 1000))
        # Sign with the full path (including /trade-api/v2 prefix)
        full_path = self._full_path(path)
        signature = self._sign_request(method.upper(), full_path, timestamp)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        """Make an authenticated request with retry logic and throttling.

        Uses a semaphore to limit concurrency and enforces a minimum interval
        between requests to avoid triggering Kalshi's rate limits.
        """
        import random
        async with self._semaphore:
            # Enforce minimum interval between requests
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.monotonic()

            client = await self._get_client()
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    headers = self._auth_headers(method, path)
                    if method.upper() == "GET":
                        resp = await client.get(path, params=params, headers=headers)
                    elif method.upper() == "POST":
                        resp = await client.post(path, json=json_body, headers=headers)
                    elif method.upper() == "DELETE":
                        resp = await client.delete(path, headers=headers)
                    else:
                        raise ValueError(f"Unsupported method: {method}")

                    if resp.status_code == 429:
                        if attempt < max_retries - 1:
                            wait = min(10, 2 ** (attempt + 1)) + random.uniform(0, 2 ** attempt)
                            logger.warning(f"Rate limited on {path}, waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(wait)
                            continue
                        logger.error(f"Rate limited on {path} after {max_retries} attempts")
                        raise KalshiRateLimitError(f"Rate limited on {path} after {max_retries} attempts")
                    resp.raise_for_status()
                    self._consecutive_timeouts = 0
                    if resp.status_code == 204:
                        return {}
                    return resp.json()
                except httpx.HTTPStatusError as e:
                    if e.response.status_code >= 500 and attempt < max_retries - 1:
                        wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                        logger.warning(f"Server error {e.response.status_code} on {path}, retrying in {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    raise
                except httpx.RequestError as e:
                    self._consecutive_timeouts += 1
                    if self._consecutive_timeouts >= 3:
                        logger.warning("3+ consecutive request errors — resetting HTTP connection pool")
                        if self._client and not self._client.is_closed:
                            await self._client.aclose()
                        self._client = None
                        self._consecutive_timeouts = 0
                    if attempt < max_retries - 1:
                        wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                        logger.warning(f"Request error on {path}: {e}, retrying in {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    raise
            return None

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ──────────────────────────────────────
    # Health Check
    # ──────────────────────────────────────

    async def health_check(self) -> bool:
        """Check if the Kalshi API is reachable."""
        try:
            result = await self._request("GET", "/exchange/status")
            return result is not None
        except Exception as e:
            logger.error(f"Health check failed: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────
    # Market Data (public)
    # ──────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: str = "open",
        event_ticker: Optional[str] = None,
    ) -> dict:
        """Fetch markets from Kalshi API. Returns {markets: [...], cursor: ...}."""
        params: dict[str, Any] = {
            "limit": min(limit, 200),
            "status": status,
        }
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker

        data = await self._request("GET", "/markets", params=params)
        if data is None:
            return {"markets": [], "cursor": None}
        return data

    async def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker."""
        try:
            data = await self._request("GET", f"/markets/{ticker}")
            if data and "market" in data:
                return data["market"]
            return data
        except Exception as e:
            logger.error(f"Failed to get market {ticker}: {e}", exc_info=True)
            return None

    async def get_events(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """Fetch events. Returns {events: [...], cursor: ...}."""
        params: dict[str, Any] = {
            "limit": min(limit, 200),
        }
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/events", params=params)
        if data is None:
            return {"events": [], "cursor": None}
        return data

    async def get_orderbook(self, ticker: str) -> Optional[dict]:
        """Get order book for a market."""
        try:
            data = await self._request("GET", f"/markets/{ticker}/orderbook")
            if data and "orderbook" in data:
                return data["orderbook"]
            return data
        except Exception as e:
            logger.error(f"Failed to get orderbook for {ticker}: {e}", exc_info=True)
            return None

    async def get_market_history(self, ticker: str, limit: int = 1000) -> list[dict]:
        """Fetch trade history for a market. Returns list of trade dicts."""
        all_trades: list[dict] = []
        cursor: Optional[str] = None

        try:
            while len(all_trades) < limit:
                params: dict[str, Any] = {
                    "limit": min(100, limit - len(all_trades)),
                    "ticker": ticker,
                }
                if cursor:
                    params["cursor"] = cursor

                data = await self._request("GET", f"/markets/trades", params=params)
                if not data:
                    break

                trades = data.get("trades", [])
                if not trades:
                    break

                all_trades.extend(trades)
                cursor = data.get("cursor")
                if not cursor:
                    break

        except Exception as e:
            logger.error(f"Failed to get trade history for {ticker}: {e}", exc_info=True)

        return all_trades

    # ──────────────────────────────────────
    # Account Data (auth required)
    # ──────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Get account balance in dollars."""
        try:
            data = await self._request("GET", "/portfolio/balance")
            if data and "balance" in data:
                # Validate balance is numeric before conversion
                raw_balance = data["balance"]
                try:
                    balance = float(raw_balance) / 100.0
                except (TypeError, ValueError):
                    logger.error(f"Invalid balance value from Kalshi: {raw_balance!r}")
                    return None
                if balance < 0:
                    logger.warning(f"Kalshi returned negative balance: ${balance:.2f}")
                    return 0.0
                if balance > 1_000_000:
                    logger.warning(f"Kalshi returned unusually large balance: ${balance:.2f}")
                return balance
            return None
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to get balance: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error("Unexpected error getting balance", exc_info=True)
            return None

    async def get_positions(self) -> list[dict]:
        """Get all open positions."""
        try:
            data = await self._request("GET", "/portfolio/positions")
            if data and "market_positions" in data:
                return data["market_positions"]
            return []
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to get positions: {e}", exc_info=True)
            return []
        except Exception as e:
            logger.error("Unexpected error getting positions", exc_info=True)
            return []

    # ──────────────────────────────────────
    # Order Management (auth required)
    # ──────────────────────────────────────

    async def create_order(
        self,
        ticker: str,
        side: str,
        yes_price: int,
        count: int,
        order_type: str = "limit",
        action: str = "buy",
    ) -> Optional[dict]:
        """Create an order on Kalshi.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            yes_price: Price in cents (1-99)
            count: Number of contracts
            order_type: "limit" or "market"
            action: "buy" or "sell"
        """
        try:
            body = {
                "ticker": ticker,
                "action": action,
                "side": side,
                "type": order_type,
                "count": count,
                "yes_price": yes_price,
            }
            data = await self._request("POST", "/portfolio/orders", json_body=body)
            if data and "order" in data:
                logger.info(f"Order created: {action} {count} {side} on {ticker} at {yes_price}c")
                return data["order"]
            return data
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to create order: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error("Unexpected error creating order", exc_info=True)
            return None

    async def cancel_order(self, order_id: str) -> Optional[dict]:
        """Cancel an open order."""
        try:
            data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
            logger.info(f"Order cancelled: {order_id}")
            return data
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to cancel order {order_id}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error cancelling order {order_id}", exc_info=True)
            return None

    async def get_order(self, order_id: str) -> Optional[dict]:
        """Get a single order by ID. Returns None if not found."""
        try:
            data = await self._request("GET", f"/portfolio/orders/{order_id}")
            if data and "order" in data:
                return data["order"]
            return None
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to get order {order_id}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting order {order_id}", exc_info=True)
            return None

    async def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        try:
            data = await self._request("GET", "/portfolio/orders", params={"status": "resting"})
            if data and "orders" in data:
                return data["orders"]
            return []
        except (httpx.HTTPStatusError, httpx.RequestError, KalshiRateLimitError) as e:
            logger.error(f"Failed to get open orders: {e}", exc_info=True)
            return []
        except Exception as e:
            logger.error("Unexpected error getting open orders", exc_info=True)
            return []
