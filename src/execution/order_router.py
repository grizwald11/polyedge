"""Order router — routes orders through paper or live execution.

Paper mode simulates fills at the order price.
Live mode submits to Kalshi API via KalshiClient.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.core.kalshi_client import KalshiClient
from src.core.models import (
    Order, OrderStatus, Trade, dollars_to_cents,
    kalshi_maker_fee, kalshi_taker_fee, OrderType,
)
from src.storage.database import Database

logger = logging.getLogger(__name__)


class OrderResult:
    """Result of order routing."""

    def __init__(
        self,
        success: bool,
        order: Order,
        trade: Optional[Trade] = None,
        error: str = "",
    ):
        self.success = success
        self.order = order
        self.trade = trade
        self.error = error


class OrderRouter:
    """Routes orders to paper or live execution."""

    def __init__(self, settings: Settings, kalshi: KalshiClient, db: Database):
        self.settings = settings
        self.kalshi = kalshi
        self.db = db

    async def route_order(self, order: Order) -> OrderResult:
        """Route an order based on current trading mode.

        Paper mode: simulates immediate fill at order price.
        Live mode: submits to Kalshi API.
        """
        if order.paper or self.settings.trading.mode == "paper":
            return await self._paper_fill(order)
        else:
            return await self._live_fill(order)

    async def _paper_fill(self, order: Order) -> OrderResult:
        """Simulate a fill in paper trading mode."""
        now = datetime.now(timezone.utc)

        # Simulate fill at order price
        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = order.price

        # Calculate fee
        price_cents = dollars_to_cents(order.price)
        if order.order_type == OrderType.GTC:
            fee_cents = kalshi_maker_fee(int(order.size), price_cents)
        else:
            fee_cents = kalshi_taker_fee(int(order.size), price_cents)
        fee_dollars = fee_cents / 100.0

        # Create trade record
        trade = Trade(
            order_id=order.id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            fee=fee_dollars,
            realized_pnl=0.0,  # P&L calculated on position close
            strategy=order.strategy,
            paper=True,
            timestamp=now,
        )

        # Persist to database
        self._log_order(order)
        self.db.log_trade(trade)

        logger.info(
            f"[PAPER] Filled: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"(cost=${order.cost:.2f}, fee=${fee_dollars:.2f})"
        )

        return OrderResult(success=True, order=order, trade=trade)

    async def _live_fill(self, order: Order) -> OrderResult:
        """Submit order to Kalshi API for live execution."""
        # Three-gate safety check
        if not self._live_gates_passed():
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Live trading gates not passed"
            self._log_order(order)
            return OrderResult(
                success=False, order=order,
                error="Live trading gates not passed"
            )

        # Determine Kalshi side and order type
        kalshi_side = "yes" if "yes" in order.token_id.lower() else "no"
        kalshi_type = "limit" if order.order_type == OrderType.GTC else "market"
        yes_price = dollars_to_cents(order.price)

        try:
            result = await self.kalshi.create_order(
                ticker=order.market_id,
                side=kalshi_side,
                yes_price=yes_price,
                count=int(order.size),
                order_type=kalshi_type,
                action=order.side.value.lower(),
            )

            if result is None:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "Kalshi API returned None"
                self._log_order(order)
                return OrderResult(
                    success=False, order=order, error="Kalshi API returned None"
                )

            # Update order with Kalshi response
            order.status = OrderStatus.FILLED
            order.filled_at = datetime.now(timezone.utc)
            order.fill_price = order.price

            # Create trade record
            price_cents = dollars_to_cents(order.price)
            if order.order_type == OrderType.GTC:
                fee_cents = kalshi_maker_fee(int(order.size), price_cents)
            else:
                fee_cents = kalshi_taker_fee(int(order.size), price_cents)

            trade = Trade(
                order_id=order.id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                price=order.price,
                size=order.size,
                fee=fee_cents / 100.0,
                realized_pnl=0.0,
                strategy=order.strategy,
                paper=False,
                timestamp=datetime.now(timezone.utc),
            )

            self._log_order(order)
            self.db.log_trade(trade)

            logger.info(
                f"[LIVE] Filled: {order.side.value} {int(order.size)}x "
                f"{order.token_id} @ ${order.price:.2f}"
            )

            return OrderResult(success=True, order=order, trade=trade)

        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = str(e)
            self._log_order(order)
            logger.error(f"Live order failed: {e}")
            return OrderResult(success=False, order=order, error=str(e))

    def _live_gates_passed(self) -> bool:
        """Three-gate safety system for live trading."""
        # Gate 1: Config mode must be "live"
        if self.settings.trading.mode != "live":
            logger.warning("Gate 1 failed: trading.mode is not 'live'")
            return False
        # Gate 2: Environment variable
        if not self.settings.live_enabled:
            logger.warning("Gate 2 failed: POLYEDGE_LIVE_ENABLED is not true")
            return False
        # Gate 3 is interactive confirmation — handled externally
        return True

    def _log_order(self, order: Order):
        """Persist order to database."""
        conn = self.db._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO orders (
                    id, market_id, token_id, side, price, size, cost,
                    order_type, fee_rate_bps, status, strategy, signal_id,
                    paper, created_at, filled_at, fill_price, cancelled_at, rejection_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.id,
                order.market_id,
                order.token_id,
                order.side.value,
                order.price,
                order.size,
                order.cost,
                order.order_type.value,
                order.fee_rate_bps,
                order.status.value,
                order.strategy.value,
                order.signal_id,
                int(order.paper),
                order.created_at.isoformat(),
                order.filled_at.isoformat() if order.filled_at else None,
                order.fill_price,
                order.cancelled_at.isoformat() if order.cancelled_at else None,
                order.rejection_reason,
            ))
            conn.commit()
        finally:
            conn.close()
