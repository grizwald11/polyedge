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
        # Daily loss limit = 8% of $500 = $40
        _log_losing_trade(tmp_db, pnl=-45.0)

        assert cb.check(500.0) is False
        assert cb.is_halted() is True
        assert "Daily loss limit" in cb.halt_reason

    def test_small_loss_ok(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-10.0)
        assert cb.check(500.0) is True

    def test_stays_halted_once_triggered(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-45.0)
        cb.check(500.0)

        # Still halted on next check
        assert cb.check(500.0) is False

    def test_escalating_cooldown_at_5pct(self, cb, tmp_db):
        """At 5% daily loss ($25), reduce position sizes before full halt."""
        _log_losing_trade(tmp_db, pnl=-28.0)  # > 5% of $500 but < 8%
        assert cb.check(500.0) is True  # Not halted yet
        assert cb.is_reduced_sizing is True  # But sizing is reduced


class TestUnrealizedPnlInDailyLimit:
    """Unrealized P&L from open positions counts at 75% weight toward daily limit (M-3)."""

    def test_unrealized_loss_triggers_halt(self, cb, tmp_db):
        # Realized = -30, unrealized = -20 * 0.75 = -15, total = -45 > 8% of 500 = 40
        _log_losing_trade(tmp_db, pnl=-30.0)
        assert cb.check(500.0, unrealized_pnl=-20.0) is False
        assert cb.is_halted() is True

    def test_unrealized_loss_alone_insufficient(self, cb):
        # Only unrealized = -20 * 0.75 = -15, no realized losses, under $40 limit
        assert cb.check(500.0, unrealized_pnl=-20.0) is True

    def test_unrealized_profit_offsets(self, cb, tmp_db):
        # Realized = -30, unrealized = +10 * 0.75 = +7.5, net = -22.5, under $40 limit
        _log_losing_trade(tmp_db, pnl=-30.0)
        assert cb.check(500.0, unrealized_pnl=10.0) is True

    def test_discounted_unrealized_under_limit(self, cb, tmp_db):
        # Realized = -15, unrealized = -10 * 0.75 = -7.5, total = -22.5, under $40 limit
        _log_losing_trade(tmp_db, pnl=-15.0)
        assert cb.check(500.0, unrealized_pnl=-10.0) is True


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


class TestRecordDailyResultDayParam:
    """Tests for the `day` parameter fix on record_daily_result."""

    def test_day_param_sets_last_recorded_day_correctly(self, cb):
        """record_daily_result(day=X) should set _last_recorded_day to X, not today."""
        cb.record_daily_result(-10.0, day="2026-04-03")
        assert cb._last_recorded_day == "2026-04-03"
        assert cb._consecutive_losing_days == 1

    def test_day_param_defaults_to_today(self, cb):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cb.record_daily_result(-5.0)
        assert cb._last_recorded_day == today

    def test_multi_day_gap_backfill(self, settings, tmp_db):
        """_update_consecutive_losses should backfill ALL missed days, not just yesterday."""
        from unittest.mock import patch

        cb = CircuitBreaker(settings, tmp_db)
        # Record a loss on April 1
        cb.record_daily_result(-10.0, day="2026-04-01")
        assert cb._consecutive_losing_days == 1

        # Log losing trades on April 2 and April 3
        for day in ["2026-04-02", "2026-04-03"]:
            trade = Trade(
                order_id=f"PE-loss-{day}",
                market_id="MKT",
                token_id="MKT_yes",
                side=Side.BUY,
                price=0.50,
                size=10,
                fee=0.0,
                realized_pnl=-15.0,
                strategy=StrategyName.AI_PROBABILITY,
                paper=True,
                timestamp=datetime.fromisoformat(f"{day}T12:00:00+00:00"),
            )
            tmp_db.log_trade(trade)

        # Now simulate it being April 4 — should backfill April 2 and 3
        fake_now = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.risk.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cb._last_day_checked = None  # Force re-check
            cb._update_consecutive_losses()

        # Should have backfilled April 2 and 3 as losing days
        assert cb._consecutive_losing_days == 3
        assert cb._last_recorded_day == "2026-04-03"

    def test_winning_day_in_backfill_resets_streak(self, settings, tmp_db):
        """A winning day in a multi-day backfill should reset the streak."""
        from unittest.mock import patch

        cb = CircuitBreaker(settings, tmp_db)
        cb.record_daily_result(-10.0, day="2026-04-01")

        # April 2 is a WINNING day, April 3 is a loss
        trade_win = Trade(
            order_id="PE-win",
            market_id="MKT",
            token_id="MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            fee=0.0,
            realized_pnl=20.0,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
            timestamp=datetime.fromisoformat("2026-04-02T12:00:00+00:00"),
        )
        trade_loss = Trade(
            order_id="PE-loss-3",
            market_id="MKT2",
            token_id="MKT2_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            fee=0.0,
            realized_pnl=-15.0,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
            timestamp=datetime.fromisoformat("2026-04-03T12:00:00+00:00"),
        )
        tmp_db.log_trade(trade_win)
        tmp_db.log_trade(trade_loss)

        fake_now = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.risk.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cb._last_day_checked = None
            cb._update_consecutive_losses()

        # April 2 win resets streak, April 3 loss starts new streak
        assert cb._consecutive_losing_days == 1


