"""Order builder — constructs limit and market orders for Kalshi and Polymarket.

Validates inputs, calculates costs and fees, and produces Order objects
ready for routing through paper or live execution. Fee calculation is
platform-aware: Kalshi charges maker/taker fees, Polymarket event markets
are fee-free.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import (
    Direction, Market, Order, OrderType, OrderStatus, Platform, Side, Signal,
    StrategyName, dollars_to_cents, kalshi_maker_fee, kalshi_taker_fee,
    polymarket_fee,
)

logger = logging.getLogger(__name__)


class OrderBuilder:
    """Builds orders for Kalshi and Polymarket execution."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _calculate_fee(self, platform: Platform, size: int, price: float, maker: bool) -> float:
        """Calculate fee in dollars based on platform and order type."""
        if platform == Platform.POLYMARKET:
            return 0.0  # Event markets are fee-free
        price_cents = dollars_to_cents(price)
        if maker:
            return kalshi_maker_fee(size, price_cents) / 100.0
        return kalshi_taker_fee(size, price_cents) / 100.0

    def build_limit_order(
        self,
        market: Market,
        signal: Signal,
        size: int,
        price: float,
    ) -> Order:
        """Build a GTC limit (maker) order.

        Args:
            market: Market to trade
            signal: Signal that triggered this order
            size: Number of contracts
            price: Price per contract in dollars (0.01-0.99)

        Returns:
            Order ready for routing
        """
        side, token_id = self._resolve_side_and_token(market, signal.direction)
        price = self._clamp_price(price)
        fee_dollars = self._calculate_fee(market.platform, size, price, maker=True)
        cost = price * size + fee_dollars
        fee_bps = 0 if market.platform == Platform.POLYMARKET else 175

        order = Order(
            id=self._generate_order_id(),
            market_id=market.ticker,
            platform=market.platform,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            cost=cost,
            order_type=OrderType.GTC,
            fee_rate_bps=fee_bps,
            status=OrderStatus.PENDING,
            strategy=signal.strategy,
            signal_id=signal.id,
            paper=self.settings.trading.mode == "paper",
            created_at=datetime.now(timezone.utc),
        )

        logger.debug(
            f"Built limit order [{market.platform.value}]: {side.value} {size}x {token_id} "
            f"@ ${price:.2f} (cost=${cost:.2f}, fee=${fee_dollars:.2f})"
        )
        return order

    def build_market_order(
        self,
        market: Market,
        signal: Signal,
        size: int,
    ) -> Order:
        """Build a FOK market (taker) order.

        Uses the current best price from the market. Fills immediately or cancels.

        Args:
            market: Market to trade
            signal: Signal that triggered this order
            size: Number of contracts

        Returns:
            Order ready for routing
        """
        side, token_id = self._resolve_side_and_token(market, signal.direction)

        # Use current market price for the appropriate side
        if signal.direction in (Direction.BUY_YES, Direction.SELL_YES):
            price = market.yes_price
        else:
            price = market.no_price

        # Reject if price is missing/zero — don't silently clamp to $0.01
        if price <= 0:
            raise ValueError(
                f"Cannot build market order: {signal.direction.value} price is "
                f"${price:.4f} for {market.ticker} (missing market token data)"
            )

        price = self._clamp_price(price)
        fee_dollars = self._calculate_fee(market.platform, size, price, maker=False)
        cost = price * size + fee_dollars
        fee_bps = 0 if market.platform == Platform.POLYMARKET else 700

        order = Order(
            id=self._generate_order_id(),
            market_id=market.ticker,
            platform=market.platform,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            cost=cost,
            order_type=OrderType.FOK,
            fee_rate_bps=fee_bps,
            status=OrderStatus.PENDING,
            strategy=signal.strategy,
            signal_id=signal.id,
            paper=self.settings.trading.mode == "paper",
            created_at=datetime.now(timezone.utc),
        )

        logger.debug(
            f"Built market order: {side.value} {size}x {token_id} "
            f"@ ${price:.2f} (cost=${cost:.2f}, fee=${fee_dollars:.2f})"
        )
        return order

    def _resolve_side_and_token(
        self, market: Market, direction: Direction
    ) -> tuple[Side, str]:
        """Map a Direction to Kalshi side + token_id."""
        if direction == Direction.BUY_YES:
            token = market.yes_token
            return Side.BUY, token.token_id if token else f"{market.ticker}_yes"
        elif direction == Direction.BUY_NO:
            token = market.no_token
            return Side.BUY, token.token_id if token else f"{market.ticker}_no"
        elif direction == Direction.SELL_YES:
            token = market.yes_token
            return Side.SELL, token.token_id if token else f"{market.ticker}_yes"
        else:  # SELL_NO
            token = market.no_token
            return Side.SELL, token.token_id if token else f"{market.ticker}_no"

    @staticmethod
    def _clamp_price(price: float) -> float:
        """Clamp price to valid Kalshi range (0.01-0.99)."""
        clamped = max(0.01, min(0.99, round(price, 2)))
        if clamped != round(price, 2):
            logger.debug(f"Price clamped: ${price:.4f} → ${clamped:.2f}")
        return clamped

    @staticmethod
    def _generate_order_id() -> str:
        return f"PE-{uuid.uuid4().hex[:12]}"
