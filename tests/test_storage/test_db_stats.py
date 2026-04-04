"""Tests for StatsMixin (db_stats.py): strategy P&L, timeseries, stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import (
    Market,
    MarketCategory,
    MarketToken,
    Side,
    StrategyName,
    Trade,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(market_id="TEST-MKT", order_id="ORD-1", **kwargs) -> Trade:
    defaults = dict(
        order_id=order_id,
        market_id=market_id,
        token_id=f"{market_id}_yes",
        side=Side.BUY,
        price=0.60,
        size=10,
        fee=0.0,
        realized_pnl=0.0,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
    )
    defaults.update(kwargs)
    return Trade(**defaults)


def _make_market(ticker="TEST-MKT", **kwargs) -> Market:
    defaults = dict(
        ticker=ticker,
        question="Will X happen?",
        category=MarketCategory.POLITICS,
        tags=["test"],
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=0.60),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=0.40),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        volume_total=1_000_000.0,
        liquidity=20000.0,
        spread=0.02,
        active=True,
        closed=False,
    )
    defaults.update(kwargs)
    return Market(**defaults)


# ---------------------------------------------------------------------------
# get_strategy_pnl
# ---------------------------------------------------------------------------


class TestGetStrategyPnl:
    def test_empty_database(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = tmp_db.get_strategy_pnl(today)
        assert result == {}

    def test_single_strategy(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t1 = _make_trade(order_id="O1", realized_pnl=5.0, timestamp=datetime.now(timezone.utc))
        t2 = _make_trade(order_id="O2", realized_pnl=-2.0, timestamp=datetime.now(timezone.utc))
        tmp_db.log_trade(t1)
        tmp_db.log_trade(t2)
        result = tmp_db.get_strategy_pnl(today)
        assert StrategyName.AI_PROBABILITY.value in result
        entry = result[StrategyName.AI_PROBABILITY.value]
        assert entry["count"] == 2
        assert entry["pnl"] == pytest.approx(3.0, abs=0.01)

    def test_multiple_strategies(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="O1", strategy=StrategyName.AI_PROBABILITY, realized_pnl=10.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="O2", strategy=StrategyName.OBVIOUS_NO, realized_pnl=2.0, timestamp=now))
        result = tmp_db.get_strategy_pnl(today)
        assert len(result) == 2
        assert result[StrategyName.AI_PROBABILITY.value]["pnl"] == pytest.approx(10.0, abs=0.01)
        assert result[StrategyName.OBVIOUS_NO.value]["pnl"] == pytest.approx(2.0, abs=0.01)

    def test_filters_by_date(self, tmp_db):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        today = datetime.now(timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="OLD", realized_pnl=100.0, timestamp=yesterday))
        tmp_db.log_trade(_make_trade(order_id="NEW", realized_pnl=5.0, timestamp=today))
        result = tmp_db.get_strategy_pnl(today.strftime("%Y-%m-%d"))
        entry = result[StrategyName.AI_PROBABILITY.value]
        assert entry["count"] == 1
        assert entry["pnl"] == pytest.approx(5.0, abs=0.01)


# ---------------------------------------------------------------------------
# get_pnl_timeseries
# ---------------------------------------------------------------------------


class TestGetPnlTimeseries:
    def test_empty_database(self, tmp_db):
        result = tmp_db.get_pnl_timeseries(days=30)
        assert result == []

    def test_cumulative_sum(self, tmp_db):
        now = datetime.now(timezone.utc)
        day1 = now - timedelta(days=2)
        day2 = now - timedelta(days=1)
        day3 = now
        tmp_db.log_trade(_make_trade(order_id="D1", realized_pnl=10.0, timestamp=day1))
        tmp_db.log_trade(_make_trade(order_id="D2", realized_pnl=-3.0, timestamp=day2))
        tmp_db.log_trade(_make_trade(order_id="D3", realized_pnl=7.0, timestamp=day3))
        result = tmp_db.get_pnl_timeseries(days=30)
        assert len(result) == 3
        # Cumulative: 10, 7, 14
        assert result[0]["pnl"] == pytest.approx(10.0, abs=0.01)
        assert result[0]["cumulative_pnl"] == pytest.approx(10.0, abs=0.01)
        assert result[1]["cumulative_pnl"] == pytest.approx(7.0, abs=0.01)
        assert result[2]["cumulative_pnl"] == pytest.approx(14.0, abs=0.01)

    def test_trade_count_per_day(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="A1", realized_pnl=1.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="A2", realized_pnl=2.0, timestamp=now))
        result = tmp_db.get_pnl_timeseries(days=30)
        assert len(result) == 1
        assert result[0]["trade_count"] == 2

    def test_respects_days_cutoff(self, tmp_db):
        old = datetime.now(timezone.utc) - timedelta(days=60)
        recent = datetime.now(timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="OLD", realized_pnl=100.0, timestamp=old))
        tmp_db.log_trade(_make_trade(order_id="NEW", realized_pnl=5.0, timestamp=recent))
        result = tmp_db.get_pnl_timeseries(days=30)
        assert len(result) == 1
        assert result[0]["pnl"] == pytest.approx(5.0, abs=0.01)


# ---------------------------------------------------------------------------
# get_strategy_stats
# ---------------------------------------------------------------------------


class TestGetStrategyStats:
    def test_empty_database(self, tmp_db):
        result = tmp_db.get_strategy_stats()
        assert result == []

    def test_win_rate_calculation(self, tmp_db):
        now = datetime.now(timezone.utc)
        # 3 winning, 1 losing, 1 break-even
        tmp_db.log_trade(_make_trade(order_id="W1", realized_pnl=5.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="W2", realized_pnl=3.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="W3", realized_pnl=1.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="L1", realized_pnl=-4.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="BE", realized_pnl=0.0, timestamp=now))
        result = tmp_db.get_strategy_stats()
        assert len(result) == 1
        stats = result[0]
        assert stats["strategy"] == StrategyName.AI_PROBABILITY.value
        assert stats["trade_count"] == 5
        assert stats["winning"] == 3
        assert stats["losing"] == 1
        assert stats["win_rate"] == pytest.approx(0.6, abs=0.01)
        assert stats["total_pnl"] == pytest.approx(5.0, abs=0.01)

    def test_multiple_strategies(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="AI1", strategy=StrategyName.AI_PROBABILITY, realized_pnl=10.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="NO1", strategy=StrategyName.OBVIOUS_NO, realized_pnl=1.0, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="NO2", strategy=StrategyName.OBVIOUS_NO, realized_pnl=-0.5, timestamp=now))
        result = tmp_db.get_strategy_stats()
        assert len(result) == 2
        # Ordered by total_pnl DESC
        assert result[0]["strategy"] == StrategyName.AI_PROBABILITY.value
        assert result[1]["strategy"] == StrategyName.OBVIOUS_NO.value
        assert result[1]["win_rate"] == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    def test_empty_database(self, tmp_db):
        result = tmp_db.get_stats()
        assert result["active_markets"] == 0
        assert result["total_signals"] == 0
        assert result["total_trades"] == 0
        assert result["total_pnl"] == 0.0

    def test_with_data(self, tmp_db):
        now = datetime.now(timezone.utc)
        # Insert an active market
        tmp_db.upsert_market(_make_market("MKT-1", active=True))
        # Insert a trade
        tmp_db.log_trade(_make_trade(order_id="T1", realized_pnl=7.5, timestamp=now))
        tmp_db.log_trade(_make_trade(order_id="T2", realized_pnl=-2.5, timestamp=now))
        result = tmp_db.get_stats()
        assert result["active_markets"] == 1
        assert result["total_trades"] == 2
        assert result["total_pnl"] == pytest.approx(5.0, abs=0.01)
