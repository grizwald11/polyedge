"""Tests for order book imbalance analyzer."""

from __future__ import annotations

import pytest

from src.data.orderbook_analyzer import (
    OrderBookAnalysis,
    analyze_orderbook,
    apply_orderbook_signal,
    _vwap,
    _parse_levels,
    _compute_confidence_modifier,
    MIN_DEPTH_FOR_SIGNAL,
    STRONG_IMBALANCE,
)


class TestAnalyzeOrderbook:
    def test_balanced_book(self):
        """Balanced order book should have ~1.0 imbalance ratio."""
        book = {
            "yes": [[0.40, 100], [0.39, 100], [0.38, 100]],
            "no": [[0.62, 100], [0.63, 100], [0.64, 100]],
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert 0.8 <= result.imbalance_ratio <= 1.2
        assert not result.has_buy_imbalance
        assert not result.has_sell_imbalance
        assert abs(result.confidence_modifier) < 0.05

    def test_strong_buy_imbalance(self):
        """Heavy bid side should show buy imbalance."""
        book = {
            "yes": [[0.40, 200], [0.39, 200], [0.38, 200]],  # 600 total
            "no": [[0.62, 50], [0.63, 50]],  # 100 total
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert result.imbalance_ratio >= STRONG_IMBALANCE
        assert result.has_buy_imbalance
        assert not result.has_sell_imbalance
        assert result.confidence_modifier > 0

    def test_strong_sell_imbalance(self):
        """Heavy ask side should show sell imbalance."""
        book = {
            "yes": [[0.40, 20], [0.39, 20]],  # 40 total
            "no": [[0.62, 200], [0.63, 200], [0.64, 200]],  # 600 total
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert result.has_sell_imbalance
        assert not result.has_buy_imbalance
        assert result.confidence_modifier < 0

    def test_empty_orderbook(self):
        assert analyze_orderbook({}) is None
        assert analyze_orderbook(None) is None

    def test_one_sided_book(self):
        """Only bids, no asks."""
        book = {
            "yes": [[0.40, 100], [0.39, 100]],
            "no": [],
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert result.ask_depth == 0
        assert result.bid_depth == 200

    def test_spread_calculation(self):
        book = {
            "yes": [[0.38, 100], [0.37, 50]],
            "no": [[0.60, 100], [0.61, 50]],
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert result.spread >= 0

    def test_vwap_calculation(self):
        book = {
            "yes": [[0.40, 100], [0.35, 200]],  # VWAP = (40*100 + 35*200)/300 = 36.67
            "no": [[0.60, 100]],
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert abs(result.vwap_bid - 0.3667) < 0.01

    def test_low_depth_no_signal(self):
        """Below MIN_DEPTH_FOR_SIGNAL, no imbalance signal."""
        book = {
            "yes": [[0.40, 10], [0.39, 10]],  # 20 total < 50 threshold
            "no": [[0.62, 5]],
        }
        result = analyze_orderbook(book, side="yes")
        assert result is not None
        assert not result.has_buy_imbalance
        assert result.confidence_modifier == 0.0


class TestApplyOrderbookSignal:
    def test_no_analysis_passthrough(self):
        edge, conf = apply_orderbook_signal(0.08, 0.7, None, "BUY_YES")
        assert edge == 0.08
        assert conf == 0.7

    def test_buy_imbalance_boosts_buy_yes(self):
        analysis = OrderBookAnalysis(
            vwap_bid=0.40, vwap_ask=0.42, vwap_midpoint=0.41,
            bid_depth=500, ask_depth=100, imbalance_ratio=5.0,
            has_buy_imbalance=True, has_sell_imbalance=False,
            confidence_modifier=0.10, spread=0.02,
            levels_bid=5, levels_ask=2,
        )
        edge, conf = apply_orderbook_signal(0.08, 0.7, analysis, "BUY_YES")
        assert conf > 0.7  # Confidence boosted
        assert edge > 0.08  # Edge boosted

    def test_buy_imbalance_reduces_buy_no(self):
        """Buy imbalance is adverse for BUY_NO — should reduce confidence."""
        analysis = OrderBookAnalysis(
            vwap_bid=0.40, vwap_ask=0.42, vwap_midpoint=0.41,
            bid_depth=500, ask_depth=100, imbalance_ratio=5.0,
            has_buy_imbalance=True, has_sell_imbalance=False,
            confidence_modifier=0.10, spread=0.02,
            levels_bid=5, levels_ask=2,
        )
        edge, conf = apply_orderbook_signal(0.08, 0.7, analysis, "BUY_NO")
        assert conf < 0.7  # Confidence reduced
        assert edge < 0.08  # Edge reduced

    def test_confidence_clamped(self):
        """Confidence stays in [0, 1]."""
        analysis = OrderBookAnalysis(
            vwap_bid=0.40, vwap_ask=0.42, vwap_midpoint=0.41,
            bid_depth=500, ask_depth=100, imbalance_ratio=5.0,
            has_buy_imbalance=True, has_sell_imbalance=False,
            confidence_modifier=0.15, spread=0.02,
            levels_bid=5, levels_ask=2,
        )
        edge, conf = apply_orderbook_signal(0.08, 0.95, analysis, "BUY_YES")
        assert conf <= 1.0


class TestHelpers:
    def test_vwap_empty(self):
        assert _vwap([]) == 0.0

    def test_vwap_single(self):
        assert _vwap([(0.50, 100)]) == 0.50

    def test_vwap_weighted(self):
        result = _vwap([(0.40, 100), (0.30, 200)])
        expected = (0.40 * 100 + 0.30 * 200) / 300
        assert abs(result - expected) < 0.001

    def test_parse_levels_valid(self):
        levels = [[0.40, 100], [0.39, 50]]
        parsed = _parse_levels(levels)
        assert len(parsed) == 2
        assert parsed[0] == (0.40, 100)

    def test_parse_levels_invalid(self):
        levels = [[0.40, 0], [0, 50], ["bad", 10]]
        parsed = _parse_levels(levels)
        assert len(parsed) == 0

    def test_confidence_modifier_balanced(self):
        mod = _compute_confidence_modifier(1.0, 200)
        assert mod == 0.0

    def test_confidence_modifier_buy_heavy(self):
        mod = _compute_confidence_modifier(4.0, 200)
        assert mod > 0

    def test_confidence_modifier_sell_heavy(self):
        mod = _compute_confidence_modifier(0.25, 200)
        assert mod < 0

    def test_confidence_modifier_low_depth(self):
        mod = _compute_confidence_modifier(4.0, 10)
        assert mod == 0.0
