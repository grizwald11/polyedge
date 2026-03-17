"""Tests for Kelly position sizer."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.risk.kelly_sizer import KellySizer


@pytest.fixture
def sizer(settings) -> KellySizer:
    return KellySizer(settings)


class TestCalculatePositionSize:
    def test_basic_sizing(self, sizer):
        contracts = sizer.calculate_position_size(
            edge=0.08, probability=0.42, bankroll=500.0,
        )
        assert contracts > 0
        assert isinstance(contracts, int)

    def test_zero_edge(self, sizer):
        assert sizer.calculate_position_size(0.0, 0.50, 500.0) == 0

    def test_negative_edge(self, sizer):
        assert sizer.calculate_position_size(-0.05, 0.50, 500.0) == 0

    def test_zero_bankroll(self, sizer):
        assert sizer.calculate_position_size(0.10, 0.50, 0.0) == 0

    def test_extreme_probability(self, sizer):
        assert sizer.calculate_position_size(0.05, 0.0, 500.0) == 0
        assert sizer.calculate_position_size(0.05, 1.0, 500.0) == 0

    def test_position_cap(self, sizer):
        """Large edge shouldn't exceed max position percentage."""
        contracts = sizer.calculate_position_size(
            edge=0.30, probability=0.80, bankroll=500.0,
        )
        # Max position = 500 * 0.05 = $25
        # Market price ≈ 0.50, so max contracts ≈ 50
        cost = contracts * 0.50
        assert cost <= 500 * 0.05 + 0.01  # Small tolerance

    def test_exposure_cap(self, sizer):
        """Should reduce size when near exposure limit."""
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0,
            current_exposure=195.0,  # 39% of 500, limit is 40%
        )
        # Only $5 of room left
        # price = 0.50 - 0.10 = 0.40, so max 12 contracts
        assert contracts <= 13  # $5 / $0.40 ≈ 12

    def test_exposure_full(self, sizer):
        """Returns 0 when exposure limit reached."""
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0,
            current_exposure=200.0,  # 40% = at limit
        )
        assert contracts == 0

    def test_minimum_one_contract(self, sizer):
        """Small edge should still produce at least 1 contract."""
        contracts = sizer.calculate_position_size(
            edge=0.05, probability=0.55, bankroll=500.0,
        )
        assert contracts >= 1

    def test_higher_edge_more_contracts(self, sizer):
        low = sizer.calculate_position_size(edge=0.05, probability=0.55, bankroll=500.0)
        high = sizer.calculate_position_size(edge=0.15, probability=0.65, bankroll=500.0)
        assert high >= low

    def test_half_kelly_applied(self, settings):
        """Half Kelly should produce smaller positions than full Kelly."""
        full_sizer = KellySizer(settings)
        settings_half = Settings()
        settings_half.trading.kelly_fraction = 0.25
        quarter_sizer = KellySizer(settings_half)

        full = full_sizer.calculate_position_size(0.10, 0.60, 500.0)
        quarter = quarter_sizer.calculate_position_size(0.10, 0.60, 500.0)

        assert quarter <= full
