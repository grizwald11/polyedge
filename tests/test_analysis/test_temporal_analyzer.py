"""Tests for the Price History Temporal Analyzer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.analysis.temporal_analyzer import (
    TemporalAnalyzer,
    TemporalSignals,
)


# ─── Helper to build mock DB with snapshot data ───


def _mock_db_with_snapshots(prices: list[float], hours_apart: float = 4.0):
    """Create a mock DB that returns snapshots at regular intervals."""
    now = datetime.now(timezone.utc)
    snapshots = []
    for i, price in enumerate(prices):
        ts = now - timedelta(hours=(len(prices) - 1 - i) * hours_apart)
        snapshots.append({
            "market_id": "TEST-MKT",
            "timestamp": ts.isoformat(),
            "yes_price": price,
            "no_price": 1.0 - price,
            "spread": 0.02,
        })

    db = MagicMock()
    db.get_snapshots_for_market.return_value = snapshots
    return db


# ─── Already Priced In tests ───


class TestAlreadyPricedIn:
    def test_no_movement(self):
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.50, price_7d_ago=0.50, claude_estimate=0.70,
        )
        assert result == 0.0

    def test_fully_priced_in(self):
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.70, price_7d_ago=0.50, claude_estimate=0.70,
        )
        assert result == pytest.approx(1.0, abs=0.01)

    def test_partially_priced_in(self):
        # Price moved from 0.50 to 0.65, Claude says 0.70
        # Movement: 0.15, Expected: 0.20 → 75% priced in
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.65, price_7d_ago=0.50, claude_estimate=0.70,
        )
        assert result == pytest.approx(0.75, abs=0.01)

    def test_opposite_direction_returns_zero(self):
        # Price went down but Claude says it should go up
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.45, price_7d_ago=0.50, claude_estimate=0.70,
        )
        assert result == 0.0

    def test_no_expected_movement(self):
        # Claude estimate equals starting price — no expected movement
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.52, price_7d_ago=0.50, claude_estimate=0.50,
        )
        assert result == 0.0

    def test_overshot(self):
        # Price moved beyond Claude's estimate — clamped to 1.0
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.80, price_7d_ago=0.50, claude_estimate=0.70,
        )
        assert result == 1.0

    def test_downward_movement(self):
        # Claude says 30% (down from 50%), price moved to 35%
        result = TemporalAnalyzer._compute_already_priced_in(
            current_price=0.35, price_7d_ago=0.50, claude_estimate=0.30,
        )
        # Movement: -0.15, Expected: -0.20 → 75%
        assert result == pytest.approx(0.75, abs=0.01)


# ─── Momentum tests ───


class TestMomentum:
    def test_flat_prices(self):
        prices = [0.50] * 10
        assert TemporalAnalyzer._compute_momentum(prices) == 0.0

    def test_upward_trend(self):
        prices = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
        momentum = TemporalAnalyzer._compute_momentum(prices)
        assert momentum > 0.5

    def test_downward_trend(self):
        prices = [0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40]
        momentum = TemporalAnalyzer._compute_momentum(prices)
        assert momentum < -0.5

    def test_single_price(self):
        assert TemporalAnalyzer._compute_momentum([0.50]) == 0.0

    def test_normalized_range(self):
        prices = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
        momentum = TemporalAnalyzer._compute_momentum(prices)
        assert -1.0 <= momentum <= 1.0


# ─── Volatility tests ───


class TestVolatility:
    def test_zero_volatility(self):
        prices = [0.50, 0.50, 0.50]
        assert TemporalAnalyzer._compute_volatility(prices) == 0.0

    def test_positive_volatility(self):
        prices = [0.50, 0.55, 0.45, 0.60, 0.40]
        vol = TemporalAnalyzer._compute_volatility(prices)
        assert vol > 0

    def test_single_price(self):
        assert TemporalAnalyzer._compute_volatility([0.50]) == 0.0


# ─── Mean Reversion tests ───


class TestMeanReversion:
    def test_at_mean(self):
        prices = [0.50, 0.50, 0.50, 0.50]
        z = TemporalAnalyzer._compute_mean_reversion(0.50, prices)
        assert z == pytest.approx(0.0, abs=0.01)

    def test_above_mean(self):
        prices = [0.48, 0.50, 0.52, 0.50]
        z = TemporalAnalyzer._compute_mean_reversion(0.60, prices)
        assert z > 0

    def test_below_mean(self):
        prices = [0.50, 0.52, 0.48, 0.50]
        z = TemporalAnalyzer._compute_mean_reversion(0.40, prices)
        assert z < 0

    def test_single_price(self):
        assert TemporalAnalyzer._compute_mean_reversion(0.50, [0.50]) == 0.0


# ─── Full analysis integration tests ───


class TestAnalyze:
    def test_insufficient_data(self):
        db = _mock_db_with_snapshots([0.50, 0.50])  # Only 2 snapshots
        analyzer = TemporalAnalyzer()
        result = analyzer.analyze("TEST", 0.55, 0.70, db)
        assert not result.has_sufficient_data
        assert result.snapshot_count == 2

    def test_with_upward_trend(self):
        prices = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
        db = _mock_db_with_snapshots(prices)
        analyzer = TemporalAnalyzer()
        result = analyzer.analyze("TEST", 0.70, 0.80, db)
        assert result.has_sufficient_data
        assert result.momentum_7d > 0
        assert result.price_7d_ago == pytest.approx(0.40, abs=0.01)
        # 0.70 - 0.40 = 0.30 of 0.80 - 0.40 = 0.40 → 75%
        assert result.already_priced_in_pct == pytest.approx(0.75, abs=0.01)

    def test_flat_market(self):
        prices = [0.50] * 10
        db = _mock_db_with_snapshots(prices)
        analyzer = TemporalAnalyzer()
        result = analyzer.analyze("TEST", 0.50, 0.70, db)
        assert result.has_sufficient_data
        assert result.already_priced_in_pct == 0.0
        assert result.momentum_7d == 0.0


# ─── Edge discount tests ───


class TestEdgeDiscount:
    def test_no_data_returns_1(self):
        analyzer = TemporalAnalyzer()
        signals = TemporalSignals(has_sufficient_data=False)
        assert analyzer.compute_edge_discount(signals) == 1.0

    def test_no_priced_in_returns_1(self):
        analyzer = TemporalAnalyzer()
        signals = TemporalSignals(
            has_sufficient_data=True,
            already_priced_in_pct=0.0,
            momentum_7d=0.0,
        )
        assert analyzer.compute_edge_discount(signals) == 1.0

    def test_fully_priced_in_heavy_discount(self):
        analyzer = TemporalAnalyzer(discount_weight=0.7)
        signals = TemporalSignals(
            has_sufficient_data=True,
            already_priced_in_pct=1.0,
            momentum_7d=0.0,
        )
        discount = analyzer.compute_edge_discount(signals)
        # 1.0 - (1.0 * 0.7) = 0.3
        assert discount == pytest.approx(0.3, abs=0.01)

    def test_partially_priced_in(self):
        analyzer = TemporalAnalyzer(discount_weight=0.7)
        signals = TemporalSignals(
            has_sufficient_data=True,
            already_priced_in_pct=0.5,
            momentum_7d=0.0,
        )
        discount = analyzer.compute_edge_discount(signals)
        # 1.0 - (0.5 * 0.7) = 0.65
        assert discount == pytest.approx(0.65, abs=0.01)

    def test_strong_momentum_adds_penalty(self):
        analyzer = TemporalAnalyzer(discount_weight=0.7)
        signals = TemporalSignals(
            has_sufficient_data=True,
            already_priced_in_pct=0.0,
            momentum_7d=0.5,
        )
        discount = analyzer.compute_edge_discount(signals)
        # api_discount = 1.0, momentum_penalty = 1.0 - 0.5*0.15 = 0.925
        assert discount < 1.0
        assert discount > 0.9

    def test_minimum_discount_floor(self):
        analyzer = TemporalAnalyzer(discount_weight=1.0)
        signals = TemporalSignals(
            has_sufficient_data=True,
            already_priced_in_pct=1.0,
            momentum_7d=0.9,
        )
        discount = analyzer.compute_edge_discount(signals)
        assert discount >= 0.1  # Floor
