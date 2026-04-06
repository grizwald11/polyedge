"""Tests for the parameter replay backtest."""

from __future__ import annotations

import pytest

from scripts.parameter_replay import (
    ParameterSet,
    ReplayResult,
    _simple_kelly_size,
    format_replay_report,
    load_data,
    replay_signals,
)


class TestSimpleKellySize:

    def test_basic_sizing(self):
        params = ParameterSet(kelly_fraction=0.25, max_position_pct=0.05, max_total_exposure_pct=0.40)
        size = _simple_kelly_size(0.08, 0.42, 500.0, 0.0, params)
        assert size > 0
        assert size <= 500 * 0.05  # Position cap

    def test_zero_edge_returns_zero(self):
        params = ParameterSet()
        assert _simple_kelly_size(0.0, 0.50, 500.0, 0.0, params) == 0.0

    def test_negative_edge_returns_zero(self):
        params = ParameterSet()
        assert _simple_kelly_size(-0.05, 0.50, 500.0, 0.0, params) == 0.0

    def test_exposure_cap(self):
        params = ParameterSet(max_total_exposure_pct=0.40)
        # Already at 40% exposure
        assert _simple_kelly_size(0.10, 0.60, 500.0, 200.0, params) == 0.0

    def test_position_cap_applied(self):
        params = ParameterSet(kelly_fraction=0.50, max_position_pct=0.05)
        size = _simple_kelly_size(0.20, 0.70, 500.0, 0.0, params)
        assert size <= 500 * 0.05


