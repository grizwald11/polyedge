"""Tests for the Market Regime Detector."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.analysis.regime_detector import (
    MarketRegime,
    RegimeAnalysis,
    RegimeDetector,
    RegimeMultipliers,
    REGIME_MULTIPLIERS,
)


def _make_db(snapshots_by_market: dict[str, list[dict]]) -> MagicMock:
    """Create a mock database with controlled snapshot data.

    Args:
        snapshots_by_market: dict of market_id -> list of snapshot dicts.
            Each snapshot should have at least: yes_price, volume_1h, timestamp.
    """
    db = MagicMock()
    conn = MagicMock()
    db._get_conn.return_value = conn

    # Return market IDs from the DISTINCT query
    market_ids = [(mid,) for mid in snapshots_by_market.keys()]
    conn.execute.return_value.fetchall.return_value = market_ids

    def get_snapshots(market_id, start=None, end=None):
        all_snaps = snapshots_by_market.get(market_id, [])
        if start:
            all_snaps = [s for s in all_snaps if s.get("timestamp", "") >= start]
        return all_snaps

    db.get_snapshots_for_market = MagicMock(side_effect=get_snapshots)
    return db


def _snapshot(price: float, volume: float = 100.0, hours_ago: float = 0) -> dict:
    """Create a snapshot dict."""
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "yes_price": price,
        "no_price": 1.0 - price,
        "volume_1h": volume,
        "timestamp": ts.isoformat(),
        "spread": 0.02,
    }


# ─── Regime classification tests ───


class TestClassify:
    def test_low_vol_calm_market(self):
        detector = RegimeDetector()
        regime = detector._classify(volatility=0.01, volume_ratio=0.8)
        assert regime == MarketRegime.LOW_VOL

    def test_normal_regime(self):
        detector = RegimeDetector()
        regime = detector._classify(volatility=0.03, volume_ratio=1.0)
        assert regime == MarketRegime.NORMAL

    def test_high_vol(self):
        detector = RegimeDetector()
        regime = detector._classify(volatility=0.08, volume_ratio=1.0)
        assert regime == MarketRegime.HIGH_VOL

    def test_crisis_from_volatility(self):
        detector = RegimeDetector()
        regime = detector._classify(volatility=0.15, volume_ratio=1.0)
        assert regime == MarketRegime.CRISIS

    def test_volume_spike_escalates_normal_to_high(self):
        detector = RegimeDetector()
        # Normal volatility but 3x volume spike → HIGH_VOL
        regime = detector._classify(volatility=0.03, volume_ratio=3.0)
        assert regime == MarketRegime.HIGH_VOL

    def test_volume_spike_escalates_high_to_crisis(self):
        detector = RegimeDetector()
        # High vol + 5x volume → CRISIS
        regime = detector._classify(volatility=0.08, volume_ratio=5.0)
        assert regime == MarketRegime.CRISIS

    def test_volume_spike_escalates_low_to_normal(self):
        detector = RegimeDetector()
        # Low vol + 2.5x volume → NORMAL (not low anymore)
        regime = detector._classify(volatility=0.01, volume_ratio=2.5)
        assert regime == MarketRegime.NORMAL

    def test_boundary_at_vol_low(self):
        detector = RegimeDetector()
        # Exactly at VOL_LOW boundary → NORMAL
        regime = detector._classify(volatility=0.02, volume_ratio=1.0)
        assert regime == MarketRegime.NORMAL

    def test_boundary_at_vol_high(self):
        detector = RegimeDetector()
        # Exactly at VOL_HIGH boundary → HIGH_VOL
        regime = detector._classify(volatility=0.06, volume_ratio=1.0)
        assert regime == MarketRegime.HIGH_VOL


# ─── Multiplier tests ───


class TestMultipliers:
    def test_all_regimes_have_multipliers(self):
        for regime in MarketRegime:
            assert regime in REGIME_MULTIPLIERS

    def test_low_vol_relaxes_edge_boosts_kelly(self):
        m = REGIME_MULTIPLIERS[MarketRegime.LOW_VOL]
        assert m.edge_multiplier < 1.0  # Relaxed edge
        assert m.kelly_multiplier > 1.0  # Larger positions

    def test_normal_is_unity(self):
        m = REGIME_MULTIPLIERS[MarketRegime.NORMAL]
        assert m.edge_multiplier == 1.0
        assert m.kelly_multiplier == 1.0

    def test_high_vol_tightens_edge_reduces_kelly(self):
        m = REGIME_MULTIPLIERS[MarketRegime.HIGH_VOL]
        assert m.edge_multiplier > 1.0
        assert m.kelly_multiplier < 1.0

    def test_crisis_most_conservative(self):
        m = REGIME_MULTIPLIERS[MarketRegime.CRISIS]
        assert m.edge_multiplier >= 2.0
        assert m.kelly_multiplier <= 0.3

    def test_description_format(self):
        m = RegimeMultipliers(edge_multiplier=1.5, kelly_multiplier=0.6)
        assert "edge×1.5" in m.description
        assert "kelly×0.6" in m.description


# ─── detect_regime integration tests with mock DB ───


class TestDetectRegime:
    def test_insufficient_markets_returns_normal(self):
        """With fewer than MIN_MARKETS, default to NORMAL."""
        db = _make_db({"mkt1": [_snapshot(0.50)]})
        detector = RegimeDetector()
        result = detector.detect_regime(db)
        assert result.regime == MarketRegime.NORMAL
        assert result.markets_analyzed == 0
        assert "default" in result.detail

    def test_calm_market_detected_as_low_vol(self):
        """All markets with tiny price changes → LOW_VOL."""
        snapshots = {}
        for i in range(10):
            # Barely any price change over 24h
            snapshots[f"mkt{i}"] = [
                _snapshot(0.50 + i * 0.001, volume=50, hours_ago=20),
                _snapshot(0.50 + i * 0.001 + 0.005, volume=50, hours_ago=0),
            ]
        db = _make_db(snapshots)
        detector = RegimeDetector()
        result = detector.detect_regime(db)
        assert result.regime == MarketRegime.LOW_VOL
        assert result.cross_market_volatility < RegimeDetector.VOL_LOW
        assert result.markets_analyzed >= 5

    def test_volatile_market_detected_as_high_vol(self):
        """Markets with large price swings → HIGH_VOL or CRISIS."""
        snapshots = {}
        for i in range(10):
            # Varying large price changes: 0.08 to 0.17
            base = 0.40
            change = 0.08 + i * 0.01  # 0.08, 0.09, ... 0.17
            snapshots[f"mkt{i}"] = [
                _snapshot(base, volume=100, hours_ago=20),
                _snapshot(base + change, volume=100, hours_ago=0),
            ]
        db = _make_db(snapshots)
        detector = RegimeDetector()
        result = detector.detect_regime(db)
        assert result.regime in (MarketRegime.HIGH_VOL, MarketRegime.CRISIS)
        assert result.cross_market_volatility >= RegimeDetector.VOL_HIGH

    def test_mixed_volatility_returns_normal(self):
        """Some calm, some volatile → likely NORMAL."""
        snapshots = {}
        # 5 calm markets
        for i in range(5):
            snapshots[f"calm{i}"] = [
                _snapshot(0.50, volume=80, hours_ago=20),
                _snapshot(0.51, volume=80, hours_ago=0),
            ]
        # 5 slightly more volatile
        for i in range(5):
            snapshots[f"vol{i}"] = [
                _snapshot(0.50, volume=120, hours_ago=20),
                _snapshot(0.54, volume=120, hours_ago=0),
            ]
        db = _make_db(snapshots)
        detector = RegimeDetector()
        result = detector.detect_regime(db)
        # Average vol should be moderate
        assert result.regime in (MarketRegime.NORMAL, MarketRegime.LOW_VOL)

    def test_db_error_returns_normal(self):
        """Database errors should not crash, just return NORMAL."""
        db = MagicMock()
        db._get_conn.side_effect = Exception("DB connection failed")
        detector = RegimeDetector()
        result = detector.detect_regime(db)
        assert result.regime == MarketRegime.NORMAL


# ─── Std dev helper ───


class TestStdDev:
    def test_empty_list(self):
        assert RegimeDetector._std_dev([]) == 0.0

    def test_single_value(self):
        assert RegimeDetector._std_dev([5.0]) == 0.0

    def test_known_values(self):
        # [2, 4, 4, 4, 5, 5, 7, 9] → mean=5, variance=4, std=2
        result = RegimeDetector._std_dev([2, 4, 4, 4, 5, 5, 7, 9])
        assert result == pytest.approx(2.0, abs=0.01)

    def test_identical_values(self):
        assert RegimeDetector._std_dev([3.0, 3.0, 3.0]) == 0.0


# ─── RegimeAnalysis dataclass ───


class TestRegimeAnalysis:
    def test_analysis_fields(self):
        analysis = RegimeAnalysis(
            regime=MarketRegime.HIGH_VOL,
            multipliers=REGIME_MULTIPLIERS[MarketRegime.HIGH_VOL],
            cross_market_volatility=0.08,
            volume_spike_ratio=1.5,
            markets_analyzed=20,
            detail="test detail",
        )
        assert analysis.regime == MarketRegime.HIGH_VOL
        assert analysis.multipliers.edge_multiplier == 1.5
        assert analysis.multipliers.kelly_multiplier == 0.6
        assert analysis.markets_analyzed == 20
