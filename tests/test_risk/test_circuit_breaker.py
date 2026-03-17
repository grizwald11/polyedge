"""Tests for circuit breaker."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.core.models import Side, StrategyName, Trade
from src.risk.circuit_breaker import CircuitBreaker
from src.storage.database import Database


@pytest.fixture
def cb(settings, tmp_db) -> CircuitBreaker:
    return CircuitBreaker(settings, tmp_db)


def _log_losing_trade(db: Database, pnl: float = -10.0):
    """Helper to log a losing trade for today."""
    trade = Trade(
        order_id="PE-loss",
        market_id="MKT",
        token_id="MKT_yes",
        side=Side.BUY,
        price=0.50,
        size=10,
        fee=0.0,
        realized_pnl=pnl,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
    )
    db.log_trade(trade)


class TestDailyLossLimit:
    def test_normal_trading_allowed(self, cb):
        assert cb.check(500.0) is True
        assert cb.is_halted() is False

    def test_daily_loss_triggers_halt(self, cb, tmp_db):
        # Daily loss limit = 10% of $500 = $50
        _log_losing_trade(tmp_db, pnl=-55.0)

        assert cb.check(500.0) is False
        assert cb.is_halted() is True
        assert "Daily loss limit" in cb.halt_reason

    def test_small_loss_ok(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-20.0)
        assert cb.check(500.0) is True

    def test_stays_halted_once_triggered(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-55.0)
        cb.check(500.0)

        # Still halted on next check
        assert cb.check(500.0) is False


class TestConsecutiveLosses:
    def test_three_losing_days_reduces_sizing(self, cb):
        cb.record_daily_result(-10.0)
        cb.record_daily_result(-5.0)
        cb.record_daily_result(-20.0)

        cb.check(500.0)  # Trigger check
        assert cb.is_reduced_sizing is True
        assert cb.get_kelly_multiplier() == 0.5

    def test_five_losing_days_halts(self, cb):
        for _ in range(5):
            cb.record_daily_result(-10.0)

        assert cb.check(500.0) is False
        assert cb.is_halted() is True
        assert "5 consecutive" in cb.halt_reason

    def test_winning_day_resets_streak(self, cb):
        cb.record_daily_result(-10.0)
        cb.record_daily_result(-5.0)
        cb.record_daily_result(15.0)  # Winner!

        cb.check(500.0)
        assert cb._consecutive_losing_days == 0
        assert cb.is_reduced_sizing is False

    def test_normal_kelly_multiplier(self, cb):
        assert cb.get_kelly_multiplier() == 1.0


class TestReset:
    def test_manual_reset(self, cb):
        cb._halted = True
        cb._halt_reason = "test"
        cb._consecutive_losing_days = 5
        cb._reduced_sizing = True

        cb.reset()

        assert cb.is_halted() is False
        assert cb.halt_reason is None
        assert cb._consecutive_losing_days == 0
        assert cb.is_reduced_sizing is False

    def test_daily_reset(self, cb):
        cb._halted = True
        cb._halt_reason = "Daily loss limit hit"

        cb.reset_daily()

        assert cb.is_halted() is False

    def test_daily_reset_doesnt_clear_other_halts(self, cb):
        cb._halted = True
        cb._halt_reason = "5 consecutive losing days"

        cb.reset_daily()

        assert cb.is_halted() is True  # Not a daily halt, shouldn't clear
