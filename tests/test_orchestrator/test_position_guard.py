"""Tests for position guard — inter-cycle stop-loss monitoring."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Direction, Order, Platform, Position, Side, StrategyName, Trade
from src.orchestrator.position_guard import CHECK_INTERVAL, run_position_guard


def _make_position(
    market_id="TEST-MKT",
    entry_price=0.60,
    size=100.0,
    direction=Direction.BUY_YES,
) -> Position:
    return Position(
        market_id=market_id,
        token_id=f"{market_id}_yes",
        direction=direction,
        size=size,
        avg_entry_price=entry_price,
        current_price=entry_price,
        cost_basis=entry_price * size,
        unrealized_pnl=0.0,
        peak_pnl=0.0,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        last_updated=datetime.now(timezone.utc),
    )


class TestPositionGuard:
    @pytest.mark.asyncio
    async def test_exits_on_stop_loss(self):
        """Guard should execute exit when position hits stop-loss."""
        pos = _make_position(entry_price=0.60)
        pos.current_price = 0.35  # Big drop
        pos.unrealized_pnl = -25.0

        pm = MagicMock()
        pm.get_all_positions.return_value = [pos]
        pm.has_pending_exit.return_value = False
        pm.should_exit.return_value = (True, "stop_loss: 42% loss exceeds 18% threshold")

        kalshi = AsyncMock()
        kalshi.get_market.return_value = {"yes_bid_dollars": "0.35", "no_bid_dollars": "0.65"}

        trade = Trade(
            order_id="PE-exit",
            market_id="TEST-MKT",
            token_id="TEST-MKT_yes",
            side=Side.SELL,
            price=0.35,
            size=100.0,
            fee=0.0,
            realized_pnl=-25.0,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
        )
        route_result = MagicMock()
        route_result.success = True
        route_result.trade = trade
        order_router = AsyncMock()
        order_router.route_order.return_value = route_result

        order_builder = MagicMock()
        order_builder._generate_order_id.return_value = "PE-guard-1"

        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        shutdown = asyncio.Event()

        async def _run_one_cycle():
            """Run guard for one cycle then shut down."""
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.get_event_loop().create_task(_run_one_cycle())
        await run_position_guard(
            pm, kalshi, order_builder, order_router,
            circuit_breaker, risk_engine, settings, shutdown,
        )

        order_router.route_order.assert_called_once()
        pm.update_from_trade.assert_called_once_with(trade)
        risk_engine.record_exit.assert_called_once_with("TEST-MKT", pnl=-25.0)

    @pytest.mark.asyncio
    async def test_skips_non_stop_loss_exits(self):
        """Guard should NOT process take-profit or edge-gone exits."""
        pos = _make_position()
        pos.unrealized_pnl = 10.0

        pm = MagicMock()
        pm.get_all_positions.return_value = [pos]
        pm.has_pending_exit.return_value = False
        pm.should_exit.return_value = (True, "take_profit: captured 80% of max gain")

        kalshi = AsyncMock()
        kalshi.get_market.return_value = {"yes_bid_dollars": "0.80", "no_bid_dollars": "0.20"}

        order_router = AsyncMock()
        order_builder = MagicMock()
        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        shutdown = asyncio.Event()

        async def _run_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.get_event_loop().create_task(_run_one())
        await run_position_guard(
            pm, kalshi, order_builder, order_router,
            circuit_breaker, risk_engine, settings, shutdown,
        )

        order_router.route_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_pending_exits(self):
        """Guard should skip positions with pending exit orders."""
        pos = _make_position()
        pm = MagicMock()
        pm.get_all_positions.return_value = [pos]
        pm.has_pending_exit.return_value = True

        kalshi = AsyncMock()
        order_router = AsyncMock()
        order_builder = MagicMock()
        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        shutdown = asyncio.Event()

        async def _run_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.get_event_loop().create_task(_run_one())
        await run_position_guard(
            pm, kalshi, order_builder, order_router,
            circuit_breaker, risk_engine, settings, shutdown,
        )

        kalshi.get_market.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_positions_skips_cycle(self):
        """Guard should sleep and retry when no positions exist."""
        pm = MagicMock()
        pm.get_all_positions.return_value = []

        kalshi = AsyncMock()
        order_router = AsyncMock()
        order_builder = MagicMock()
        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        shutdown = asyncio.Event()

        async def _run_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.get_event_loop().create_task(_run_one())
        await run_position_guard(
            pm, kalshi, order_builder, order_router,
            circuit_breaker, risk_engine, settings, shutdown,
        )

        kalshi.get_market.assert_not_called()
        order_router.route_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_event_stops_loop(self):
        """Guard should exit promptly when shutdown_event is set."""
        pm = MagicMock()
        pm.get_all_positions.return_value = []
        kalshi = AsyncMock()
        order_router = AsyncMock()
        order_builder = MagicMock()
        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        shutdown = asyncio.Event()
        shutdown.set()  # Already set — should exit immediately

        await asyncio.wait_for(
            run_position_guard(
                pm, kalshi, order_builder, order_router,
                circuit_breaker, risk_engine, settings, shutdown,
            ),
            timeout=2.0,
        )
        # If we get here without timeout, shutdown was respected

    @pytest.mark.asyncio
    async def test_none_shutdown_event_does_not_crash(self):
        """Guard should not crash if shutdown_event is None (graceful fallback)."""
        pm = MagicMock()
        pm.get_all_positions.return_value = []
        kalshi = AsyncMock()
        order_router = AsyncMock()
        order_builder = MagicMock()
        circuit_breaker = MagicMock()
        risk_engine = MagicMock()
        settings = MagicMock()
        settings.trading.prefer_maker = True

        # Run with None shutdown — must be externally cancelled
        task = asyncio.get_event_loop().create_task(
            run_position_guard(
                pm, kalshi, order_builder, order_router,
                circuit_breaker, risk_engine, settings, None,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

    def test_check_interval_is_reasonable(self):
        assert 15 <= CHECK_INTERVAL <= 60
