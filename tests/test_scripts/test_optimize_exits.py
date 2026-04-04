"""Tests for the exit threshold optimizer script."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.optimize_exits import (
    ExitAnalysis,
    _normalize_reason,
    analyze_exits,
    format_exit_report,
)


class TestNormalizeReason:

    def test_stop_loss_variants(self):
        assert _normalize_reason("stop_loss") == "stop_loss"
        assert _normalize_reason("Stop Loss triggered") == "stop_loss"
        assert _normalize_reason("STOP_LOSS") == "stop_loss"

    def test_take_profit_variants(self):
        assert _normalize_reason("take_profit") == "take_profit"
        assert _normalize_reason("Take Profit hit") == "take_profit"

    def test_edge_gone_variants(self):
        assert _normalize_reason("edge_gone") == "edge_gone"
        assert _normalize_reason("Edge Gone: price moved") == "edge_gone"

    def test_time_limit_variants(self):
        assert _normalize_reason("time_limit") == "time_limit"
        assert _normalize_reason("max_hold exceeded") == "time_limit"
        assert _normalize_reason("expired") == "time_limit"

    def test_trailing_stop(self):
        assert _normalize_reason("trailing_stop_hit") == "trailing_stop"

    def test_capital_rotation(self):
        assert _normalize_reason("capital_rotation") == "capital_rotation"

    def test_market_resolved(self):
        assert _normalize_reason("settled") == "market_resolved"
        assert _normalize_reason("market resolved YES") == "market_resolved"

    def test_unknown_reason(self):
        assert _normalize_reason("") == "unknown"
        assert _normalize_reason("some_custom_reason") == "some_custom_reason"

    def test_long_reason_truncated(self):
        long_reason = "a" * 50
        result = _normalize_reason(long_reason)
        assert len(result) <= 30


class TestExitAnalysis:

    def test_defaults(self):
        ea = ExitAnalysis(reason="stop_loss")
        assert ea.count == 0
        assert ea.total_pnl == 0.0
        assert ea.premature_exits == 0

    def test_fields(self):
        ea = ExitAnalysis(
            reason="take_profit",
            count=10,
            total_pnl=50.0,
            avg_pnl=5.0,
            win_rate=0.80,
            premature_exits=1,
        )
        assert ea.reason == "take_profit"
        assert ea.count == 10
        assert ea.win_rate == 0.80


def _mock_db_no_data():
    """Mock DB that returns no exit data."""
    db = MagicMock()
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    db._get_conn.return_value = conn
    return db


def _make_row(data: dict):
    """Create a mock sqlite3.Row-like object that supports dict() and .get()."""
    m = MagicMock()
    m.__getitem__ = lambda self, key: data[key]
    m.keys = lambda: data.keys()
    # Make dict(row) work
    m.__iter__ = lambda self: iter(data)
    m.__len__ = lambda self: len(data)
    # Support .get()
    m.get = lambda key, default=None: data.get(key, default)
    return m


def _mock_db_with_exits(exits, snapshots=None, trades=None, pairs=None):
    """Mock DB with exit and trade data."""
    db = MagicMock()
    conn = MagicMock()
    db._get_conn.return_value = conn

    call_count = [0]

    def mock_execute(sql, params=None):
        result = MagicMock()
        sql_lower = sql.strip().lower()

        if "position_exits" in sql_lower:
            result.fetchall.return_value = [_make_row(e) for e in exits]
        elif "market_snapshots" in sql_lower:
            rows = snapshots or []
            result.fetchall.return_value = [_make_row(s) for s in rows]
        elif "from trades" in sql_lower and "join" not in sql_lower:
            rows = trades or []
            result.fetchall.return_value = [_make_row(t) for t in rows]
        elif "join trades" in sql_lower or ("from trades b" in sql_lower):
            rows = pairs or []
            result.fetchall.return_value = [_make_row(p) for p in rows]
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None

        return result

    conn.execute = mock_execute
    return db


class TestAnalyzeExits:

    def test_no_data_returns_error(self):
        db = _mock_db_no_data()
        result = analyze_exits(db, days=30)
        assert "error" in result

    def test_basic_exit_analysis(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.50,
                "position_size": 10.0,
                "realized_pnl": -2.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
            {
                "market_id": "M2",
                "strategy": "ai_probability",
                "exit_reason": "take_profit",
                "exit_price": 0.80,
                "position_size": 10.0,
                "realized_pnl": 5.0,
                "timestamp": "2026-04-02T12:00:00Z",
            },
        ]
        trades = [
            {"market_id": "M1", "realized_pnl": -2.0, "price": 0.60, "size": 10.0},
            {"market_id": "M2", "realized_pnl": 5.0, "price": 0.50, "size": 10.0},
        ]
        db = _mock_db_with_exits(exits, trades=trades)
        result = analyze_exits(db, days=60)

        assert "error" not in result
        assert result["total_exits"] == 2
        assert len(result["by_reason"]) == 2

    def test_premature_exit_detection(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.50,
                "position_size": 10.0,
                "realized_pnl": -2.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
        ]
        # Price moved up significantly after exit
        snapshots = [
            {"yes_price": 0.55},
            {"yes_price": 0.60},
            {"yes_price": 0.70},
        ]
        trades = [
            {"market_id": "M1", "realized_pnl": -2.0, "price": 0.60, "size": 10.0},
        ]
        db = _mock_db_with_exits(exits, snapshots=snapshots, trades=trades)
        result = analyze_exits(db, days=60)

        stop_loss_analysis = [a for a in result["by_reason"] if a.reason == "stop_loss"]
        assert len(stop_loss_analysis) == 1
        # Price went from 0.50 to 0.70 (>5% favorable), so should be premature
        assert stop_loss_analysis[0].premature_exits == 1

    def test_no_premature_exit_when_price_flat(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.50,
                "position_size": 10.0,
                "realized_pnl": -2.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
        ]
        # Price stayed flat after exit
        snapshots = [
            {"yes_price": 0.50},
            {"yes_price": 0.51},
        ]
        trades = []
        db = _mock_db_with_exits(exits, snapshots=snapshots, trades=trades)
        result = analyze_exits(db, days=60)

        stop_loss_analysis = [a for a in result["by_reason"] if a.reason == "stop_loss"]
        assert stop_loss_analysis[0].premature_exits == 0


class TestStopLossAnalysis:

    def test_stop_loss_with_trades(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.40,
                "position_size": 10.0,
                "realized_pnl": -3.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
        ]
        trades = [
            {"market_id": "M1", "realized_pnl": -3.0, "price": 0.60, "size": 10.0},
            {"market_id": "M2", "realized_pnl": 5.0, "price": 0.50, "size": 10.0},
        ]
        db = _mock_db_with_exits(exits, trades=trades)
        result = analyze_exits(db, days=60)

        sl = result["stop_loss_analysis"]
        assert sl["total_losses"] == 1
        assert sl["total_wins"] == 1
        assert sl["optimal_stop_loss"] is not None

    def test_no_trades_returns_none(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.40,
                "position_size": 10.0,
                "realized_pnl": -3.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
        ]
        db = _mock_db_with_exits(exits, trades=[])
        result = analyze_exits(db, days=60)
        assert result["stop_loss_analysis"]["optimal_stop_loss"] is None


class TestHoldPeriodAnalysis:

    def test_with_pairs(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "take_profit",
                "exit_price": 0.80,
                "position_size": 10.0,
                "realized_pnl": 5.0,
                "timestamp": "2026-04-02T12:00:00Z",
            },
        ]
        pairs = [
            {
                "market_id": "M1",
                "buy_time": "2026-03-30T12:00:00Z",
                "sell_time": "2026-04-02T12:00:00Z",
                "realized_pnl": 5.0,
                "days_held": 3.0,
            },
            {
                "market_id": "M2",
                "buy_time": "2026-03-20T12:00:00Z",
                "sell_time": "2026-04-02T12:00:00Z",
                "realized_pnl": -1.0,
                "days_held": 13.0,
            },
        ]
        db = _mock_db_with_exits(exits, pairs=pairs)
        result = analyze_exits(db, days=60)

        hp = result["hold_period_analysis"]
        assert hp["total_pairs"] == 2
        assert "0-3d" in hp["buckets"]
        assert "7-14d" in hp["buckets"]

    def test_no_pairs(self):
        exits = [
            {
                "market_id": "M1",
                "strategy": "ai_probability",
                "exit_reason": "stop_loss",
                "exit_price": 0.40,
                "position_size": 10.0,
                "realized_pnl": -3.0,
                "timestamp": "2026-04-01T12:00:00Z",
            },
        ]
        db = _mock_db_with_exits(exits, pairs=[])
        result = analyze_exits(db, days=60)
        assert result["hold_period_analysis"]["optimal_hold_days"] is None


class TestFormatExitReport:

    def test_basic_report(self):
        analysis = {
            "period_days": 60,
            "total_exits": 5,
            "by_reason": [
                ExitAnalysis(reason="stop_loss", count=3, total_pnl=-10.0,
                             avg_pnl=-3.33, win_rate=0.0, premature_exits=1),
                ExitAnalysis(reason="take_profit", count=2, total_pnl=15.0,
                             avg_pnl=7.50, win_rate=1.0, premature_exits=0),
            ],
            "stop_loss_analysis": {
                "optimal_stop_loss": 0.20,
                "total_losses": 3,
                "total_wins": 2,
                "total_loss_pnl": -10.0,
                "total_win_pnl": 15.0,
                "loss_pct_median": 0.15,
                "thresholds": [
                    {"threshold": 0.20, "would_stop": 2, "capital_saved": 3.50},
                ],
            },
            "hold_period_analysis": {
                "total_pairs": 5,
                "buckets": {
                    "0-3d": {"count": 2, "total_pnl": 8.0, "avg_pnl": 4.0, "win_rate": 1.0},
                    "7-14d": {"count": 3, "total_pnl": -3.0, "avg_pnl": -1.0, "win_rate": 0.333},
                },
            },
        }
        report = format_exit_report(analysis)
        assert "EXIT OPTIMIZATION REPORT" in report
        assert "stop_loss" in report
        assert "take_profit" in report
        assert "Stop-Loss Analysis" in report
        assert "Hold Period Analysis" in report
        assert "0-3d" in report

    def test_empty_analysis(self):
        analysis = {
            "period_days": 30,
            "total_exits": 0,
            "by_reason": [],
            "stop_loss_analysis": {},
            "hold_period_analysis": {},
        }
        report = format_exit_report(analysis)
        assert "EXIT OPTIMIZATION REPORT" in report
        assert "Total exits: 0" in report
