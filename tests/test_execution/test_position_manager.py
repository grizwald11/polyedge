"""Tests for position manager."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.models import Direction, Side, StrategyName, Trade
from src.execution.position_manager import PositionManager


def _make_trade(
    market_id="FED-RATE-CUT-MAY26",
    token_id="FED-RATE-CUT-MAY26_yes",
    side=Side.BUY,
    price=0.34,
    size=10.0,
    strategy=StrategyName.AI_PROBABILITY,
) -> Trade:
    return Trade(
        order_id="PE-test123",
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        fee=0.02,
        strategy=strategy,
        paper=True,
    )


class TestUpdateFromTrade:
    def test_new_position(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        trade = _make_trade()

        pos = pm.update_from_trade(trade, "Will the Fed cut rates?")

        assert pos.market_id == "FED-RATE-CUT-MAY26"
        assert pos.direction == Direction.BUY_YES
        assert pos.size == 10.0
        assert pos.avg_entry_price == 0.34
        assert pos.current_price == 0.34

    def test_add_to_position(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.30, size=10))
        pos = pm.update_from_trade(_make_trade(price=0.40, size=10))

        assert pos.size == 20.0
        assert pos.avg_entry_price == pytest.approx(0.35)  # Weighted avg

    def test_close_position(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))
        pm.update_from_trade(_make_trade(side=Side.SELL, size=10))

        assert pm.has_position("FED-RATE-CUT-MAY26") is False
        assert pm.get_position_count() == 0

    def test_partial_close(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))
        pm.update_from_trade(_make_trade(side=Side.SELL, size=5))

        pos = pm.get_position("FED-RATE-CUT-MAY26")
        assert pos is not None
        assert pos.size == 5.0

    def test_buy_no_direction(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        trade = _make_trade(token_id="FED-RATE-CUT-MAY26_no")
        pos = pm.update_from_trade(trade)

        assert pos.direction == Direction.BUY_NO


class TestPriceUpdate:
    def test_unrealized_pnl_buy_yes(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.34, size=10))

        pm.update_price("FED-RATE-CUT-MAY26", 0.40, 0.60)
        pos = pm.get_position("FED-RATE-CUT-MAY26")

        assert pos.current_price == 0.40
        assert pos.unrealized_pnl == pytest.approx(0.60)  # (0.40 - 0.34) * 10

    def test_unrealized_pnl_buy_no(self, tmp_db):
        """BUY_NO positions should use no_price, not yes_price."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(
            token_id="FED-RATE-CUT-MAY26_no", price=0.97, size=10
        ))

        # YES=0.04, NO=0.96 — NO dropped from 0.97 to 0.96
        pm.update_price("FED-RATE-CUT-MAY26", 0.04, 0.96)
        pos = pm.get_position("FED-RATE-CUT-MAY26")

        assert pos.current_price == 0.96  # Should use no_price
        assert pos.unrealized_pnl == pytest.approx(-0.10)  # (0.96 - 0.97) * 10

    def test_unrealized_pnl_loss(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.34, size=10))

        pm.update_price("FED-RATE-CUT-MAY26", 0.30, 0.70)
        pos = pm.get_position("FED-RATE-CUT-MAY26")

        assert pos.unrealized_pnl == pytest.approx(-0.40)

    def test_update_nonexistent_market(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_price("DOESNT-EXIST", 0.50, 0.50)  # Should not raise

    def test_zero_price_ignored(self, tmp_db):
        """Zero prices (no data) should not overwrite valid prices."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.34, size=10))

        # First update with real prices
        pm.update_price("FED-RATE-CUT-MAY26", 0.40, 0.60)
        pos = pm.get_position("FED-RATE-CUT-MAY26")
        assert pos.current_price == 0.40

        # Update with zero prices — should be ignored
        pm.update_price("FED-RATE-CUT-MAY26", 0.0, 0.0)
        pos = pm.get_position("FED-RATE-CUT-MAY26")
        assert pos.current_price == 0.40  # Unchanged


class TestPortfolioMetrics:
    def test_total_exposure(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(market_id="MKT-A", token_id="MKT-A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade(market_id="MKT-B", token_id="MKT-B_yes", price=0.50, size=5))

        # cost_basis = avg_entry * size + total_fees
        # Each trade has fee=0.02, so 3.0 + 0.02 + 2.5 + 0.02 = 5.54
        assert pm.get_total_exposure() == pytest.approx(5.54)  # 3.0+0.02 + 2.5+0.02

    def test_exposure_pct(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.50, size=100))

        # cost_basis = 100*0.50 + 0.02 fee = 50.02, pct = 50.02/500
        assert pm.get_total_exposure_pct() == pytest.approx(0.10004)  # (50+0.02)/500

    def test_strategy_exposure(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(
            market_id="MKT-A", token_id="MKT-A_yes",
            strategy=StrategyName.AI_PROBABILITY, price=0.30, size=10
        ))
        pm.update_from_trade(_make_trade(
            market_id="MKT-B", token_id="MKT-B_no",
            strategy=StrategyName.OBVIOUS_NO, price=0.95, size=10
        ))

        assert pm.get_strategy_exposure(StrategyName.AI_PROBABILITY) == pytest.approx(3.02)  # 3.0 + 0.02 fee
        assert pm.get_strategy_exposure(StrategyName.OBVIOUS_NO) == pytest.approx(9.52)  # 9.5 + 0.02 fee

    def test_has_position(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade())

        assert pm.has_position("FED-RATE-CUT-MAY26") is True
        assert pm.has_position("NONEXISTENT") is False

    def test_get_all_positions(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(market_id="A", token_id="A_yes"))
        pm.update_from_trade(_make_trade(market_id="B", token_id="B_yes"))

        positions = pm.get_all_positions()
        assert len(positions) == 2

    def test_load_positions_preserves_timestamps(self, tmp_db):
        """Positions loaded from DB should have historical timestamps, not restart time."""
        historical_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        trade = Trade(
            order_id="PE-ts-test",
            market_id="TS-MKT",
            token_id="TS-MKT_yes",
            side=Side.BUY,
            price=0.40,
            size=5.0,
            fee=0.01,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
            timestamp=historical_ts,
        )
        # Log trade directly to DB
        tmp_db.log_trade(trade)

        # Create new PositionManager — simulates restart
        pm = PositionManager(tmp_db, bankroll=500.0)
        pos = pm.get_position("TS-MKT")

        assert pos is not None
        assert pos.opened_at == historical_ts
        assert pos.last_updated == historical_ts

    def test_multiple_partial_closes(self, tmp_db):
        """BUY 10, SELL 3, SELL 4, SELL 3 should fully close position."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))
        pm.update_from_trade(_make_trade(side=Side.SELL, size=3))
        assert pm.get_position("FED-RATE-CUT-MAY26").size == 7.0

        pm.update_from_trade(_make_trade(side=Side.SELL, size=4))
        assert pm.get_position("FED-RATE-CUT-MAY26").size == 3.0

        pm.update_from_trade(_make_trade(side=Side.SELL, size=3))
        assert pm.has_position("FED-RATE-CUT-MAY26") is False

    def test_sell_more_than_held(self, tmp_db):
        """Selling more than held should close position, not go negative."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))
        pm.update_from_trade(_make_trade(side=Side.SELL, size=15))

        assert pm.has_position("FED-RATE-CUT-MAY26") is False
        assert pm.get_position_count() == 0

    def test_sell_clamping_does_not_mutate_trade(self, tmp_db):
        """Regression: clamping sell size should not mutate the Trade object."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))

        sell_trade = _make_trade(side=Side.SELL, size=15)
        pm.update_from_trade(sell_trade)

        # Trade object should not have been mutated
        assert sell_trade.size == 15

    def test_total_unrealized_pnl(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(market_id="A", token_id="A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade(market_id="B", token_id="B_yes", price=0.50, size=10))

        pm.update_price("A", 0.40, 0.60)  # +1.0
        pm.update_price("B", 0.45, 0.55)  # -0.5

        assert pm.get_total_unrealized_pnl() == pytest.approx(0.50)


class TestFeeTracking:
    """Tests for fee propagation in positions."""

    def test_new_position_includes_fee(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        trade = _make_trade(price=0.50, size=10)  # fee=0.02
        pos = pm.update_from_trade(trade)
        assert pos.total_fees == 0.02

    def test_buy_accumulates_fees(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.50, size=10))  # fee=0.02
        pos = pm.update_from_trade(_make_trade(price=0.60, size=5))  # fee=0.02
        assert pos.total_fees == pytest.approx(0.04)

    def test_partial_sell_accumulates_fees(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, price=0.50, size=10))  # fee=0.02
        # Sell half: fees are sunk costs — buy fee stays, sell fee added
        pos = pm.update_from_trade(_make_trade(side=Side.SELL, price=0.60, size=5))
        # original buy fee=0.02 + sell fee=0.02 = 0.04
        assert pos.total_fees == pytest.approx(0.04)

    def test_cost_basis_includes_fees(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        trade = _make_trade(price=0.40, size=10)  # fee=0.02
        pos = pm.update_from_trade(trade)
        # cost_basis = size * avg_entry + total_fees = 10*0.40 + 0.02 = 4.02
        assert pos.cost_basis == pytest.approx(4.02)


class TestEdgeCases:
    """Edge case tests for position manager."""

    def test_zero_size_trade(self, tmp_db):
        """A trade with size=0 should create a position with size 0."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        trade = _make_trade(size=0.0)
        pos = pm.update_from_trade(trade)
        assert pos.size == 0.0

    def test_double_close(self, tmp_db):
        """Closing an already-closed position should not crash."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(side=Side.BUY, size=10))
        pm.update_from_trade(_make_trade(side=Side.SELL, size=10))
        assert not pm.has_position("FED-RATE-CUT-MAY26")

        # Second close — position no longer exists
        pos = pm.update_from_trade(_make_trade(side=Side.SELL, size=5))
        # Should create a new position (the sell opens a short-like entry)
        assert pos is not None

    def test_update_price_unknown_market(self, tmp_db):
        """Updating price for a market we don't hold should be a no-op."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_price("NONEXISTENT", 0.50, 0.50)
        assert pm.get_position_count() == 0


class TestPositionSync:
    @pytest.mark.asyncio
    async def test_sync_adds_missing_positions(self, tmp_db):
        """Auto-correct should add positions found on Kalshi but missing locally."""
        from unittest.mock import AsyncMock

        pm = PositionManager(tmp_db, bankroll=500.0)
        assert pm.get_position_count() == 0

        mock_kalshi = AsyncMock()
        mock_kalshi.get_positions = AsyncMock(return_value=[
            {
                "ticker": "FED-RATE-CUT",
                "market_exposure": 100,
                "yes_count": 10,
                "no_count": 0,
                "average_price": 34,
                "title": "Will the Fed cut rates?",
            },
        ])

        mismatches = await pm.sync_with_kalshi(mock_kalshi, auto_correct=True)
        assert mismatches == 1
        assert pm.has_position("FED-RATE-CUT")
        pos = pm.get_position("FED-RATE-CUT")
        assert pos.size == 10.0
        assert pos.direction == Direction.BUY_YES

    @pytest.mark.asyncio
    async def test_sync_removes_stale_live_positions(self, tmp_db):
        """Auto-correct should remove local live positions not on Kalshi."""
        from unittest.mock import AsyncMock

        pm = PositionManager(tmp_db, bankroll=500.0)
        # Create a local live position
        pm.update_from_trade(_make_trade(
            market_id="STALE-MKT", token_id="STALE-MKT_yes",
            side=Side.BUY, price=0.50, size=5,
        ))
        # Mark it as live (not paper)
        pm._positions["STALE-MKT"].paper = False
        assert pm.has_position("STALE-MKT")

        mock_kalshi = AsyncMock()
        # Return at least one API position so api_positions is non-empty
        mock_kalshi.get_positions = AsyncMock(return_value=[
            {"ticker": "OTHER-MKT", "market_exposure": 50, "yes_count": 5, "no_count": 0},
        ])

        mismatches = await pm.sync_with_kalshi(mock_kalshi, auto_correct=True)
        assert mismatches >= 1
        assert not pm.has_position("STALE-MKT")

    @pytest.mark.asyncio
    async def test_sync_log_only_mode(self, tmp_db):
        """With auto_correct=False, mismatches are logged but not corrected."""
        from unittest.mock import AsyncMock

        pm = PositionManager(tmp_db, bankroll=500.0)

        mock_kalshi = AsyncMock()
        mock_kalshi.get_positions = AsyncMock(return_value=[
            {
                "ticker": "FED-RATE-CUT",
                "market_exposure": 100,
                "yes_count": 10,
                "no_count": 0,
            },
        ])

        mismatches = await pm.sync_with_kalshi(mock_kalshi, auto_correct=False)
        assert mismatches == 1
        # Position should NOT have been added
        assert not pm.has_position("FED-RATE-CUT")
