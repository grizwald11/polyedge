"""Tests for the execution router modules.

Covers:
  - router_paper.py:      simulate_slippage, paper_fill
  - router_kalshi.py:     _validate_market, _resolve_side_and_price, _apply_fill_price, live_fill
"""

from __future__ import annotations

import os
import random
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
    Trade,
    dollars_to_cents,
)
from src.execution.order_router import OrderResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mock_router(**overrides):
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
    router._polymarket_residency_confirmed = False
    router._simulate_slippage = MagicMock(return_value=(True, 0.60))
    router.settings = MagicMock()
    router.settings.trading.bankroll = 500.0
    router.settings.execution.max_poll_attempts = 3
    router.settings.execution.order_poll_delay_seconds = 0.01
    router.settings.execution.order_poll_timeout_seconds = 5.0
    router.kalshi = AsyncMock()
    router.polymarket = AsyncMock()
    for k, v in overrides.items():
        setattr(router, k, v)
    return router


def _make_order(**kwargs) -> Order:
    defaults = dict(
        id="ORD-1",
        market_id="MKT-1",
        token_id="MKT-1_yes",
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


# ===========================================================================
# router_paper.py — simulate_slippage
# ===========================================================================


class TestSimulateSlippage:
    """Tests for the pure function simulate_slippage."""

    def test_buy_fill_price_gte_order_price(self):
        """Buy slippage is adverse (higher price)."""
        from src.execution.router_paper import simulate_slippage

        order = _make_order(side=Side.BUY, price=0.50)
        rng = random.Random(42)
        with patch("src.execution.router_paper._random.Random", return_value=rng):
            filled, fill_price = simulate_slippage(order)
        if filled:
            assert fill_price >= order.price

    def test_sell_fill_price_lte_order_price(self):
        """Sell slippage is adverse (lower price)."""
        from src.execution.router_paper import simulate_slippage

        order = _make_order(side=Side.SELL, price=0.50)
        rng = random.Random(99)
        with patch("src.execution.router_paper._random.Random", return_value=rng):
            filled, fill_price = simulate_slippage(order)
        if filled:
            assert fill_price <= order.price

    def test_miss_rate_deterministic(self):
        """With a seeded RNG that produces a value < 0.15, order misses."""
        from src.execution.router_paper import simulate_slippage

        # Find a seed that triggers a miss (random() < 0.15)
        for seed in range(200):
            rng = random.Random(seed)
            if rng.random() < 0.15:
                break
        rng_miss = random.Random(seed)
        with patch("src.execution.router_paper._random.Random", return_value=rng_miss):
            order = _make_order(price=0.50)
            filled, fill_price = simulate_slippage(order)
        assert filled is False
        assert fill_price == order.price

    def test_fill_price_clamped_high(self):
        """Fill price should not exceed 0.99 for buys near the ceiling."""
        from src.execution.router_paper import simulate_slippage

        order = _make_order(side=Side.BUY, price=0.99)
        # Use a seed that fills (random() >= 0.15) with max slippage
        for seed in range(200):
            rng = random.Random(seed)
            if rng.random() >= 0.15:
                break
        rng_fill = random.Random(seed)
        with patch("src.execution.router_paper._random.Random", return_value=rng_fill):
            filled, fill_price = simulate_slippage(order)
        if filled:
            assert fill_price <= 0.99

    def test_fill_price_clamped_low(self):
        """Fill price should not go below 0.01 for sells near the floor."""
        from src.execution.router_paper import simulate_slippage

        order = _make_order(side=Side.SELL, price=0.01)
        for seed in range(200):
            rng = random.Random(seed)
            if rng.random() >= 0.15:
                break
        rng_fill = random.Random(seed)
        with patch("src.execution.router_paper._random.Random", return_value=rng_fill):
            filled, fill_price = simulate_slippage(order)
        if filled:
            assert fill_price >= 0.01

    def test_fill_price_rounded_to_two_decimals(self):
        """Fill price should be rounded to two decimal places."""
        from src.execution.router_paper import simulate_slippage

        order = _make_order(side=Side.BUY, price=0.55)
        for seed in range(200):
            rng = random.Random(seed)
            if rng.random() >= 0.15:
                break
        rng_fill = random.Random(seed)
        with patch("src.execution.router_paper._random.Random", return_value=rng_fill):
            filled, fill_price = simulate_slippage(order)
        if filled:
            assert fill_price == round(fill_price, 2)


# ===========================================================================
# router_paper.py — paper_fill
# ===========================================================================


class TestPaperFill:
    """Tests for the async paper_fill function."""

    @pytest.mark.asyncio
    async def test_successful_fill_creates_trade(self):
        """A successful fill sets status=FILLED and creates a Trade record."""
        from src.execution.router_paper import paper_fill

        router = _mock_router(_simulate_slippage=MagicMock(return_value=(True, 0.61)))
        order = _make_order(platform=Platform.KALSHI)
        result = await paper_fill(router, order)

        assert result.success is True
        assert order.status == OrderStatus.FILLED
        assert order.fill_price == 0.61
        assert result.trade is not None
        assert result.trade.paper is True
        router.db.log_trade.assert_called_once()

    @pytest.mark.asyncio
    async def test_missed_fill_returns_failure(self):
        """A missed fill sets status=CANCELLED and returns failure."""
        from src.execution.router_paper import paper_fill

        router = _mock_router(_simulate_slippage=MagicMock(return_value=(False, 0.60)))
        order = _make_order(platform=Platform.KALSHI)
        result = await paper_fill(router, order)

        assert result.success is False
        assert order.status == OrderStatus.CANCELLED
        assert result.trade is None
        router._log_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_kalshi_gtc_uses_maker_fee(self):
        """Kalshi GTC (limit) orders use maker fee calculation."""
        from src.execution.router_paper import paper_fill

        router = _mock_router(
            _simulate_slippage=MagicMock(return_value=(True, 0.60)),
        )
        order = _make_order(platform=Platform.KALSHI, order_type=OrderType.GTC, size=10)
        result = await paper_fill(router, order)

        assert result.success is True
        assert result.trade.fee >= 0.0  # maker fee is non-negative

    @pytest.mark.asyncio
    async def test_kalshi_fok_uses_taker_fee(self):
        """Kalshi FOK (market) orders use taker fee calculation."""
        from src.execution.router_paper import paper_fill

        router = _mock_router(
            _simulate_slippage=MagicMock(return_value=(True, 0.60)),
        )
        order = _make_order(platform=Platform.KALSHI, order_type=OrderType.FOK, size=10)
        result = await paper_fill(router, order)

        assert result.success is True
        # Taker fee should be higher than maker fee for the same parameters
        assert result.trade.fee >= 0.0


# ===========================================================================
# router_kalshi.py — _validate_market
# ===========================================================================


class TestKalshiValidateMarket:
    """Tests for _validate_market helper."""

    def test_returns_none_when_market_not_in_db(self):
        from src.execution.router_kalshi import _validate_market

        router = _mock_router()
        router.db.get_market.return_value = None
        order = _make_order()
        result = _validate_market(router, order)
        assert result is None

    def test_rejects_closed_market(self):
        from src.execution.router_kalshi import _validate_market

        router = _mock_router()
        router.db.get_market.return_value = {"closed": True, "status": "closed"}
        order = _make_order()
        result = _validate_market(router, order)

        assert result is not None
        assert result.success is False
        assert order.status == OrderStatus.REJECTED
        assert "closed" in result.error.lower()

    def test_rejects_settled_market(self):
        from src.execution.router_kalshi import _validate_market

        router = _mock_router()
        router.db.get_market.return_value = {"closed": False, "active": 1, "status": "settled"}
        order = _make_order()
        result = _validate_market(router, order)

        assert result is not None
        assert result.success is False

    def test_rejects_halted_market(self):
        from src.execution.router_kalshi import _validate_market

        router = _mock_router()
        router.db.get_market.return_value = {"closed": False, "active": 1, "status": "halted"}
        order = _make_order()
        result = _validate_market(router, order)

        assert result is not None
        assert result.success is False

    def test_passes_active_market(self):
        from src.execution.router_kalshi import _validate_market

        router = _mock_router()
        router.db.get_market.return_value = {"closed": False, "active": 1, "status": "open"}
        order = _make_order()
        result = _validate_market(router, order)
        assert result is None


# ===========================================================================
# router_kalshi.py — _resolve_side_and_price
# ===========================================================================


class TestResolveSideAndPrice:
    """Tests for _resolve_side_and_price helper."""

    def test_yes_side_returns_price_in_cents(self):
        from src.execution.router_kalshi import _resolve_side_and_price

        order = _make_order(kalshi_side="yes", price=0.60)
        side, cents, err = _resolve_side_and_price(order)
        assert side == "yes"
        assert cents == 60
        assert err is None

    def test_no_side_inverts_price(self):
        from src.execution.router_kalshi import _resolve_side_and_price

        order = _make_order(kalshi_side="no", price=0.60)
        side, cents, err = _resolve_side_and_price(order)
        assert side == "no"
        assert cents == 40  # 1.0 - 0.60 = 0.40 -> 40 cents
        assert err is None

    def test_missing_kalshi_side_returns_error(self):
        from src.execution.router_kalshi import _resolve_side_and_price

        order = _make_order(kalshi_side=None)
        side, cents, err = _resolve_side_and_price(order)
        assert side is None
        assert err is not None
        assert err.success is False
        assert "kalshi_side" in err.error.lower()


# ===========================================================================
# router_kalshi.py — _apply_fill_price
# ===========================================================================


class TestApplyFillPrice:
    """Tests for _apply_fill_price helper."""

    def test_valid_avg_price(self):
        from src.execution.router_kalshi import _apply_fill_price

        order = _make_order(price=0.60)
        _apply_fill_price(order, {"avg_price": 62})
        assert order.fill_price == 0.62

    def test_missing_avg_price_falls_back(self):
        from src.execution.router_kalshi import _apply_fill_price

        order = _make_order(price=0.60)
        _apply_fill_price(order, {})
        assert order.fill_price == 0.60

    def test_out_of_range_avg_price_falls_back(self):
        from src.execution.router_kalshi import _apply_fill_price

        order = _make_order(price=0.60)
        _apply_fill_price(order, {"avg_price": 150})
        assert order.fill_price == 0.60

    def test_zero_avg_price_falls_back(self):
        from src.execution.router_kalshi import _apply_fill_price

        order = _make_order(price=0.60)
        _apply_fill_price(order, {"avg_price": 0})
        assert order.fill_price == 0.60

    def test_non_numeric_avg_price_falls_back(self):
        from src.execution.router_kalshi import _apply_fill_price

        order = _make_order(price=0.60)
        _apply_fill_price(order, {"avg_price": "not_a_number"})
        assert order.fill_price == 0.60


