"""Kalshi WebSocket client — real-time price feeds and fill notifications.

Maintains a persistent connection to Kalshi's WebSocket API, subscribes
to ticker/fill/lifecycle channels, and dispatches updates via callbacks.
Auto-reconnects on disconnect with exponential backoff.

Endpoints:
  Production: wss://api.elections.kalshi.com/trade-api/ws/v2
  Demo:       wss://demo-api.kalshi.co/trade-api/ws/v2
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# WebSocket path used for auth signing
WS_PATH = "/trade-api/ws/v2"

# Reconnect parameters
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
BACKOFF_MULTIPLIER = 2.0


@dataclass
class TickerUpdate:
    """Parsed ticker channel message."""

    market_ticker: str
    price: float  # last trade price in dollars
    yes_bid: float
    yes_ask: float
    volume: float
    open_interest: float
    ts: int  # unix ms


@dataclass
class FillUpdate:
    """Parsed fill channel message (private — your orders)."""

    order_id: str
    market_ticker: str
    side: str
    price: float
    count: int
    ts: int


@dataclass
class LifecycleUpdate:
    """Parsed market lifecycle message."""

    market_ticker: str
    status: str  # open, closed, determined
    settlement_value: Optional[float] = None


# Callback types
PriceCallback = Callable[[TickerUpdate], Coroutine[Any, Any, None]]
FillCallback = Callable[[FillUpdate], Coroutine[Any, Any, None]]
LifecycleCallback = Callable[[LifecycleUpdate], Coroutine[Any, Any, None]]


class KalshiWebSocket:
    """WebSocket client for real-time Kalshi market data.

    Usage:
        ws = KalshiWebSocket(host, api_key_id, private_key_path)
        ws.on_price_update(my_price_handler)
        ws.subscribe(["TICKER-A", "TICKER-B"])
        await ws.connect()  # blocks, auto-reconnects
    """

    def __init__(
        self,
        host: str = "wss://demo-api.kalshi.co/trade-api/ws/v2",
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
    ):
        self.host = host
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self._private_key = None

        self._subscriptions: set[str] = set()
        self._channels: list[str] = ["ticker", "fill"]
        self._ws = None
        self._cmd_id: int = 0
        self._running: bool = False

        # Callbacks keyed by id to prevent duplicates and allow removal
        self._price_callbacks: dict[int, PriceCallback] = {}
        self._fill_callbacks: dict[int, FillCallback] = {}
        self._lifecycle_callbacks: dict[int, LifecycleCallback] = {}

    # ── Subscription management ────────────────────

    def subscribe(self, tickers: list[str]):
        """Add tickers to subscription set. Sends subscribe if connected."""
        new = set(tickers) - self._subscriptions
        if not new:
            return
        self._subscriptions.update(new)
        if self._ws is not None:
            task = asyncio.create_task(self._send_subscribe(list(new)))
            task.add_done_callback(self._log_task_exception)

    def unsubscribe(self, tickers: list[str]):
        """Remove tickers from subscription set."""
        removing = set(tickers) & self._subscriptions
        if not removing:
            return
        self._subscriptions -= removing
        if self._ws is not None:
            task = asyncio.create_task(self._send_unsubscribe(list(removing)))
            task.add_done_callback(self._log_task_exception)

    def set_channels(self, channels: list[str]):
        """Set which channels to subscribe to (ticker, fill, orderbook_delta, etc.)."""
        self._channels = channels

    # ── Callback registration ──────────────────────

    def on_price_update(self, callback: PriceCallback) -> int:
        cb_id = id(callback)
        self._price_callbacks[cb_id] = callback
        return cb_id

    def on_fill(self, callback: FillCallback) -> int:
        cb_id = id(callback)
        self._fill_callbacks[cb_id] = callback
        return cb_id

    def on_lifecycle(self, callback: LifecycleCallback) -> int:
        cb_id = id(callback)
        self._lifecycle_callbacks[cb_id] = callback
        return cb_id

    def remove_callback(self, cb_id: int) -> bool:
        """Remove a previously registered callback by its id.

        Returns True if the callback was found and removed.
        """
        for registry in (self._price_callbacks, self._fill_callbacks, self._lifecycle_callbacks):
            if cb_id in registry:
                del registry[cb_id]
                return True
        return False

    # ── Connection lifecycle ───────────────────────

    async def connect(self):
        """Connect and run the message loop. Auto-reconnects on failure.

        This is a blocking call — run it as an asyncio task.
        """
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed — WebSocket client disabled")
            return

        self._running = True
        backoff = INITIAL_BACKOFF
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 10  # Stop after 10 consecutive failures (likely permanent)

        while self._running:
            try:
                headers = self._auth_headers()
                # Explicit SSL context avoids Python 3.12 segfault in
                # asyncio TLS on macOS ARM64 (null-deref in ssl.read).
                ssl_ctx = ssl.create_default_context()
                async with websockets.connect(
                    self.host,
                    additional_headers=headers,
                    ping_interval=None,  # Kalshi sends its own pings
                    ssl=ssl_ctx,
                ) as ws:
                    self._ws = ws
                    backoff = INITIAL_BACKOFF
                    consecutive_failures = 0
                    logger.info(f"WebSocket connected to {self.host}")

                    # Resubscribe to all tickers
                    if self._subscriptions:
                        await self._send_subscribe(list(self._subscriptions))

                    await self._message_loop(ws)

            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled")
                self._running = False
                break
            except Exception as e:
                self._ws = None
                if not self._running:
                    break
                consecutive_failures += 1
                # Detect permanent auth failures — check HTTP status code first
                # (websockets wraps it in InvalidStatusCode), then fall back to
                # string matching for other exception types.
                is_auth_error = False
                if hasattr(e, "status_code"):
                    is_auth_error = getattr(e, "status_code", 0) in (401, 403)
                if not is_auth_error:
                    err_str = str(e).lower()
                    is_auth_error = any(code in err_str for code in ("401", "403", "authentication", "unauthorized"))
                if is_auth_error or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        f"WebSocket permanently failed after {consecutive_failures} attempts: {e}. "
                        f"Stopping reconnect loop."
                    )
                    break
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)

        self._ws = None
        logger.info("WebSocket client stopped")

    async def close(self):
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"WebSocket close error: {e}")
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    @staticmethod
    def _log_task_exception(task: asyncio.Task):
        """Callback to log exceptions from fire-and-forget tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"WebSocket background task failed: {exc}", exc_info=exc)

    # ── Internal ───────────────────────────────────

    async def _run_callbacks(self, callbacks, update, label: str):
        """Run callbacks concurrently instead of sequentially."""
        if not callbacks:
            return
        cb_list = list(callbacks.values()) if isinstance(callbacks, dict) else callbacks
        results = await asyncio.gather(
            *(cb(update) for cb in cb_list),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"{label} callback error: {result}")

    async def _message_loop(self, ws):
        """Process incoming WebSocket messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await self._dispatch(msg)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON WebSocket message: {raw[:100]}")
            except Exception as e:
                logger.error(f"Error processing WebSocket message: {e}", exc_info=True)

    async def _dispatch(self, msg: dict):
        """Route a parsed message to the appropriate handler."""
        msg_type = msg.get("type", "")

        if msg_type == "ticker":
            update = self._parse_ticker(msg)
            if update:
                await self._run_callbacks(self._price_callbacks, update, "Price")

        elif msg_type == "fill":
            update = self._parse_fill(msg)
            if update:
                await self._run_callbacks(self._fill_callbacks, update, "Fill")

        elif msg_type == "market_lifecycle_v2":
            update = self._parse_lifecycle(msg)
            if update:
                await self._run_callbacks(self._lifecycle_callbacks, update, "Lifecycle")

        elif msg_type in ("subscribed", "unsubscribed", "error"):
            if msg_type == "error":
                logger.error(f"WebSocket error: {msg}")
            else:
                logger.debug(f"WebSocket {msg_type}: {msg}")

    def _parse_ticker(self, msg: dict) -> Optional[TickerUpdate]:
        """Parse a ticker channel message."""
        try:
            data = msg.get("msg", msg)
            return TickerUpdate(
                market_ticker=data.get("market_ticker", ""),
                price=float(data.get("price_dollars", data.get("price", 0))),
                yes_bid=float(data.get("yes_bid_dollars", data.get("yes_bid", 0))),
                yes_ask=float(data.get("yes_ask_dollars", data.get("yes_ask", 0))),
                volume=float(data.get("volume_fp", data.get("volume", 0))),
                open_interest=float(data.get("open_interest_fp", data.get("open_interest", 0))),
                ts=int(data.get("ts", 0)),
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to parse ticker: {e}")
            return None

    def _parse_fill(self, msg: dict) -> Optional[FillUpdate]:
        """Parse a fill channel message."""
        try:
            data = msg.get("msg", msg)
            return FillUpdate(
                order_id=data.get("order_id", ""),
                market_ticker=data.get("market_ticker", data.get("ticker", "")),
                side=data.get("side", ""),
                price=float(data.get("yes_price_dollars", data.get("price", 0))),
                count=int(data.get("count_fp", data.get("count", 0))),
                ts=int(data.get("ts", 0)),
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to parse fill: {e}")
            return None

    def _parse_lifecycle(self, msg: dict) -> Optional[LifecycleUpdate]:
        """Parse a market lifecycle message."""
        try:
            data = msg.get("msg", msg)
            settlement = data.get("settlement_value")
            settlement_float = float(settlement) if settlement is not None else None
            if settlement_float is not None and not (0.0 <= settlement_float <= 1.0):
                logger.warning(
                    f"Settlement value {settlement_float} out of range [0, 1] "
                    f"for {data.get('market_ticker', '?')} — clamping"
                )
                settlement_float = max(0.0, min(1.0, settlement_float))
            return LifecycleUpdate(
                market_ticker=data.get("market_ticker", ""),
                status=data.get("status", ""),
                settlement_value=settlement_float,
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to parse lifecycle: {e}")
            return None

    async def _send_subscribe(self, tickers: list[str]):
        """Send a subscribe command."""
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": self._channels,
                "market_tickers": tickers,
            },
        }
        await self._send(cmd)
        logger.info(f"Subscribed to {len(tickers)} tickers: {tickers[:5]}{'...' if len(tickers) > 5 else ''}")

    async def _send_unsubscribe(self, tickers: list[str]):
        """Send an unsubscribe command."""
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "unsubscribe",
            "params": {
                "channels": self._channels,
                "market_tickers": tickers,
            },
        }
        await self._send(cmd)

    async def _send(self, data: dict):
        """Send JSON to WebSocket."""
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(data))
        except Exception as e:
            logger.warning(f"WebSocket send failed: {e}")

    def _auth_headers(self) -> dict[str, str]:
        """Generate authentication headers for the WebSocket handshake."""
        if not self.api_key_id or not self.private_key_path:
            logger.warning("WebSocket auth: missing api_key_id or private_key_path — connecting without auth (no fills)")
            return {}

        key = self._load_private_key()
        if key is None:
            logger.error("WebSocket auth: failed to load private key — fills will NOT be received")
            return {}

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}GET{WS_PATH}".encode()

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _load_private_key(self):
        """Load the RSA private key for API signing."""
        if self._private_key is not None:
            return self._private_key
        if not self.private_key_path:
            return None
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            with open(self.private_key_path, "rb") as f:
                self._private_key = load_pem_private_key(f.read(), password=None)
            return self._private_key
        except Exception as e:
            logger.error(f"Failed to load private key: {e}", exc_info=True)
            return None
