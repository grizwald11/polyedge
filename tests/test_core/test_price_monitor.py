"""Tests for real-time price monitor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.core.models import Direction, Position, StrategyName
from src.core.price_monitor import (
    ADVERSE_MOVE_THRESHOLD,
    ALERT_THRESHOLD,
    PriceMonitor,
)


def _make_position(
    market_id="FED-RATE",
    direction=Direction.BUY_YES,
    entry_price=0.40,
    current_price=0.40,
    minutes_ago=5,
) -> Position:
    return Position(
        market_id=market_id,
        token_id=f"{market_id}_yes",
        direction=direction,
        size=10,
        avg_entry_price=entry_price,
        current_price=current_price,
        unrealized_pnl=0.0,
        strategy=StrategyName.AI_PROBABILITY,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


class TestAdverseDetection:
    @pytest.mark.asyncio
    async def test_no_alert_favorable_move(self):
        """Price moving in our favor should not trigger."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert = await monitor.check_price("FED-RATE", 0.50, 0.50)
        assert alert is None

    @pytest.mark.asyncio
    async def test_alert_at_10pct(self):
        """10% adverse move triggers alert but not exit."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert = await monitor.check_price("FED-RATE", 0.36, 0.64)
        assert alert is not None
        assert not alert.should_exit
        assert alert.adverse_pct >= ALERT_THRESHOLD

    @pytest.mark.asyncio
    async def test_auto_exit_at_15pct(self):
        """15%+ adverse move triggers auto-exit."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert = await monitor.check_price("FED-RATE", 0.33, 0.67)
        assert alert is not None
        assert alert.should_exit
        assert alert.adverse_pct >= ADVERSE_MOVE_THRESHOLD

    @pytest.mark.asyncio
    async def test_buy_no_direction(self):
        """BUY_NO position: adverse = NO price dropping."""
        monitor = PriceMonitor()
        pos = _make_position(direction=Direction.BUY_NO, entry_price=0.60)
        monitor.update_positions([pos])

        # NO price drops from 0.60 to 0.50 → 16.7% adverse
        alert = await monitor.check_price("FED-RATE", 0.50, 0.50)
        assert alert is not None
        assert alert.should_exit

    @pytest.mark.asyncio
    async def test_no_alert_on_unknown_market(self):
        """Markets not tracked should be ignored."""
        monitor = PriceMonitor()
        alert = await monitor.check_price("UNKNOWN", 0.10, 0.90)
        assert alert is None

    @pytest.mark.asyncio
    async def test_dedup_alerts(self):
        """Same market should only alert once."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert1 = await monitor.check_price("FED-RATE", 0.36, 0.64)
        assert alert1 is not None

        alert2 = await monitor.check_price("FED-RATE", 0.35, 0.65)
        assert alert2 is None  # Already alerted

    @pytest.mark.asyncio
    async def test_dedup_auto_exit(self):
        """Auto-exit should only trigger once per market."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert1 = await monitor.check_price("FED-RATE", 0.30, 0.70)
        assert alert1 is not None
        assert alert1.should_exit

        alert2 = await monitor.check_price("FED-RATE", 0.25, 0.75)
        assert alert2 is None  # Already exited

    @pytest.mark.asyncio
    async def test_fresh_position_ignored(self):
        """Positions less than 60s old should not trigger."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40, minutes_ago=0)
        pos.opened_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        monitor.update_positions([pos])

        alert = await monitor.check_price("FED-RATE", 0.20, 0.80)
        assert alert is None

    @pytest.mark.asyncio
    async def test_reset_allows_re_alert(self):
        """After reset, the same market can alert again."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        await monitor.check_price("FED-RATE", 0.36, 0.64)
        monitor.reset_alerts("FED-RATE")

        alert = await monitor.check_price("FED-RATE", 0.35, 0.65)
        assert alert is not None

    @pytest.mark.asyncio
    async def test_callback_fired(self):
        """Registered callback should be called on adverse move."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        callback = AsyncMock()
        monitor.on_adverse_move(callback)

        await monitor.check_price("FED-RATE", 0.33, 0.67)
        callback.assert_called_once()
        alert = callback.call_args[0][0]
        assert alert.should_exit

    @pytest.mark.asyncio
    async def test_closed_positions_cleaned_up(self):
        """When positions are closed, monitoring state is cleaned up."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])
        assert monitor.monitored_count == 1

        monitor.update_positions([])  # Position closed
        assert monitor.monitored_count == 0

    @pytest.mark.asyncio
    async def test_small_adverse_no_alert(self):
        """5% adverse move should not trigger anything."""
        monitor = PriceMonitor()
        pos = _make_position(entry_price=0.40)
        monitor.update_positions([pos])

        alert = await monitor.check_price("FED-RATE", 0.38, 0.62)
        assert alert is None
