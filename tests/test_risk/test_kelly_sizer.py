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

    def test_order_price_respected_for_cap(self, sizer):
        """When order_price is higher than derived market_price, contracts
        should be reduced so that contracts * order_price <= position cap."""
        # Obvious NO scenario: probability=0.97, edge=0.03
        # Derived market_price = 0.97 - 0.03 = 0.94
        # Actual order price = 0.97 (the NO price we pay)
        # Max position = $25, so max contracts = floor(25 / 0.97) = 25
        contracts = sizer.calculate_position_size(
            edge=0.03, probability=0.97, bankroll=500.0,
            order_price=0.97,
        )
        cost = contracts * 0.97
        max_position = 500.0 * 0.05  # $25
        assert cost <= max_position, (
            f"Cost ${cost:.2f} exceeds position limit ${max_position:.2f}"
        )

    def test_order_price_high_no_price(self, sizer):
        """Various high NO prices should never breach the position cap."""
        max_position = 500.0 * 0.05  # $25
        for no_price in [0.95, 0.96, 0.97, 0.98, 0.99]:
            edge = 1.0 - no_price  # yes_price
            contracts = sizer.calculate_position_size(
                edge=edge, probability=no_price, bankroll=500.0,
                order_price=no_price,
            )
            cost = contracts * no_price
            assert cost <= max_position, (
                f"NO@${no_price}: {contracts} contracts cost ${cost:.2f} > ${max_position:.2f}"
            )

    def test_order_price_none_uses_derived(self, sizer):
        """When order_price is None, behavior matches the original."""
        a = sizer.calculate_position_size(0.08, 0.42, 500.0)
        b = sizer.calculate_position_size(0.08, 0.42, 500.0, order_price=None)
        assert a == b

    def test_fee_included_in_position_cap(self, sizer):
        """Contracts * price + estimated fee must not exceed position cap.

        Regression: Cuba trade deal NO at $0.66 produced 38 contracts
        ($25.08) which exceeded the $25 cap before fees were considered.
        Fee formula: ceil(0.07 * contracts * price * (1 - price)).
        """
        import math

        max_position = 500.0 * 0.05  # $25
        price = 0.66
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
        )
        fee = math.ceil(0.07 * contracts * price * (1.0 - price))
        total = contracts * price + fee
        assert total <= max_position, (
            f"{contracts} contracts @ ${price}: cost ${contracts * price:.2f} "
            f"+ fee ${fee} = ${total:.2f} exceeds cap ${max_position:.2f}"
        )
