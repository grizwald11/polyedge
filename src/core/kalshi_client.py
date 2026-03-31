"""Kalshi API client wrapper — authenticated trading operations.

Wraps kalshi-python SDK with retry logic, error handling, and typed responses.
Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in environment.
Uses RSA private key signing for authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Circuit breaker triggers after this many consecutive 5xx errors
MAX_CONSECUTIVE_5XX = 5


class TokenBucket:
    """H-2: Proactive token bucket rate limiter.

    Prevents 429s by limiting requests to a maximum rate, spreading them
    evenly over time. Tokens refill continuously at `rate` per second,
    up to `capacity`.
    """

    def __init__(self, rate: float = 8.0, capacity: float = 10.0):
        self._rate = rate  # tokens per second
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Wait for enough time to get 1 token
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)


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
        metrics: Optional[Any] = None,
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
        # H-2: Proactive token bucket — prevents 429s by throttling outgoing requests.
        # Kalshi's rate limit is ~10 req/s; we default to 8/s with burst up to 10.
        self._rate_limiter = TokenBucket(rate=8.0, capacity=10.0)
        self._consecutive_timeouts: int = 0
        self._consecutive_5xx: int = 0
        self._circuit_breaker_triggers: int = 0  # M-10: track for exponential backoff
        self._circuit_open_until: float = 0.0
        self._recovery_successes: int = 0  # L-1: half-open circuit breaker recovery
        self._metrics = metrics  # L-5: Optional Metrics instance for latency tracking

    def _load_private_key(self):
        """Load the RSA private key for API signing.

        Uses shared key_loader (L-2) for the actual PEM loading.
        """
        if self._private_key is not None:
            return self._private_key
        if self._key_load_attempted:
            return None
        if not self.private_key_path:
            return None
        self._key_load_attempted = True
        from src.core.key_loader import load_rsa_private_key
        self._private_key = load_rsa_private_key(
            self.private_key_path, check_permissions=True
        )
        if self._private_key is not None:
            import os

            # Record the mtime at load time for freshness checking (M-6)
            self._key_load_mtime: float = os.stat(self.private_key_path).st_mtime
            logger.info("Loaded RSA private key for Kalshi auth")
        return self._private_key

    def check_key_freshness(self) -> bool:
        """Check if the private key file has been modified since it was loaded.

        Compares the file's current mtime against the mtime recorded at load time.
        If the file has changed (e.g., cert rotation), clears the cached key so
        the next signing operation will reload it automatically.

        Call this periodically (e.g., every hour) to support key rotation without
        requiring a restart. The main scan loop or a scheduled task should invoke
        this method to ensure the bot picks up rotated credentials promptly.

        Returns:
            True if the key is still fresh (unchanged), False if it was stale and
            has been cleared for reload.
        """
        if not self.private_key_path or not self._private_key:
            return True  # No key loaded — nothing to check
        import os
        try:
            current_mtime = os.stat(self.private_key_path).st_mtime
            load_mtime = getattr(self, '_key_load_mtime', 0.0)
            if current_mtime != load_mtime:
                logger.info(
                    f"Private key file changed (mtime {load_mtime} → {current_mtime}) "
                    f"— clearing cached key for reload on next request"
                )
                self._private_key = None
                self._key_load_attempted = False
                return False
            return True
        except OSError as e:
            logger.warning(f"Could not check key freshness for {self.private_key_path}: {e}")
            return True

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
            # Circuit breaker: skip API calls if too many consecutive 5xx errors
            now_mono = time.monotonic()
            if now_mono < self._circuit_open_until:
                remaining = self._circuit_open_until - now_mono
                raise httpx.HTTPStatusError(
                    f"Circuit breaker open — {self._consecutive_5xx} consecutive 5xx errors. "
                    f"Retry in {remaining:.0f}s.",
                    request=httpx.Request("GET", self.host + path),
                    response=httpx.Response(503),
                )

            # H-2: Proactive rate limiting — wait for a token before proceeding.
            await self._rate_limiter.acquire()

            # Enforce minimum interval between requests
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.monotonic()

            client = await self._get_client()
            max_retries = 3
            auth_retried = False  # H-5: track single auth retry
            for attempt in range(max_retries):
                try:
                    headers = self._auth_headers(method, path)
                    _req_start = time.monotonic()
                    if method.upper() == "GET":
                        resp = await client.get(path, params=params, headers=headers)
                    elif method.upper() == "POST":
                        resp = await client.post(path, json=json_body, headers=headers)
                    elif method.upper() == "DELETE":
                        resp = await client.delete(path, headers=headers)
                    else:
                        raise ValueError(f"Unsupported method: {method}")
                    # L-5: Record API latency if metrics available
                    if self._metrics is not None:
                        _latency_ms = (time.monotonic() - _req_start) * 1000
                        self._metrics.record_api_latency(path, _latency_ms)

                    if resp.status_code == 429:
                        if attempt < max_retries - 1:
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    wait = float(retry_after)
                                    logger.warning(
                                        f"Rate limited on {path}, using Retry-After={wait:.1f}s "
                                        f"(attempt {attempt + 1}/{max_retries})"
                                    )
                                except ValueError:
                                    # H-4: Try HTTP-date format (e.g. "Sun, 30 Mar 2026 12:00:00 GMT")
                                    try:
                                        from email.utils import parsedate_to_datetime
                                        retry_dt = parsedate_to_datetime(retry_after)
                                        from datetime import datetime, timezone
                                        wait = max(0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
                                        logger.warning(
                                            f"Rate limited on {path}, parsed HTTP-date Retry-After, "
                                            f"waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                                        )
                                    except Exception:
                                        wait = min(10, 2 ** (attempt + 1) + random.uniform(0, 1))
                                        logger.warning(
                                            f"Rate limited on {path}, invalid Retry-After header "
                                            f"'{retry_after}', using backoff {wait:.1f}s "
                                            f"(attempt {attempt + 1}/{max_retries})"
                                        )
                            else:
                                wait = min(10, 2 ** (attempt + 1) + random.uniform(0, 1))
                                logger.warning(f"Rate limited on {path}, waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(wait)
                            continue
                        logger.error(f"Rate limited on {path} after {max_retries} attempts")
                        raise KalshiRateLimitError(f"Rate limited on {path} after {max_retries} attempts")

                    # H-5: Retry once on 401/403 auth errors with backoff
                    if resp.status_code in (401, 403) and not auth_retried:
                        auth_retried = True
                        logger.warning(
                            f"Auth error {resp.status_code} on {path}, retrying once after 2s"
                        )
                        await asyncio.sleep(2.0)
                        continue

                    resp.raise_for_status()
                    # Success — reset timeout counter and handle circuit breaker recovery
                    self._consecutive_timeouts = 0
                    if self._circuit_breaker_triggers > 0:
                        # L-1: Half-open pattern — require 3 consecutive successes
                        self._recovery_successes += 1
                        if self._recovery_successes >= 3:
                            logger.info(
                                f"Circuit breaker recovered after {self._recovery_successes} "
                                "consecutive successes"
                            )
                            self._consecutive_5xx = 0
                            self._circuit_breaker_triggers = 0
                            self._recovery_successes = 0
                    else:
                        self._consecutive_5xx = 0
                    if resp.status_code == 204:
                        return {}
                    # H-10: Wrap JSON parsing in try/except
                    try:
                        return resp.json()
                    except (ValueError, json.JSONDecodeError) as json_err:
                        logger.error(
                            f"Failed to parse JSON response from {path} "
                            f"(status {resp.status_code}): {json_err}"
                        )
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                            logger.warning(f"Retrying {path} after JSON parse failure in {wait:.1f}s")
                            await asyncio.sleep(wait)
                            continue
                        raise
                except httpx.HTTPStatusError as e:
                    # L-1: Reset recovery progress on any failure
                    self._recovery_successes = 0
                    # H-3: Track consecutive 5xx for circuit breaker
                    if e.response.status_code >= 500:
                        self._consecutive_5xx += 1
                        if self._consecutive_5xx >= MAX_CONSECUTIVE_5XX:
                            self._circuit_breaker_triggers += 1
                            backoff_seconds = min(600, 60 * (2 ** (self._circuit_breaker_triggers - 1)))
                            self._circuit_open_until = time.monotonic() + backoff_seconds
                            logger.error(
                                f"Circuit breaker OPEN — {self._consecutive_5xx} consecutive 5xx errors. "
                                f"Blocking requests for {backoff_seconds}s "
                                f"(trigger #{self._circuit_breaker_triggers})."
                            )
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                            logger.warning(f"Server error {e.response.status_code} on {path}, retrying in {wait:.1f}s")
                            await asyncio.sleep(wait)
                            continue
                    raise
                except httpx.RequestError as e:
                    # L-1: Reset recovery progress on any failure
                    self._recovery_successes = 0
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
            # L-5: All retry attempts exhausted without raising — should not normally
            # be reached, but return None explicitly rather than falling through.
            logger.error(f"All {max_retries} retry attempts exhausted for {method} {path}")
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
            market_data: Optional[dict] = None
            if data and "market" in data:
                market_data = data["market"]
            else:
                market_data = data
            # Validate that essential fields are present in the response
            if market_data:
                missing = [f for f in ("ticker", "status") if f not in market_data]
                if missing:
                    logger.warning(
                        f"get_market({ticker}): response missing expected fields {missing} "
                        f"— API schema may have changed"
                    )
            return market_data
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
        """Get account balance in dollars.

        Uses Decimal arithmetic internally to avoid float rounding errors
        on monetary values (C-1 audit fix).
        """
        from decimal import Decimal, ROUND_HALF_UP

        try:
            data = await self._request("GET", "/portfolio/balance")
            if data and "balance" in data:
                # Validate balance is numeric before conversion
                raw_balance = data["balance"]
                try:
                    # C-1: Use Decimal to avoid float precision loss on money.
                    balance = float(
                        (Decimal(str(raw_balance)) / Decimal(100))
                        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    )
                except (TypeError, ValueError, ArithmeticError):
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
