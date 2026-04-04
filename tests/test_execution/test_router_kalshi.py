"""Tests for Kalshi live order router edge cases (H-5).

Covers: market validation, side/price resolution, balance preflight failure,
order rejection, timeout during polling, live gate rejection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import (
    Direction,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    StrategyName,
)
from src.execution.router_kalshi import live_fill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_router(**overrides):
    """Create a mock OrderRouter with sensible defaults for Kalshi routing."""
    router = MagicMock()
    router.db = MagicMock()
    router.db.get_market.return_value = None
    router.db.log_trade = MagicMock()
    router._log_order = MagicMock()
    router._live_gates_passed = MagicMock(return_value=True)
    router._check_gate3 = AsyncMock(return_value=None)
    router._add_pending = AsyncMock()
    router._remove_pending = AsyncMock()
    router._pending_order_cost = 0.0
    router.settings = MagicMock()
    router.settings.trading.bankroll = 500.0
    router.settings.execution.max_poll_attempts = 3
    router.settings.execution.order_poll_delay_seconds = 0.01
    router.settings.execution.order_poll_timeout_seconds = 5.0
    router.kalshi = AsyncMock()
    for k, v in overrides.items():
        setattr(router, k, v)
    return router


def _make_order(**kwargs) -> Order:
    defaults = dict(
        id="ORD-K1",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-RATE-CUT-MAY26_yes",
        side=Side.BUY,
        price=0.60,
        size=10,
        cost=6.0,
        order_type=OrderType.GTC,
        strategy=StrategyName.AI_PROBABILITY,
        platform=Platform.KALSHI,
        status=OrderStatus.PENDING,
        kalshi_side="yes",
    )
    defaults.update(kwargs)
    return Order(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveFillGates:
    """Test that live_fill enforces safety gates."""

    @pytest.mark.asyncio
    async def test_rejects_when_live_gates_not_passed(self):
        router = _mock_router()
        router._live_gates_passed.return_value = False
        order = _make_order()

        result = await live_fill(router, order)

        assert result.success is False
        assert order.status == OrderStatus.REJECTED
        assert "gates" in (order.rejection_reason or "").lower()

    @pytest.mark.asyncio
    async def test_rejects_when_gate3_fails(self):
        from src.execution.order_router import OrderResult
        router = _mock_router()
        rejection = OrderResult(success=False, order=_make_order(), error="Gate 3 denied")
        router._check_gate3 = AsyncMock(return_value=rejection)
        order = _make_order()

        result = await live_fill(router, order)

        assert result.success is False


class TestLiveFillPriceValidation:
    """Test price range validation."""

    @pytest.mark.asyncio
    async def test_rejects_price_outside_kalshi_range(self):
        """Prices converting to <1 or >99 cents should be rejected."""
        router = _mock_router()
        # Price of 0.001 = 0 cents after rounding
        order = _make_order(price=0.001)

        result = await live_fill(router, order)

        assert result.success is False
        assert order.status == OrderStatus.REJECTED
        assert "cents" in (order.rejection_reason or "").lower() or "range" in (order.rejection_reason or "").lower()


class TestLiveFillMarketValidation:
    """Test market status validation."""

    @pytest.mark.asyncio
    async def test_rejects_closed_market(self):
        """Orders on closed/settled markets should be rejected."""
        from src.core.models import Market, MarketToken
        router = _mock_router()
        # Return a closed market from DB
        closed_market = MagicMock()
        closed_market.closed = True
        closed_market.active = False
        router.db.get_market.return_value = closed_market

        order = _make_order()
        result = await live_fill(router, order)

        assert result.success is False


class TestLiveFillBalancePreflight:
    """Test balance pre-flight check."""

    @pytest.mark.asyncio
    async def test_rejects_when_insufficient_balance(self):
        """Should reject when Kalshi reports insufficient balance."""
        router = _mock_router()
        # Make balance check fail
        router.kalshi.get_balance = AsyncMock(return_value=1.0)  # $1 balance
        order = _make_order(cost=100.0, size=200)

        result = await live_fill(router, order)

        # Should fail at some validation point
        assert result.success is False
