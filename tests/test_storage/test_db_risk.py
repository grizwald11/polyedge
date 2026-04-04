"""Tests for RiskMixin (db_risk.py): circuit breaker state, cooldowns, settings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Circuit Breaker State
# ---------------------------------------------------------------------------


class TestCircuitBreakerState:
    """save_circuit_breaker_state / load_circuit_breaker_state."""

    def test_save_and_load_roundtrip(self, tmp_db):
        tmp_db.save_circuit_breaker_state(
            consecutive_losing_days=3,
            reduced_sizing=True,
            halted=False,
            halt_reason=None,
            halt_time=None,
            last_recorded_day="2026-04-02",
            high_water_mark=520.0,
        )
        state = tmp_db.load_circuit_breaker_state()
        assert state is not None
        assert state["consecutive_losing_days"] == 3
        assert state["reduced_sizing"] == 1  # stored as int
        assert state["halted"] == 0
        assert state["halt_reason"] is None
        assert state["last_recorded_day"] == "2026-04-02"
        assert state["high_water_mark"] == 520.0
        assert state["last_updated"] is not None

    def test_load_returns_none_when_empty(self, tmp_db):
        assert tmp_db.load_circuit_breaker_state() is None

    def test_upsert_overwrites_existing(self, tmp_db):
        tmp_db.save_circuit_breaker_state(
            consecutive_losing_days=1,
            reduced_sizing=False,
            halted=False,
        )
        tmp_db.save_circuit_breaker_state(
            consecutive_losing_days=5,
            reduced_sizing=True,
            halted=True,
            halt_reason="5 consecutive losing days",
            halt_time="2026-04-03T12:00:00+00:00",
        )
        state = tmp_db.load_circuit_breaker_state()
        assert state["consecutive_losing_days"] == 5
        assert state["halted"] == 1
        assert state["halt_reason"] == "5 consecutive losing days"

    def test_singleton_row_count(self, tmp_db):
        """Only one row should ever exist (id=1)."""
        for i in range(5):
            tmp_db.save_circuit_breaker_state(
                consecutive_losing_days=i,
                reduced_sizing=False,
                halted=False,
            )
        conn = tmp_db._get_conn()
        count = conn.execute("SELECT COUNT(*) as cnt FROM circuit_breaker_state").fetchone()["cnt"]
        assert count == 1


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------


class TestCooldowns:
    """save_cooldown / load_cooldowns / load_cooldown_durations / delete_cooldown."""

    def test_save_and_load_active_cooldown(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-A", exit_time=now, duration_seconds=3600)
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "MKT-A" in active
        assert active["MKT-A"].tzinfo is not None

    def test_expired_cooldown_is_cleaned_up(self, tmp_db):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        tmp_db.save_cooldown("MKT-OLD", exit_time=old_time, duration_seconds=3600)
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "MKT-OLD" not in active
        # Verify row was deleted
        conn = tmp_db._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM cooldowns WHERE market_id='MKT-OLD'").fetchone()
        assert row["cnt"] == 0

    def test_max_age_fallback_when_no_duration(self, tmp_db):
        """When duration_seconds is NULL, max_age_seconds parameter is used."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        tmp_db.save_cooldown("MKT-NODUR", exit_time=recent, duration_seconds=None)
        # With a generous max_age, should be active
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "MKT-NODUR" in active
        # With a tiny max_age, should be expired
        active2 = tmp_db.load_cooldowns(max_age_seconds=1)
        assert "MKT-NODUR" not in active2

    def test_load_cooldown_durations(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-1", exit_time=now, duration_seconds=1800)
        tmp_db.save_cooldown("MKT-2", exit_time=now, duration_seconds=7200)
        tmp_db.save_cooldown("MKT-3", exit_time=now, duration_seconds=None)
        durations = tmp_db.load_cooldown_durations()
        assert durations == {"MKT-1": 1800, "MKT-2": 7200}
        assert "MKT-3" not in durations

    def test_delete_cooldown(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-DEL", exit_time=now, duration_seconds=3600)
        tmp_db.delete_cooldown("MKT-DEL")
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "MKT-DEL" not in active

    def test_save_cooldown_replaces_existing(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-X", exit_time=now - timedelta(hours=1), duration_seconds=600)
        tmp_db.save_cooldown("MKT-X", exit_time=now, duration_seconds=7200)
        durations = tmp_db.load_cooldown_durations()
        assert durations["MKT-X"] == 7200

    def test_multiple_cooldowns_mixed_active_expired(self, tmp_db):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("ACTIVE-1", exit_time=now, duration_seconds=3600)
        tmp_db.save_cooldown("ACTIVE-2", exit_time=now - timedelta(minutes=5), duration_seconds=3600)
        tmp_db.save_cooldown("EXPIRED-1", exit_time=now - timedelta(hours=3), duration_seconds=3600)
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "ACTIVE-1" in active
        assert "ACTIVE-2" in active
        assert "EXPIRED-1" not in active


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    """save_setting / load_setting."""

    def test_save_and_load_roundtrip(self, tmp_db):
        tmp_db.save_setting("bankroll", "500.00")
        assert tmp_db.load_setting("bankroll") == "500.00"

    def test_load_missing_key_returns_none(self, tmp_db):
        # First create the settings table by saving something
        tmp_db.save_setting("dummy", "x")
        assert tmp_db.load_setting("nonexistent") is None

    def test_load_before_table_exists_returns_none(self, tmp_db):
        """load_setting gracefully returns None if settings table doesn't exist yet."""
        assert tmp_db.load_setting("anything") is None

    def test_overwrite_existing_key(self, tmp_db):
        tmp_db.save_setting("mode", "paper")
        tmp_db.save_setting("mode", "live")
        assert tmp_db.load_setting("mode") == "live"

    def test_multiple_keys(self, tmp_db):
        tmp_db.save_setting("key_a", "val_a")
        tmp_db.save_setting("key_b", "val_b")
        assert tmp_db.load_setting("key_a") == "val_a"
        assert tmp_db.load_setting("key_b") == "val_b"