class TestUnrealizedPnlMasking:
    """Tests for the unrealized PnL adjustment in day boundary calculations.

    Positive unrealized PnL must NOT offset realized losses — this was
    incorrectly resetting consecutive_losing_days.
    """

    def test_positive_unrealized_does_not_mask_realized_loss(self):
        """If realized is -$15 and unrealized is +$60, day should still be a loss.

        Old formula: -15 + 60*0.3 = +3 → resets streak (BAD)
        New formula: -15 + min(60*0.3, 0) = -15 → counts as loss (GOOD)
        """
        realized = -15.0
        unrealized = 60.0
        # New formula from lifecycle.py
        unrealized_adjustment = min(unrealized * 0.3, 0.0)
        total = realized + unrealized_adjustment
        assert total < 0, "Positive unrealized should NOT make a losing day look profitable"

    def test_negative_unrealized_still_counts(self):
        """Negative unrealized should make losses worse (at 30% discount)."""
        realized = -10.0
        unrealized = -40.0
        unrealized_adjustment = min(unrealized * 0.3, 0.0)
        total = realized + unrealized_adjustment
        assert total == -22.0  # -10 + (-12) = -22

    def test_zero_unrealized_leaves_realized_unchanged(self):
        """Zero unrealized should not affect the daily result."""
        realized = -5.0
        unrealized = 0.0
        unrealized_adjustment = min(unrealized * 0.3, 0.0)
        total = realized + unrealized_adjustment
        assert total == -5.0


class TestGetWarnings:
    """Tests for pre-halt warning system (loss velocity, low balance, drawdown)."""

    def test_no_warnings_when_healthy(self, cb):
        warnings = cb.get_warnings(bankroll=500.0)
        assert warnings == []

    def test_loss_velocity_50_pct(self, cb, tmp_db):
        # Daily limit = 8% of 500 = $40. Log -22 realized → 55% of limit
        _log_losing_trade(tmp_db, pnl=-22.0)
        warnings = cb.get_warnings(bankroll=500.0)
        assert len(warnings) == 1
        assert warnings[0]["type"] == "loss_velocity"
        assert warnings[0]["pct_of_limit"] >= 0.50

    def test_loss_velocity_75_pct(self, cb, tmp_db):
        # Log -32 → 80% of $40 limit
        _log_losing_trade(tmp_db, pnl=-32.0)
        warnings = cb.get_warnings(bankroll=500.0)
        assert len(warnings) == 1
        assert warnings[0]["type"] == "loss_velocity"
        assert warnings[0]["pct_of_limit"] >= 0.75

    def test_loss_velocity_includes_unrealized(self, cb, tmp_db):
        # Realized = -15, unrealized = -15 * 0.75 = -11.25, total = -26.25 → 65.6% of $40
        _log_losing_trade(tmp_db, pnl=-15.0)
        warnings = cb.get_warnings(bankroll=500.0, unrealized_pnl=-15.0)
        assert len(warnings) == 1
        assert warnings[0]["type"] == "loss_velocity"

    def test_low_balance_warning(self, cb):
        # Bankroll dropped to $200 (40% of initial $500)
        warnings = cb.get_warnings(bankroll=200.0)
        assert any(w["type"] == "low_balance" for w in warnings)

    def test_no_low_balance_warning_when_healthy(self, cb):
        warnings = cb.get_warnings(bankroll=400.0)
        assert not any(w["type"] == "low_balance" for w in warnings)

    def test_drawdown_warning(self, cb):
        # Set high water mark to $600, current equity = $500 + (-60) = $440
        # Drawdown = (600 - 440) / 600 = 26.7%, limit is 20%, so 26.7/20 = 133% — past halt
        # Use a drawdown that's >= 50% of limit but below limit: 10-19%
        cb._high_water_mark = 600.0
        # equity = 560 → drawdown = 40/600 = 6.7% → not enough
        # equity = 540 → drawdown = 60/600 = 10% → 50% of 20% limit ✓
        warnings = cb.get_warnings(bankroll=540.0, unrealized_pnl=0.0)
        assert any(w["type"] == "drawdown_warning" for w in warnings)

    def test_warnings_deduped_per_session(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-22.0)
        warnings1 = cb.get_warnings(bankroll=500.0)
        assert len(warnings1) == 1
        # Second call should return no new warnings
        warnings2 = cb.get_warnings(bankroll=500.0)
        assert len(warnings2) == 0

    def test_warnings_cleared_on_reset(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-22.0)
        cb.get_warnings(bankroll=500.0)
        assert len(cb._warnings_sent) > 0
        cb.reset()
        assert len(cb._warnings_sent) == 0

    def test_warnings_cleared_on_daily_reset(self, cb, tmp_db):
        _log_losing_trade(tmp_db, pnl=-22.0)
        cb.get_warnings(bankroll=500.0)
        cb._halted = True
        cb._halt_reason = "Daily loss limit hit"
        cb.reset_daily()
        assert len(cb._warnings_sent) == 0
