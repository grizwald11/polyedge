"""Integration test: signal → risk → order → position → cooldown pipeline.

Tests the full lifecycle across multiple trading cycles:
  Cycle 1: signal passes risk, order fills, position opened
  Cycle 2: exit with loss triggers cooldown
  Cycle 3: re-entry blocked by cooldown
  Cycle 4: cooldown expires, re-entry allowed

This catches bugs where in-memory and DB cooldown state diverge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    Signal,
    StrategyName,
    Trade,
)
from src.execution.order_router import OrderRouter, OrderResult
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.kelly_sizer import KellySizer
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database


@pytest.fixture
def integ_db(tmp_path) -> Database:
    db_path = str(tmp_path / "integ_test.db")
    db = Database(db_path=db_path, wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


@pytest.fixture
def integ_settings() -> Settings:
    return Settings(
        trading=Settings.model_fields["trading"].default_factory()
    )


@pytest.fixture
def integ_market() -> Market:
    return Market(
        ticker="INTEG-MKT-1",
        question="Integration test market?",
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id="INTEG-MKT-1_yes", outcome="Yes", price=0.40),
            MarketToken(token_id="INTEG-MKT-1_no", outcome="No", price=0.60),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        volume_total=500000.0,
        liquidity=20000.0,
        spread=0.02,
        active=True,
    )


@pytest.fixture
def integ_signal() -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="INTEG-MKT-1",
        market_question="Integration test market?",
        direction=Direction.BUY_YES,
        edge=0.10,
        probability_estimate=0.50,
        market_price=0.40,
        confidence=0.75,
        reasoning="Integration test signal.",
    )


class TestCooldownPipelineIntegration:
    """Signal → risk check → fill → exit → cooldown → re-entry blocked → cooldown expires."""

    def test_cooldown_blocks_reentry_after_loss(
        self, integ_db, integ_settings, integ_market, integ_signal,
    ):
        """Full cooldown lifecycle: exit with loss → blocked → cooldown expires → allowed."""
        # Set up components
        cb = CircuitBreaker(integ_settings, integ_db)
        sizer = KellySizer(integ_settings)
        pm = PositionManager(integ_db, bankroll=integ_settings.trading.bankroll)
        risk = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # --- Cycle 1: Signal passes risk checks ---
        contracts = sizer.calculate_position_size(
            edge=integ_signal.edge,
            probability=integ_signal.probability_estimate,
            bankroll=integ_settings.trading.bankroll,
            current_exposure=0.0,
            order_price=integ_market.yes_price,
            confidence=integ_signal.confidence,
        )
        assert contracts >= 1

        proposed_cost = contracts * integ_market.yes_price
        result = risk.check_all(
            signal=integ_signal,
            market=integ_market,
            proposed_size=contracts,
            proposed_cost=proposed_cost,
        )
        assert result.passed, f"Cycle 1 should pass: {result.failed_checks}"

        # Simulate position opened via trade
        trade = Trade(
            order_id="order-1",
            market_id="INTEG-MKT-1",
            token_id="INTEG-MKT-1_yes",
            side=Side.BUY,
            price=0.40,
            size=float(contracts),
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(trade)
        assert pm.get_position("INTEG-MKT-1") is not None

        # --- Cycle 2: Exit with loss ---
        exit_trade = Trade(
            order_id="order-2",
            market_id="INTEG-MKT-1",
            token_id="INTEG-MKT-1_yes",
            side=Side.SELL,
            price=0.30,  # Lost money
            size=float(contracts),
            realized_pnl=-float(contracts) * 0.10,  # $0.10 loss per contract
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(exit_trade)

        # Record exit with loss → triggers 4h cooldown
        risk.record_exit("INTEG-MKT-1", pnl=-float(contracts) * 0.10)

        # Verify cooldown persisted to DB
        conn = integ_db._get_conn()
        row = conn.execute(
            "SELECT * FROM cooldowns WHERE market_id = 'INTEG-MKT-1'"
        ).fetchone()
        assert row is not None, "Cooldown should be persisted to DB"

        # --- Cycle 3: Re-entry BLOCKED by cooldown ---
        result = risk.check_all(
            signal=integ_signal,
            market=integ_market,
            proposed_size=contracts,
            proposed_cost=proposed_cost,
        )
        assert not result.passed, "Should be blocked by cooldown"
        assert any("cooldown" in c.lower() for c in result.failed_checks), \
            f"Should fail on cooldown check, got: {result.failed_checks}"

    def test_cooldown_expires_allows_reentry(
        self, integ_db, integ_settings, integ_market, integ_signal,
    ):
        """After cooldown expires, re-entry should be allowed."""
        cb = CircuitBreaker(integ_settings, integ_db)
        pm = PositionManager(integ_db, bankroll=integ_settings.trading.bankroll)
        risk = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # Record cooldown in the past (expired)
        past = datetime.now(timezone.utc) - timedelta(hours=5)
        risk._cooldowns["INTEG-MKT-1"] = past
        risk._cooldown_durations["INTEG-MKT-1"] = 14400  # 4h

        # Also persist to DB
        integ_db.save_cooldown("INTEG-MKT-1", past, 14400)

        contracts = 5
        proposed_cost = contracts * integ_market.yes_price
        result = risk.check_all(
            signal=integ_signal,
            market=integ_market,
            proposed_size=contracts,
            proposed_cost=proposed_cost,
        )
        # Cooldown check should pass (4h expired, 5h ago)
        cooldown_failures = [c for c in result.failed_checks if "cooldown" in c.lower()]
        assert len(cooldown_failures) == 0, \
            f"Cooldown should have expired, but got: {cooldown_failures}"

    def test_profit_exit_shorter_cooldown(
        self, integ_db, integ_settings, integ_market, integ_signal,
    ):
        """Profitable exits should have a shorter cooldown (1h vs 4h for losses)."""
        cb = CircuitBreaker(integ_settings, integ_db)
        pm = PositionManager(integ_db, bankroll=integ_settings.trading.bankroll)
        risk = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # Exit with profit
        risk.record_exit("INTEG-MKT-1", pnl=5.0)
        assert risk._cooldown_durations["INTEG-MKT-1"] == risk.cooldown_profit_seconds

        # Exit with loss on another market
        risk.record_exit("INTEG-MKT-2", pnl=-5.0)
        assert risk._cooldown_durations["INTEG-MKT-2"] == risk.cooldown_loss_seconds

        # Loss cooldown should be longer
        assert risk.cooldown_loss_seconds > risk.cooldown_profit_seconds

    def test_cooldown_survives_engine_restart(
        self, integ_db, integ_settings,
    ):
        """Cooldowns persisted to DB should survive engine recreation."""
        cb = CircuitBreaker(integ_settings, integ_db)
        pm = PositionManager(integ_db, bankroll=integ_settings.trading.bankroll)
        risk1 = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # Record cooldown
        risk1.record_exit("PERSIST-MKT", pnl=-10.0)

        # Create new engine (simulates restart)
        risk2 = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # Cooldown should be loaded from DB
        assert "PERSIST-MKT" in risk2._cooldowns, \
            "Cooldown should be loaded from DB on restart"


class TestMultiSignalRiskIntegration:
    """Tests risk engine with multiple concurrent signals."""

    def test_second_signal_blocked_by_exposure(
        self, integ_db, integ_settings, integ_market,
    ):
        """After opening a position, a second large signal should be exposure-capped."""
        cb = CircuitBreaker(integ_settings, integ_db)
        pm = PositionManager(integ_db, bankroll=integ_settings.trading.bankroll)
        risk = RiskEngine(integ_settings, pm, circuit_breaker=cb, db=integ_db)

        # Open a position taking 35% of bankroll
        bankroll = integ_settings.trading.bankroll
        big_trade = Trade(
            order_id="big-1",
            market_id="BIG-MKT",
            token_id="BIG-MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=bankroll * 0.35 / 0.50,  # 35% exposure
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(big_trade)

        # Now try a signal that would push over 40% total exposure
        signal2 = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="INTEG-MKT-1",
            market_question="Test?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.75,
        )
        # Propose 10% bankroll cost
        proposed_cost = bankroll * 0.10
        result = risk.check_all(
            signal=signal2,
            market=integ_market,
            proposed_size=int(proposed_cost / 0.40),
            proposed_cost=proposed_cost,
        )
        # Should fail: 35% existing + 10% new = 45% > 40% cap
        assert not result.passed
        assert any("exposure" in c.lower() for c in result.failed_checks)
