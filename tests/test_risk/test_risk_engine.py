"""Tests for risk engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.models import (
    Direction, Market, MarketCategory, MarketToken, Signal, StrategyName,
)
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.risk_engine import RiskEngine


@pytest.fixture
def position_manager(tmp_db) -> PositionManager:
    return PositionManager(tmp_db, bankroll=500.0)


@pytest.fixture
def circuit_breaker(settings, tmp_db) -> CircuitBreaker:
    return CircuitBreaker(settings, tmp_db)


@pytest.fixture
def engine(settings, position_manager, circuit_breaker, tmp_db) -> RiskEngine:
    return RiskEngine(settings, position_manager, circuit_breaker, tmp_db)


@pytest.fixture
def signal() -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="FED-RATE-CUT-MAY26",
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.42,
        market_price=0.34,
        confidence=0.7,
    )


@pytest.fixture
def market() -> Market:
    return Market(
        ticker="FED-RATE-CUT-MAY26",
        question="Will the Fed cut rates?",
        tokens=[
            MarketToken(token_id="FED-RATE-CUT-MAY26_yes", outcome="Yes", price=0.34),
            MarketToken(token_id="FED-RATE-CUT-MAY26_no", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
        liquidity=50000,
        active=True,
    )


class TestCheckAll:
    def test_passes_valid_trade(self, engine, signal, market):
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is True
        assert result.approved_size == 10
        assert len(result.failed_checks) == 0

    def test_fails_insufficient_balance(self, engine, signal, market):
        result = engine.check_all(signal, market, proposed_size=2000, proposed_cost=600.0)
        assert result.passed is False
        assert any("Insufficient balance" in c for c in result.failed_checks)

    def test_fails_position_too_large(self, engine, signal, market):
        # Max position = 500 * 0.05 = $25
        result = engine.check_all(signal, market, proposed_size=100, proposed_cost=30.0)
        assert result.passed is False
        assert any("Position too large" in c for c in result.failed_checks)

    def test_fails_total_exposure(self, engine, signal, market, position_manager, tmp_db):
        from src.core.models import Side, Trade
        # Fill up exposure to near limit
        for i in range(8):
            trade = Trade(
                order_id=f"PE-{i}", market_id=f"MKT-{i}", token_id=f"MKT-{i}_yes",
                side=Side.BUY, price=0.50, size=50, strategy=StrategyName.AI_PROBABILITY, paper=True,
            )
            position_manager.update_from_trade(trade)

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Total exposure" in c for c in result.failed_checks)

    def test_fails_circuit_breaker(self, engine, signal, market, circuit_breaker):
        circuit_breaker._halted = True
        circuit_breaker._halt_reason = "Daily loss limit"

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Circuit breaker" in c for c in result.failed_checks)

    def test_fails_existing_position(self, engine, signal, market, position_manager):
        from src.core.models import Side, Trade
        trade = Trade(
            order_id="PE-x", market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes", side=Side.BUY,
            price=0.34, size=5, strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        position_manager.update_from_trade(trade)

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Already have position" in c for c in result.failed_checks)

    def test_fails_edge_too_small(self, engine, market):
        weak_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=0.02,  # Below 5% minimum
            probability_estimate=0.36,
            market_price=0.34,
        )
        result = engine.check_all(weak_signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Edge too small" in c for c in result.failed_checks)

    def test_fails_market_resolving_soon(self, engine, signal):
        expiring_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving soon",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=12),
            liquidity=50000,
        )
        result = engine.check_all(signal, expiring_market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("<1 day" in c for c in result.failed_checks)

    def test_fails_cooldown(self, engine, signal, market):
        engine.record_exit("FED-RATE-CUT-MAY26")

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Cooldown" in c for c in result.failed_checks)

    def test_fails_liquidity(self, engine, signal):
        thin_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Thin market",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=45),
            liquidity=20.0,
        )
        result = engine.check_all(signal, thin_market, proposed_size=100, proposed_cost=34.0)
        assert result.passed is False
        assert any("liquidity" in c.lower() for c in result.failed_checks)

    def test_obvious_no_exposure_limit(self, engine, market, position_manager):
        from src.core.models import Side, Trade
        # Fill up obvious NO exposure
        trade = Trade(
            order_id="PE-no1", market_id="OTHER-MKT",
            token_id="OTHER-MKT_no", side=Side.BUY,
            price=0.97, size=50, strategy=StrategyName.OBVIOUS_NO, paper=True,
        )
        position_manager.update_from_trade(trade)

        no_signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.97,
            market_price=0.66,
        )
        result = engine.check_all(no_signal, market, proposed_size=10, proposed_cost=9.70)
        assert result.passed is False
        assert any("Obvious NO" in c for c in result.failed_checks)

    def test_warns_long_dated(self, engine, signal):
        long_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Long dated",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=400),
            liquidity=50000,
        )
        result = engine.check_all(signal, long_market, proposed_size=10, proposed_cost=3.40)
        assert any("Long-dated" in w for w in result.warnings)


class TestCooldownPersistence:
    def test_cooldown_survives_restart(self, settings, tmp_db):
        """Cooldowns should persist across RiskEngine restarts."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        cb = CircuitBreaker(settings, tmp_db)

        engine1 = RiskEngine(settings, pm, cb, tmp_db)
        engine1.record_exit("FED-RATE-CUT-MAY26")

        # Simulate restart: new instance on same DB
        engine2 = RiskEngine(settings, pm, cb, tmp_db)
        assert "FED-RATE-CUT-MAY26" in engine2._cooldowns

        # Verify it actually blocks
        sig = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
        )
        mkt = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Test",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=45),
            liquidity=50000,
        )
        result = engine2.check_all(sig, mkt, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Cooldown" in c for c in result.failed_checks)
