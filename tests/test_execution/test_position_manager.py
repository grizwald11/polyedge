"""Tests for position manager."""

from __future__ import annotations

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
    def test_unrealized_pnl_buy(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.34, size=10))

        pm.update_price("FED-RATE-CUT-MAY26", 0.40)
        pos = pm.get_position("FED-RATE-CUT-MAY26")

        assert pos.current_price == 0.40
        assert pos.unrealized_pnl == pytest.approx(0.60)  # (0.40 - 0.34) * 10

    def test_unrealized_pnl_loss(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.34, size=10))

        pm.update_price("FED-RATE-CUT-MAY26", 0.30)
        pos = pm.get_position("FED-RATE-CUT-MAY26")

        assert pos.unrealized_pnl == pytest.approx(-0.40)

    def test_update_nonexistent_market(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_price("DOESNT-EXIST", 0.50)  # Should not raise


class TestPortfolioMetrics:
    def test_total_exposure(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(market_id="MKT-A", token_id="MKT-A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade(market_id="MKT-B", token_id="MKT-B_yes", price=0.50, size=5))

        # cost_basis = avg_entry * size
        assert pm.get_total_exposure() == pytest.approx(5.50)  # 3.0 + 2.5

    def test_exposure_pct(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(price=0.50, size=100))

        assert pm.get_total_exposure_pct() == pytest.approx(0.10)  # 50/500

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

        assert pm.get_strategy_exposure(StrategyName.AI_PROBABILITY) == pytest.approx(3.0)
        assert pm.get_strategy_exposure(StrategyName.OBVIOUS_NO) == pytest.approx(9.5)

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

    def test_total_unrealized_pnl(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade(market_id="A", token_id="A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade(market_id="B", token_id="B_yes", price=0.50, size=10))

        pm.update_price("A", 0.40)  # +1.0
        pm.update_price("B", 0.45)  # -0.5

        assert pm.get_total_unrealized_pnl() == pytest.approx(0.50)
