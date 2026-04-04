"""Tests for the parameter replay backtest."""

from __future__ import annotations

import pytest

from scripts.parameter_replay import (
    ParameterSet,
    ReplayResult,
    _simple_kelly_size,
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
