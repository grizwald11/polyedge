"""Tests for backtest framework."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.run_backtest import (
    BacktestResult,
    _analyze_strategy,
    _compute_brier,
    format_report,
    run_backtest,
)
from src.storage.database import Database


def _insert_trade(db, market_id, strategy, pnl, side="BUY", price=0.50, size=10, days_ago=5):
    """Insert a trade directly into the database."""
    conn = db._get_conn()
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute("""
        INSERT INTO trades (order_id, market_id, token_id, side, price, size, fee, realized_pnl,
                           strategy, paper, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        f"order-{market_id}-{days_ago}",
        market_id,
        f"{market_id}_yes",
        side,
        price,
        size,
        0.0,
        pnl,
        strategy,
        1,
        ts,
    ))
    conn.commit()
    conn.close()


def _insert_calibration(db, market_id, strategy, predicted, actual_outcome, days_ago=5):
    """Insert a calibration record."""
    conn = db._get_conn()
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute("""
        INSERT INTO calibration_records (market_id, market_question, strategy,
                                        predicted_probability, market_price_at_prediction,
                                        actual_outcome, predicted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (market_id, "Test?", strategy, predicted, 0.50, int(actual_outcome), ts))
    conn.commit()
    conn.close()


class TestRunBacktest:
    def test_empty_db(self, tmp_db):
        results = run_backtest(tmp_db, days=30)
        assert results == []

    def test_single_strategy(self, tmp_db):
        _insert_trade(tmp_db, "M1", "ai_probability", pnl=5.0, days_ago=3)
        _insert_trade(tmp_db, "M2", "ai_probability", pnl=-2.0, days_ago=2)

        results = run_backtest(tmp_db, days=30)
        assert len(results) == 1
        assert results[0].strategy == "ai_probability"
        assert results[0].total_trades == 2
        assert results[0].winning_trades == 1
        assert results[0].losing_trades == 1
        assert results[0].total_pnl == pytest.approx(3.0)

    def test_multiple_strategies(self, tmp_db):
        _insert_trade(tmp_db, "M1", "ai_probability", pnl=5.0)
        _insert_trade(tmp_db, "M2", "obvious_no", pnl=1.0)

        results = run_backtest(tmp_db, days=30)
        assert len(results) == 2
        strategies = {r.strategy for r in results}
        assert "ai_probability" in strategies
        assert "obvious_no" in strategies

    def test_strategy_filter(self, tmp_db):
        _insert_trade(tmp_db, "M1", "ai_probability", pnl=5.0)
        _insert_trade(tmp_db, "M2", "obvious_no", pnl=1.0)

        results = run_backtest(tmp_db, days=30, strategy="obvious_no")
        assert len(results) == 1
        assert results[0].strategy == "obvious_no"

    def test_days_filter(self, tmp_db):
        _insert_trade(tmp_db, "M1", "ai_probability", pnl=5.0, days_ago=3)
        _insert_trade(tmp_db, "M2", "ai_probability", pnl=10.0, days_ago=60)

        results = run_backtest(tmp_db, days=30)
        assert len(results) == 1
        assert results[0].total_trades == 1
        assert results[0].total_pnl == pytest.approx(5.0)


class TestAnalyzeStrategy:
    def test_win_rate(self):
        trades = [
            {"realized_pnl": 5.0},
            {"realized_pnl": 3.0},
            {"realized_pnl": -2.0},
        ]
        result = _analyze_strategy("test", trades, [], 500.0)
        assert result.win_rate == pytest.approx(2 / 3)

    def test_max_drawdown(self):
        trades = [
            {"realized_pnl": 10.0},
            {"realized_pnl": -20.0},
            {"realized_pnl": 5.0},
        ]
        result = _analyze_strategy("test", trades, [], 500.0)
        assert result.max_drawdown == pytest.approx(20.0)

    def test_profit_factor(self):
        trades = [
            {"realized_pnl": 10.0},
            {"realized_pnl": -5.0},
        ]
        result = _analyze_strategy("test", trades, [], 500.0)
        assert result.profit_factor == pytest.approx(2.0)

    def test_no_losses_infinite_pf(self):
        trades = [{"realized_pnl": 5.0}]
        result = _analyze_strategy("test", trades, [], 500.0)
        assert result.profit_factor == float("inf")

    def test_brier_from_calibrations(self):
        cals = [
            {"predicted_probability": 0.80, "actual_outcome": 1},
            {"predicted_probability": 0.20, "actual_outcome": 0},
        ]
        result = _analyze_strategy("test", [], cals, 500.0)
        assert result.brier_score == pytest.approx(0.04)


class TestComputeBrier:
    def test_perfect_calibration(self):
        cals = [
            {"predicted_probability": 1.0, "actual_outcome": 1},
            {"predicted_probability": 0.0, "actual_outcome": 0},
        ]
        assert _compute_brier(cals) == pytest.approx(0.0)

    def test_worst_calibration(self):
        cals = [
            {"predicted_probability": 1.0, "actual_outcome": 0},
            {"predicted_probability": 0.0, "actual_outcome": 1},
        ]
        assert _compute_brier(cals) == pytest.approx(1.0)

    def test_empty(self):
        assert _compute_brier([]) is None

    def test_no_resolved(self):
        assert _compute_brier([{"predicted_probability": 0.5, "actual_outcome": None}]) is None


class TestFormatReport:
    def test_basic_format(self):
        results = [
            BacktestResult(
                strategy="ai_probability",
                total_trades=10,
                winning_trades=6,
                losing_trades=4,
                total_pnl=25.50,
                win_rate=0.6,
                avg_win=8.0,
                avg_loss=-5.0,
                profit_factor=2.4,
                max_drawdown=15.0,
                brier_score=0.18,
            ),
        ]
        report = format_report(results, 30)
        assert "ai_probability" in report
        assert "60.0%" in report
        assert "$25.50" in report
        assert "0.180" in report

    def test_empty_results(self):
        report = format_report([], 30)
        assert "No trades" in report