class TestReplaySignals:

    def _make_signal(self, market_id: str, edge: float = 0.08, prob: float = 0.42,
                     confidence: float = 0.85, strategy: str = "ai_probability",
                     direction: str = "BUY_YES") -> dict:
        return {
            "market_id": market_id,
            "strategy": strategy,
            "direction": direction,
            "edge": edge,
            "probability_estimate": prob,
            "market_price": prob - edge,
            "confidence": confidence,
            "timestamp": "2026-04-01T00:00:00Z",
        }

    def test_winning_trade(self):
        signals = [self._make_signal("MKT-1", edge=0.10, prob=0.50)]
        outcomes = {"MKT-1": 1}  # YES wins
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 1
        assert result.total_pnl > 0
        assert result.win_rate == 1.0

    def test_losing_trade(self):
        signals = [self._make_signal("MKT-1", edge=0.10, prob=0.50)]
        outcomes = {"MKT-1": 0}  # NO wins, BUY_YES loses
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 1
        assert result.total_pnl < 0
        assert result.win_rate == 0.0

    def test_edge_filter(self):
        signals = [self._make_signal("MKT-1", edge=0.03)]
        outcomes = {"MKT-1": 1}
        params = ParameterSet(min_edge_ai=0.05)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 0  # Filtered by min_edge

    def test_confidence_filter(self):
        signals = [self._make_signal("MKT-1", confidence=0.40)]
        outcomes = {"MKT-1": 1}
        params = ParameterSet(min_confidence=0.60)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 0  # Filtered by min_confidence

    def test_no_outcomes_no_trades(self):
        signals = [self._make_signal("MKT-1")]
        outcomes = {}  # No resolved outcomes

        result = replay_signals(signals, outcomes, ParameterSet())
        assert result.acted_signals == 0

    def test_same_market_multiple_signals(self):
        signals = [
            self._make_signal("MKT-1", edge=0.10, prob=0.50),
            self._make_signal("MKT-1", edge=0.15, prob=0.55),  # Same market
        ]
        outcomes = {"MKT-1": 1}
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        # In replay mode, positions resolve immediately so re-entry is possible.
        # Both signals are acted on since the first resolves before the second.
        assert result.acted_signals == 2

    def test_multiple_markets(self):
        signals = [
            self._make_signal("MKT-1", edge=0.10, prob=0.60),
            self._make_signal("MKT-2", edge=0.08, prob=0.50),
        ]
        outcomes = {"MKT-1": 1, "MKT-2": 0}
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 2
        assert result.total_signals == 2

    def test_buy_no_direction(self):
        signals = [self._make_signal("MKT-1", edge=0.10, prob=0.50, direction="BUY_NO")]
        outcomes = {"MKT-1": 0}  # NO wins
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 1
        assert result.total_pnl > 0  # BUY_NO + outcome=0 = win

    def test_news_strategy_uses_news_edge(self):
        signals = [self._make_signal("MKT-1", edge=0.035, strategy="news_reactive")]
        outcomes = {"MKT-1": 1}
        params = ParameterSet(min_edge_ai=0.05, min_edge_news=0.03, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.acted_signals == 1  # 0.035 > news threshold 0.03

    def test_drawdown_tracked(self):
        signals = [
            self._make_signal("MKT-1", edge=0.10, prob=0.60),
            self._make_signal("MKT-2", edge=0.10, prob=0.60),
        ]
        outcomes = {"MKT-1": 0, "MKT-2": 0}  # Both lose
        params = ParameterSet(min_edge_ai=0.05, min_confidence=0.50)

        result = replay_signals(signals, outcomes, params)
        assert result.max_drawdown > 0


class TestParameterSet:

    def test_label_format(self):
        p = ParameterSet()
        label = p.label()
        assert "kelly=" in label
        assert "edge_ai=" in label
        assert "conf=" in label


class TestReplayResultOOS:
    """Tests for out-of-sample flag on ReplayResult."""

    def test_default_is_in_sample(self):
        r = ReplayResult(params=ParameterSet())
        assert r.is_oos is False

    def test_can_set_oos(self):
        r = ReplayResult(params=ParameterSet(), is_oos=True)
        assert r.is_oos is True


class TestLoadDataSplit:
    """Tests for temporal train/test split in load_data."""

    def test_load_data_splits_temporally(self, tmp_db):
        """Signals should be split into train and test sets by timestamp order."""
        conn = tmp_db._get_conn()
        # Insert 10 signals with increasing timestamps
        for i in range(10):
            conn.execute(
                """INSERT INTO signals (market_id, strategy, direction, edge,
                   probability_estimate, market_price, confidence, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"MKT-{i}", "ai_probability", "BUY_YES", 0.08, 0.42, 0.34, 0.7,
                 f"2026-04-0{i+1 if i < 9 else 9}T{i:02d}:00:00Z"),
            )
        conn.commit()

        train, test, outcomes, has_synthetic = load_data(tmp_db, days=30, train_pct=0.60)

        assert len(train) == 6
        assert len(test) == 4
        assert len(train) + len(test) == 10

    def test_load_data_empty_db(self, tmp_db):
        train, test, outcomes, has_synthetic = load_data(tmp_db, days=30)
        assert train == []
        assert test == []
        assert outcomes == {}

    def test_load_data_detects_synthetic(self, tmp_db):
        """Should detect is_synthetic flag in market_snapshots."""
        conn = tmp_db._get_conn()
        # Insert a market first (for FK if enabled)
        conn.execute(
            """INSERT INTO markets (ticker, platform, question, first_seen, last_updated)
               VALUES ('SYN-MKT', 'kalshi', 'Test?', '2026-04-01', '2026-04-01')"""
        )
        conn.execute(
            """INSERT INTO market_snapshots (market_id, timestamp, yes_price, no_price, spread, is_synthetic)
               VALUES ('SYN-MKT', '2026-04-01T00:00:00', 0.50, 0.50, 0.0, 1)"""
        )
        conn.commit()

        _, _, _, has_synthetic = load_data(tmp_db, days=30)
        assert has_synthetic is True

    def test_load_data_no_synthetic(self, tmp_db):
        """Should return False when no synthetic snapshots exist."""
        _, _, _, has_synthetic = load_data(tmp_db, days=30)
        assert has_synthetic is False

    def test_train_pct_boundary(self, tmp_db):
        """100% train should put everything in train set."""
        conn = tmp_db._get_conn()
        for i in range(5):
            conn.execute(
                """INSERT INTO signals (market_id, strategy, direction, edge,
                   probability_estimate, market_price, confidence, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"MKT-{i}", "ai_probability", "BUY_YES", 0.08, 0.42, 0.34, 0.7,
                 f"2026-04-0{i+1}T00:00:00Z"),
            )
        conn.commit()

        train, test, _, _ = load_data(tmp_db, days=30, train_pct=1.0)
        assert len(train) == 5
        assert len(test) == 0


class TestFormatReplayReport:
    """Tests for the report formatter with IS/OOS sections."""

    def _make_result(self, pnl: float, is_oos: bool = False) -> ReplayResult:
        return ReplayResult(
            params=ParameterSet(),
            total_signals=10,
            acted_signals=5,
            total_pnl=pnl,
            win_rate=0.6,
            profit_factor=1.5,
            is_oos=is_oos,
        )

    def test_report_contains_in_sample_section(self):
        is_results = [self._make_result(50.0)]
        oos_results = [self._make_result(30.0, is_oos=True)]
        report = format_replay_report(is_results, oos_results, 30, 60, 40, 100)
        assert "IN-SAMPLE" in report
        assert "OUT-OF-SAMPLE" in report

    def test_report_contains_synthetic_warning(self):
        is_results = [self._make_result(50.0)]
        oos_results = [self._make_result(30.0, is_oos=True)]
        report = format_replay_report(
            is_results, oos_results, 30, 60, 40, 100, has_synthetic=True
        )
        assert "SYNTHETIC" in report
        assert "UPPER BOUND" in report

    def test_report_no_synthetic_warning_when_clean(self):
        is_results = [self._make_result(50.0)]
        oos_results = [self._make_result(30.0, is_oos=True)]
        report = format_replay_report(
            is_results, oos_results, 30, 60, 40, 100, has_synthetic=False
        )
        assert "SYNTHETIC" not in report

    def test_report_empty_oos(self):
        """Report should handle empty OOS results gracefully."""
        is_results = [self._make_result(50.0)]
        report = format_replay_report(is_results, [], 30, 100, 0, 50)
        assert "IN-SAMPLE" in report


class TestSyntheticSnapshotFlag:
    """Tests for is_synthetic flag on MarketSnapshot model and DB persistence."""

    def test_snapshot_default_not_synthetic(self):
        from src.core.models import MarketSnapshot
        snap = MarketSnapshot(
            market_id="TEST", yes_price=0.5, no_price=0.5, spread=0.0,
        )
        assert snap.is_synthetic is False

    def test_snapshot_synthetic_flag(self):
        from src.core.models import MarketSnapshot
        snap = MarketSnapshot(
            market_id="TEST", yes_price=0.5, no_price=0.5, spread=0.0,
            is_synthetic=True,
        )
        assert snap.is_synthetic is True

    def test_synthetic_persisted_to_db(self, tmp_db):
        from src.core.models import MarketSnapshot
        conn = tmp_db._get_conn()
        # Create parent market
        conn.execute(
            """INSERT INTO markets (ticker, platform, question, first_seen, last_updated)
               VALUES ('SYN-TEST', 'kalshi', 'Test?', '2026-04-01', '2026-04-01')"""
        )
        conn.commit()

        snap = MarketSnapshot(
            market_id="SYN-TEST", yes_price=0.5, no_price=0.5, spread=0.0,
            is_synthetic=True,
        )
        tmp_db.log_snapshot(snap)

        row = conn.execute(
            "SELECT is_synthetic FROM market_snapshots WHERE market_id = 'SYN-TEST'"
        ).fetchone()
        assert row is not None
        assert row["is_synthetic"] == 1

    def test_real_snapshot_persisted_as_zero(self, tmp_db):
        from src.core.models import MarketSnapshot
        conn = tmp_db._get_conn()
        conn.execute(
            """INSERT INTO markets (ticker, platform, question, first_seen, last_updated)
               VALUES ('REAL-TEST', 'kalshi', 'Test?', '2026-04-01', '2026-04-01')"""
        )
        conn.commit()

        snap = MarketSnapshot(
            market_id="REAL-TEST", yes_price=0.5, no_price=0.5, spread=0.0,
        )
        tmp_db.log_snapshot(snap)

        row = conn.execute(
            "SELECT is_synthetic FROM market_snapshots WHERE market_id = 'REAL-TEST'"
        ).fetchone()
        assert row is not None
        assert row["is_synthetic"] == 0
