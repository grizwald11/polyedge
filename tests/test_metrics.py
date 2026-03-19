"""Tests for the Metrics tracker."""

from __future__ import annotations

import time

from src.metrics import Metrics


class TestMetrics:
    """Unit tests for Metrics class."""

    def test_initial_state(self):
        m = Metrics()
        assert m.cycle_count == 0
        assert m.trades_today == 0
        assert m.errors_today == 0
        assert m.last_cycle_time is None

    def test_record_cycle(self):
        m = Metrics()
        m.record_cycle(duration_ms=150.0, trades=2, signals=5, positions=3)
        assert m.cycle_count == 1
        assert m.trades_today == 2
        assert m.last_cycle_duration_ms == 150.0
        assert m.last_cycle_signals == 5
        assert m.open_positions == 3
        assert m.last_cycle_time is not None

    def test_record_multiple_cycles(self):
        m = Metrics()
        m.record_cycle(duration_ms=100, trades=1, signals=3, positions=1)
        m.record_cycle(duration_ms=200, trades=2, signals=4, positions=2)
        assert m.cycle_count == 2
        assert m.trades_today == 3
        assert m.last_cycle_duration_ms == 200

    def test_record_error(self):
        m = Metrics()
        m.record_error("scanner", "Connection timeout")
        assert m.errors_today == 1
        m.record_error("forecaster", "Rate limited")
        assert m.errors_today == 2

    def test_record_trade(self):
        m = Metrics()
        # Should not raise
        m.record_trade("FED-RATE-CUT", "BUY_YES", 10, 0.34)

    def test_health_status_initial(self):
        m = Metrics()
        status = m.get_health_status()
        assert status["status"] == "healthy"
        assert status["cycle_count"] == 0
        assert status["trades_today"] == 0
        assert status["errors_today"] == 0
        assert status["seconds_since_last_cycle"] is None
        assert "timestamp" in status

    def test_health_status_after_cycles(self):
        m = Metrics()
        m.record_cycle(duration_ms=100, trades=1, signals=2, positions=1)
        status = m.get_health_status()
        assert status["status"] == "healthy"
        assert status["cycle_count"] == 1
        assert status["trades_today"] == 1
        assert status["open_positions"] == 1
        assert status["seconds_since_last_cycle"] is not None

    def test_health_degraded_after_long_gap(self):
        m = Metrics()
        m.last_cycle_time = time.time() - 700  # 11+ minutes ago
        assert m._is_healthy() is False
        status = m.get_health_status()
        assert status["status"] == "degraded"

    def test_daily_reset_clears_counters(self):
        m = Metrics()
        m.record_cycle(100, trades=5, signals=10, positions=2)
        m.record_error("test", "err")
        assert m.trades_today == 5
        assert m.errors_today == 1

        # Simulate crossing midnight
        m._daily_reset_date = "2025-01-01"
        m.record_cycle(100, trades=1, signals=2, positions=0)
        assert m.trades_today == 1  # Reset then added 1
        assert m.errors_today == 0  # Reset

    def test_error_message_truncated_in_log(self):
        """Long error messages should not crash."""
        m = Metrics()
        m.record_error("test", "x" * 500)
        assert m.errors_today == 1

    def test_uptime_positive(self):
        m = Metrics()
        status = m.get_health_status()
        assert status["uptime_seconds"] >= 0
