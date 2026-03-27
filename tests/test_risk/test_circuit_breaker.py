"""Tests for circuit breaker."""

from __future__ import annotations

from datetime import datetime, timezone

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


class TestUnrealizedPnlInDailyLimit:
    """Unrealized P&L from open positions counts at 30% weight toward daily limit."""

    def test_unrealized_loss_triggers_halt(self, cb, tmp_db):
        # Realized = -40, unrealized = -80 * 0.3 = -24, total = -64 > 10% of 500 = 50
        _log_losing_trade(tmp_db, pnl=-40.0)
        assert cb.check(500.0, unrealized_pnl=-80.0) is False
        assert cb.is_halted() is True

    def test_unrealized_loss_alone_insufficient(self, cb):
        # Only unrealized = -30 * 0.3 = -9, no realized losses, under limit
        assert cb.check(500.0, unrealized_pnl=-30.0) is True

    def test_unrealized_profit_offsets(self, cb, tmp_db):
        # Realized = -40, unrealized = +20 * 0.3 = +6, net = -34, under limit
        _log_losing_trade(tmp_db, pnl=-40.0)
        assert cb.check(500.0, unrealized_pnl=20.0) is True

    def test_discounted_unrealized_under_limit(self, cb, tmp_db):
        # Realized = -30, unrealized = -25 * 0.3 = -7.5, total = -37.5, under $50 limit
        _log_losing_trade(tmp_db, pnl=-30.0)
        assert cb.check(500.0, unrealized_pnl=-25.0) is True


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


class TestAutoReset:
    def test_daily_halt_auto_resets_next_day(self, cb):
        """Daily halt should auto-clear when check() is called on a new UTC day
        and at least 6 hours have passed since the halt."""
        cb._halted = True
        cb._halt_reason = "Daily loss limit hit"
        # Halt at 5pm yesterday — >6 hours ago and a new day
        cb._halt_time = datetime(2026, 3, 17, 17, 0, 0, tzinfo=timezone.utc)

        # check() on a new day should auto-reset and proceed to checks
        result = cb.check(bankroll=500.0)
        assert cb._halt_reason is None or "Daily loss limit" not in (cb._halt_reason or "")

    def test_daily_halt_not_reset_if_too_recent(self, cb):
        """Daily halt should NOT reset if <6 hours have passed, even on a new UTC day.
        This prevents edge cases where a halt at 23:55 UTC resets at 00:01 UTC."""
        from unittest.mock import patch

        cb._halted = True
        cb._halt_reason = "Daily loss limit hit"
        # Halt at 23:55 UTC on March 17
        cb._halt_time = datetime(2026, 3, 17, 23, 55, 0, tzinfo=timezone.utc)

        # It's now 00:05 UTC on March 18 — new day but only 10 minutes later
        fake_now = datetime(2026, 3, 18, 0, 5, 0, tzinfo=timezone.utc)
        with patch("src.risk.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = cb.check(bankroll=500.0)

        assert result is False
        assert cb.is_halted() is True

    def test_non_daily_halt_not_auto_reset(self, cb):
        """Non-daily halts (e.g., consecutive losses) should NOT auto-reset."""
        cb._halted = True
        cb._halt_reason = "5 consecutive losing days"
        cb._halt_time = datetime(2026, 3, 17, 23, 0, 0, tzinfo=timezone.utc)

        result = cb.check(bankroll=500.0)
        assert result is False
        assert cb.is_halted() is True


class TestPersistence:
    def test_consecutive_losses_survive_restart(self, settings, tmp_db):
        """Consecutive loss counter should persist across restarts."""
        cb1 = CircuitBreaker(settings, tmp_db)
        cb1.record_daily_result(-10.0)
        cb1.record_daily_result(-5.0)
        cb1.record_daily_result(-20.0)
        cb1.check(500.0)  # Triggers reduced sizing at 3 losses

        # Simulate restart: new instance on same DB
        cb2 = CircuitBreaker(settings, tmp_db)
        assert cb2._consecutive_losing_days == 3
        assert cb2.is_reduced_sizing is True

    def test_halt_survives_restart(self, settings, tmp_db):
        """Halt state should persist across restarts."""
        cb1 = CircuitBreaker(settings, tmp_db)
        for _ in range(5):
            cb1.record_daily_result(-10.0)
        cb1.check(500.0)  # Triggers halt
        assert cb1.is_halted() is True

        # Simulate restart
        cb2 = CircuitBreaker(settings, tmp_db)
        assert cb2.is_halted() is True
        assert "5 consecutive" in cb2.halt_reason

    def test_reset_clears_persisted_state(self, settings, tmp_db):
        """Reset should clear persisted state so new instance starts clean."""
        cb1 = CircuitBreaker(settings, tmp_db)
        for _ in range(5):
            cb1.record_daily_result(-10.0)
        cb1.check(500.0)
        cb1.reset()

        # Simulate restart
        cb2 = CircuitBreaker(settings, tmp_db)
        assert cb2.is_halted() is False
        assert cb2._consecutive_losing_days == 0
        assert cb2.is_reduced_sizing is False

    def test_reset_clears_db_state(self, settings, tmp_db):
        """After reset, the DB row should reflect cleared state."""
        cb = CircuitBreaker(settings, tmp_db)
        for _ in range(5):
            cb.record_daily_result(-10.0)
        cb.check(500.0)

        # Verify DB has halted state
        state = tmp_db.load_circuit_breaker_state()
        assert state is not None
        assert state["halted"] == 1
        assert state["consecutive_losing_days"] == 5

        cb.reset()

        # Verify DB reflects reset
        state = tmp_db.load_circuit_breaker_state()
        assert state is not None
        assert state["halted"] == 0
        assert state["consecutive_losing_days"] == 0
        assert state["reduced_sizing"] == 0
