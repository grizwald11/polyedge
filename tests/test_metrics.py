"""Tests for the Metrics tracker."""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock, patch

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

    def test_edge_return_log_bounded(self):
        """Edge-return log should be trimmed to max entries (M-3)."""
        m = Metrics()
        m._max_edge_return_entries = 5
        for i in range(20):
            m.record_closed_position(
                market_id=f"MKT-{i}",
                predicted_edge=0.05,
                realized_return=0.03,
                days_held=1.0,
            )
        assert len(m._edge_return_log) == 5
        # Should keep the most recent entries
        assert m._edge_return_log[-1]["market"] == "MKT-19"

    def test_record_closed_position(self):
        m = Metrics()
        m.record_closed_position("TEST", 0.08, 0.05, 3.5)
        assert len(m._edge_return_log) == 1
        entry = m._edge_return_log[0]
        assert entry["market"] == "TEST"
        assert entry["predicted_edge"] == 0.08
        assert entry["realized_return"] == 0.05
        assert entry["days_held"] == 3.5

    # ── Signal tracking (M-22) ────────────────────────────────────────────────

    def test_record_signal_generated_increments_counter(self):
        m = Metrics()
        m.record_signal_generated(edge=0.07)
        assert m.signals_generated == 1
        assert m._all_signal_edges == [0.07]

    def test_record_signal_generated_multiple(self):
        m = Metrics()
        m.record_signal_generated(edge=0.05)
        m.record_signal_generated(edge=0.10)
        assert m.signals_generated == 2
        assert m._all_signal_edges == [0.05, 0.10]

    def test_record_signal_generated_trims_to_max(self):
        m = Metrics()
        m._max_edge_return_entries = 3
        for i in range(7):
            m.record_signal_generated(edge=float(i) * 0.01)
        assert len(m._all_signal_edges) == 3
        # Should retain most recent 3 entries
        assert m._all_signal_edges == [0.04, 0.05, 0.06]

    def test_record_signal_generated_default_edge(self):
        m = Metrics()
        m.record_signal_generated()
        assert m._all_signal_edges == [0.0]

    def test_record_signal_risk_gated_increments_counter(self):
        m = Metrics()
        m.record_signal_risk_gated(edge=0.03)
        assert m.signals_risk_gated == 1
        assert m._gated_signal_edges == [0.03]

    def test_record_signal_risk_gated_multiple(self):
        m = Metrics()
        m.record_signal_risk_gated(edge=0.04)
        m.record_signal_risk_gated(edge=0.06)
        assert m.signals_risk_gated == 2
        assert m._gated_signal_edges == [0.04, 0.06]

    def test_record_signal_risk_gated_trims_to_max(self):
        m = Metrics()
        m._max_edge_return_entries = 3
        for i in range(8):
            m.record_signal_risk_gated(edge=float(i) * 0.01)
        assert len(m._gated_signal_edges) == 3
        assert m._gated_signal_edges == [0.05, 0.06, 0.07]

    def test_record_signal_risk_gated_default_edge(self):
        m = Metrics()
        m.record_signal_risk_gated()
        assert m._gated_signal_edges == [0.0]

    def test_record_signal_executed_increments_counter(self):
        m = Metrics()
        m.record_signal_executed()
        assert m.signals_executed == 1
        m.record_signal_executed()
        assert m.signals_executed == 2

    def test_signal_counters_in_health_status(self):
        m = Metrics()
        m.record_signal_generated(edge=0.08)
        m.record_signal_generated(edge=0.10)
        m.record_signal_risk_gated(edge=0.05)
        m.record_signal_executed()
        status = m.get_health_status()
        assert status["signals_generated"] == 2
        assert status["signals_risk_gated"] == 1
        assert status["signals_executed"] == 1
        assert status["avg_signal_edge"] == round((0.08 + 0.10) / 2, 4)
        assert status["avg_gated_edge"] == 0.05

    def test_health_status_avg_edges_none_when_no_signals(self):
        m = Metrics()
        status = m.get_health_status()
        assert status["avg_signal_edge"] is None
        assert status["avg_gated_edge"] is None

    # ── API latency tracking (L-5) ────────────────────────────────────────────

    def test_record_api_latency_creates_entry(self):
        m = Metrics()
        m.record_api_latency("gamma_api", 120.5)
        assert "gamma_api" in m._api_latencies
        assert list(m._api_latencies["gamma_api"]) == [120.5]

    def test_record_api_latency_multiple_endpoints(self):
        m = Metrics()
        m.record_api_latency("gamma_api", 100.0)
        m.record_api_latency("clob_api", 200.0)
        assert "gamma_api" in m._api_latencies
        assert "clob_api" in m._api_latencies

    def test_record_api_latency_appends_to_existing(self):
        m = Metrics()
        m.record_api_latency("gamma_api", 100.0)
        m.record_api_latency("gamma_api", 150.0)
        assert list(m._api_latencies["gamma_api"]) == [100.0, 150.0]

    def test_record_api_latency_deque_bounded_by_maxlen(self):
        m = Metrics()
        # deque is created with maxlen=_max_latency_entries (100)
        for i in range(150):
            m.record_api_latency("gamma_api", float(i))
        assert len(m._api_latencies["gamma_api"]) == 100

    def test_get_api_latency_stats_returns_empty_for_unknown_endpoint(self):
        m = Metrics()
        assert m.get_api_latency_stats("nonexistent") == {}

    def test_get_api_latency_stats_single_value(self):
        m = Metrics()
        m.record_api_latency("gamma_api", 100.0)
        stats = m.get_api_latency_stats("gamma_api")
        assert stats["count"] == 1
        assert stats["p50"] == 100.0
        assert stats["p99"] == 100.0

    def test_get_api_latency_stats_multiple_values(self):
        m = Metrics()
        # 10 evenly spaced values: 10, 20, ..., 100
        for v in range(10, 110, 10):
            m.record_api_latency("clob_api", float(v))
        stats = m.get_api_latency_stats("clob_api")
        assert stats["count"] == 10
        # Sorted: [10,20,30,40,50,60,70,80,90,100]; p50 index = 10//2 = 5 → 60
        assert stats["p50"] == 60.0
        # p99 index = min(9, int(10*0.99)) = min(9,9) = 9 → 100
        assert stats["p99"] == 100.0

    def test_get_all_latency_stats_empty(self):
        m = Metrics()
        assert m.get_all_latency_stats() == {}

    def test_get_all_latency_stats_multiple_endpoints(self):
        m = Metrics()
        m.record_api_latency("gamma_api", 100.0)
        m.record_api_latency("clob_api", 200.0)
        all_stats = m.get_all_latency_stats()
        assert "gamma_api" in all_stats
        assert "clob_api" in all_stats
        assert all_stats["gamma_api"]["count"] == 1
        assert all_stats["clob_api"]["count"] == 1

    # ── persist_to_db ─────────────────────────────────────────────────────────

    def test_persist_to_db_creates_table_and_inserts(self, tmp_path):
        """persist_to_db creates the metrics_snapshots table and writes a row."""
        db_path = str(tmp_path / "metrics_test.db")
        conn = sqlite3.connect(db_path)
        conn.commit()

        # Build a minimal mock database whose _get_conn returns a real connection
        mock_db = MagicMock()
        mock_db._get_conn.return_value = conn

        m = Metrics()
        m.record_cycle(duration_ms=50.0, trades=1, signals=2, positions=1)
        m.persist_to_db(mock_db)

        rows = conn.execute("SELECT * FROM metrics_snapshots").fetchall()
        assert len(rows) == 1
        conn.close()

    def test_persist_to_db_trims_old_snapshots(self, tmp_path):
        """persist_to_db removes rows beyond the 1000-snapshot limit."""
        db_path = str(tmp_path / "metrics_trim.db")
        conn = sqlite3.connect(db_path)
        # Pre-populate with 1001 rows so the trim actually fires
        conn.execute(
            "CREATE TABLE metrics_snapshots "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, data TEXT NOT NULL)"
        )
        for i in range(1001):
            conn.execute(
                "INSERT INTO metrics_snapshots (timestamp, data) VALUES (?, ?)",
                (f"2026-01-01T00:00:{i:02d}Z", "{}"),
            )
        conn.commit()

        mock_db = MagicMock()
        mock_db._get_conn.return_value = conn

        m = Metrics()
        m.persist_to_db(mock_db)

        # After trimming, should be ≤ 1000 rows
        count = conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0]
        assert count <= 1000
        conn.close()

    def test_persist_to_db_swallows_exception(self):
        """persist_to_db catches exceptions and does not raise."""
        mock_db = MagicMock()
        mock_db._get_conn.side_effect = RuntimeError("DB unavailable")

        m = Metrics()
        # Should not raise even if the DB call fails
        m.persist_to_db(mock_db)
