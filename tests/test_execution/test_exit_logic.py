"""Tests for position exit logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import (
    Direction, Market, MarketCategory, MarketToken, Position, StrategyName,
)
from src.execution.position_manager import PositionManager


def _make_position(
    market_id="FED-RATE",
    direction=Direction.BUY_YES,
    size=10,
    entry_price=0.34,
    current_price=0.34,
    days_ago=5,
) -> Position:
    return Position(
        market_id=market_id,
        token_id=f"{market_id}_yes",
        direction=direction,
        size=size,
        avg_entry_price=entry_price,
        current_price=current_price,
        unrealized_pnl=(current_price - entry_price) * size
        if direction in (Direction.BUY_YES, Direction.BUY_NO)
        else (entry_price - current_price) * size,
        strategy=StrategyName.AI_PROBABILITY,
        opened_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _make_market(ticker="FED-RATE", yes_price=0.34, no_price=0.66):
    return Market(
        ticker=ticker,
        question="Test market?",
        category=MarketCategory.FED_MACRO,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000,
    )


class TestStopLoss:
    def test_no_exit_when_profitable(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.50)
        should, reason = pm.should_exit(pos)
        assert should is False

    def test_no_exit_small_loss(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.30)
        should, reason = pm.should_exit(pos)
        assert should is False

    def test_exit_on_large_loss(self, tmp_db):
        pm = PositionManager(tmp_db)
        # 50%+ loss: entry 0.34, current 0.15 → loss = 0.19 * 10 = 1.90, cost = 3.40
        pos = _make_position(entry_price=0.34, current_price=0.15)
        should, reason = pm.should_exit(pos)
        assert should is True
        assert "stop_loss" in reason

    def test_custom_stop_loss_threshold(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.25)
        # Default 50% threshold — not triggered
        should, _ = pm.should_exit(pos)
        assert should is False
        # Tighter 20% threshold — triggered (loss = 26%)
        should, reason = pm.should_exit(pos, stop_loss_pct=0.20)
        assert should is True
        assert "stop_loss" in reason


class TestTimeBasedExit:
    def test_no_exit_recent_position(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(days_ago=5)
        should, _ = pm.should_exit(pos)
        assert should is False

    def test_exit_old_position(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(days_ago=35)
        should, reason = pm.should_exit(pos)
        assert should is True
        assert "time_exit" in reason

    def test_custom_max_hold(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(days_ago=8)
        should, reason = pm.should_exit(pos, max_hold_days=7)
        assert should is True
        assert "time_exit" in reason

    def test_expiry_exit_underwater(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.30, days_ago=2)
        market = Market(
            ticker="FED-RATE",
            question="Test?",
            tokens=[
                MarketToken(token_id="FED-RATE_yes", outcome="Yes", price=0.30),
                MarketToken(token_id="FED-RATE_no", outcome="No", price=0.70),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=12),
            volume_24h=50000,
        )
        should, reason = pm.should_exit(pos, market)
        assert should is True
        assert "expiry_exit" in reason

    def test_no_expiry_exit_when_profitable(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.50, days_ago=2)
        market = Market(
            ticker="FED-RATE",
            question="Test?",
            tokens=[
                MarketToken(token_id="FED-RATE_yes", outcome="Yes", price=0.50),
                MarketToken(token_id="FED-RATE_no", outcome="No", price=0.50),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=12),
            volume_24h=50000,
        )
        should, _ = pm.should_exit(pos, market)
        assert should is False


class TestEdgeGone:
    def test_exit_when_edge_evaporated(self, tmp_db):
        pm = PositionManager(tmp_db)
        # Bought YES at 0.34, now price is 0.995 — virtually no upside left
        # Take-profit fires first (99% of max gain captured), edge_gone also applies
        pos = _make_position(entry_price=0.34, current_price=0.995)
        market = _make_market(yes_price=0.995, no_price=0.005)
        should, reason = pm.should_exit(pos, market)
        assert should is True
        assert "edge_gone" in reason or "take_profit" in reason

    def test_no_exit_when_edge_remains(self, tmp_db):
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.40)
        market = _make_market(yes_price=0.40, no_price=0.60)
        should, _ = pm.should_exit(pos, market)
        assert should is False

    def test_no_exit_without_market(self, tmp_db):
        """Edge-gone check is skipped if no market data provided."""
        pm = PositionManager(tmp_db)
        pos = _make_position(entry_price=0.34, current_price=0.34)
        should, _ = pm.should_exit(pos, market=None)
        assert should is False


class TestGetExitCandidates:
    def test_returns_exit_candidates(self, tmp_db):
        pm = PositionManager(tmp_db)
        # Manually inject positions
        pm._positions["OLD-MARKET"] = _make_position(
            market_id="OLD-MARKET", days_ago=35,
        )
        pm._positions["OK-MARKET"] = _make_position(
            market_id="OK-MARKET", days_ago=3,
        )

        candidates = pm.get_exit_candidates()
        assert len(candidates) == 1
        assert candidates[0][0].market_id == "OLD-MARKET"
        assert "time_exit" in candidates[0][1]

    def test_with_market_data(self, tmp_db):
        pm = PositionManager(tmp_db)
        pm._positions["EDGE-GONE"] = _make_position(
            market_id="EDGE-GONE", entry_price=0.34, current_price=0.995,
        )

        markets = {
            "EDGE-GONE": _make_market(ticker="EDGE-GONE", yes_price=0.995, no_price=0.005),
        }
        candidates = pm.get_exit_candidates(markets)
        assert len(candidates) == 1
        assert "edge_gone" in candidates[0][1] or "take_profit" in candidates[0][1]
