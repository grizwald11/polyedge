"""Tests for portfolio risk module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Side,
    StrategyName,
    Trade,
)
from src.execution.position_manager import PositionManager
from src.risk.portfolio_risk import PortfolioRisk


def _make_trade(market_id, token_id, price=0.34, size=10, strategy=StrategyName.AI_PROBABILITY):
    return Trade(
        order_id=f"PE-{market_id}",
        market_id=market_id,
        token_id=token_id,
        side=Side.BUY,
        price=price,
        size=size,
        fee=0.02,
        strategy=strategy,
        paper=True,
    )


def _make_market(ticker, event_ticker="", category="Politics"):
    return Market(
        ticker=ticker,
        question=f"Test market {ticker}",
        category=MarketCategory(category) if category in [e.value for e in MarketCategory] else MarketCategory.OTHER,
        event_ticker=event_ticker,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=0.34),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
    )


class TestCorrelatedExposure:
    def test_same_event_correlated(self, tmp_db):
        # Create markets in same event
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-1")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade("MKT-A", "MKT-A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade("MKT-B", "MKT-B_yes", price=0.40, size=10))

        pr = PortfolioRisk(pm, tmp_db)

        # Both are in EVENT-1, so correlated exposure for MKT-A includes MKT-B
        # cost_basis includes fees: (3.0+0.02) + (4.0+0.02) = 7.04
        exposure = pr.get_correlated_exposure("MKT-A")
        assert exposure == pytest.approx(7.04)  # 3.02 + 4.02

    def test_different_events_not_correlated(self, tmp_db):
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-2")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade("MKT-A", "MKT-A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade("MKT-B", "MKT-B_yes", price=0.40, size=10))

        pr = PortfolioRisk(pm, tmp_db)

        exposure = pr.get_correlated_exposure("MKT-A")
        assert exposure == pytest.approx(3.02)  # Only MKT-A (3.0 + 0.02 fee)

    def test_no_event_ticker_zero(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        pr = PortfolioRisk(pm, tmp_db)

        exposure = pr.get_correlated_exposure("NONEXISTENT")
        assert exposure == 0.0


class TestDiversification:
    def test_warns_high_event_concentration(self, tmp_db):
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        tmp_db.upsert_market(m1)

        pm = PositionManager(tmp_db, bankroll=100.0)
        pm.update_from_trade(_make_trade("MKT-A", "MKT-A_yes", price=0.50, size=40))

        pr = PortfolioRisk(pm, tmp_db)
        warnings = pr.check_diversification(bankroll=100.0)

        assert any("EVENT-1" in w for w in warnings)

    def test_no_warnings_diversified(self, tmp_db):
        for i in range(5):
            m = _make_market(f"MKT-{i}", event_ticker=f"EVENT-{i}")
            tmp_db.upsert_market(m)

        pm = PositionManager(tmp_db, bankroll=500.0)
        for i in range(5):
            pm.update_from_trade(_make_trade(f"MKT-{i}", f"MKT-{i}_yes", price=0.10, size=10))

        pr = PortfolioRisk(pm, tmp_db)
        warnings = pr.check_diversification(bankroll=500.0)

        assert len(warnings) == 0


class TestEventExposure:
    def test_event_exposure(self, tmp_db):
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-1")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade("MKT-A", "MKT-A_yes", price=0.30, size=10))
        pm.update_from_trade(_make_trade("MKT-B", "MKT-B_yes", price=0.40, size=10))

        pr = PortfolioRisk(pm, tmp_db)

        assert pr.get_event_exposure("EVENT-1") == pytest.approx(7.04)  # 3.02 + 4.02
        assert pr.get_event_exposure("EVENT-2") == pytest.approx(0.0)
