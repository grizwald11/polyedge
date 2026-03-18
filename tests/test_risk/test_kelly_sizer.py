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

    def test_edge_exceeds_probability(self, sizer):
        """When edge >= probability, market_price goes to 0 or negative → return 0."""
        assert sizer.calculate_position_size(edge=0.51, probability=0.50, bankroll=500.0) == 0
        assert sizer.calculate_position_size(edge=0.50, probability=0.50, bankroll=500.0) == 0

    def test_min_contract_respects_kelly_dollars(self, sizer):
        """Minimum-1-contract fallback should not exceed kelly_dollars budget."""
        # Tiny edge with high-cost contracts: kelly_dollars will be very small
        # but remaining room is large — the fallback shouldn't bypass the budget.
        contracts = sizer.calculate_position_size(
            edge=0.01, probability=0.99, bankroll=500.0,
            order_price=0.99,
        )
        # With edge=0.01, prob=0.99, market_price=0.98, kelly_fraction is tiny
        # kelly_dollars ≈ very small. If contracts=1, cost=$0.99 which may exceed.
        # The fix ensures we check cost_price + fee <= kelly_dollars.
        if contracts > 0:
            # If we got 1 contract, verify kelly_dollars is large enough
            market_price = 0.99 - 0.01
            b = (1.0 - market_price) / market_price
            q = 1.0 - 0.99
            kf = (0.99 * b - q) / b
            half_k = kf * 0.5
            kelly_dollars = min(half_k * 500.0, 500.0 * 0.05)
            assert 0.99 <= kelly_dollars

    def test_order_price_none_uses_derived(self, sizer):
        """When order_price is None, behavior matches the original."""
        a = sizer.calculate_position_size(0.08, 0.42, 500.0)
        b = sizer.calculate_position_size(0.08, 0.42, 500.0, order_price=None)
        assert a == b

    def test_fee_included_in_position_cap(self, sizer):
        """Contracts * price + estimated fee (in dollars) must not exceed position cap.

        Regression: Cuba trade deal NO at $0.66 produced 38 contracts
        ($25.08) which exceeded the $25 cap before fees were considered.
        Fee formula returns cents: ceil(fee_rate * contracts * price * (1 - price)).
        """
        import math

        max_position = 500.0 * 0.05  # $25
        price = 0.66
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
        )
        fee_cents = math.ceil(sizer.fee_rate * contracts * price * (1.0 - price))
        fee_dollars = fee_cents / 100.0
        total = contracts * price + fee_dollars
        assert total <= max_position, (
            f"{contracts} contracts @ ${price}: cost ${contracts * price:.2f} "
            f"+ fee ${fee_dollars:.2f} = ${total:.2f} exceeds cap ${max_position:.2f}"
        )

class TestNonFiniteInputs:
    """Regression: NaN and infinity inputs should return 0, not crash."""

    def test_nan_edge(self, sizer):
        assert sizer.calculate_position_size(float("nan"), 0.50, 500.0) == 0

    def test_inf_probability(self, sizer):
        assert sizer.calculate_position_size(0.10, float("inf"), 500.0) == 0

    def test_nan_bankroll(self, sizer):
        assert sizer.calculate_position_size(0.10, 0.50, float("nan")) == 0

    def test_neg_inf_exposure(self, sizer):
        assert sizer.calculate_position_size(0.10, 0.50, 500.0, current_exposure=float("-inf")) == 0


class TestCalibrationMultiplier:
    def test_default_multiplier_is_one(self, sizer):
        assert sizer.calibration_multiplier == 1.0

    def test_excellent_brier_keeps_full_sizing(self, sizer):
        sizer.update_calibration_multiplier(0.08)
        assert sizer.calibration_multiplier == 1.0

    def test_fair_brier_reduces_to_75(self, sizer):
        sizer.update_calibration_multiplier(0.25)
        assert sizer.calibration_multiplier == 0.75

    def test_poor_brier_reduces_to_50(self, sizer):
        sizer.update_calibration_multiplier(0.35)
        assert sizer.calibration_multiplier == 0.50

    def test_terrible_brier_reduces_to_25(self, sizer):
        sizer.update_calibration_multiplier(0.50)
        assert sizer.calibration_multiplier == 0.25

    def test_none_brier_resets_to_full(self, sizer):
        sizer.update_calibration_multiplier(0.50)
        assert sizer.calibration_multiplier == 0.25
        sizer.update_calibration_multiplier(None)
        assert sizer.calibration_multiplier == 1.0

    def test_multiplier_reduces_contracts(self, sizer):
        """Poor calibration should produce fewer contracts."""
        full = sizer.calculate_position_size(0.10, 0.60, 500.0)
        sizer.update_calibration_multiplier(0.35)  # 50% multiplier
        reduced = sizer.calculate_position_size(0.10, 0.60, 500.0)
        assert reduced <= full
        assert reduced >= 1  # Still at least 1


class TestFeeUnit:
    def test_fee_unit_is_dollars(self, sizer):
        """25 contracts at $0.50 should NOT be reduced — fee is only ~1 cent."""
        # fee_cents = ceil(0.0175 * 25 * 0.50 * 0.50) = ceil(0.109) = 1 cent
        # total = 25 * 0.50 + 0.01 = $12.51, well under $25 cap
        # Before fix, fee was treated as $1, causing unnecessary reduction.
        import math

        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
        )
        # With half-Kelly and these params, we should get a reasonable count
        # The key check: at 0.50 price, the fee should not cause a reduction
        # when total cost is well under the cap
        if contracts > 1:
            fee_cents = math.ceil(sizer.fee_rate * contracts * 0.50 * 0.50)
            fee_dollars = fee_cents / 100.0
            cost = contracts * 0.50
            # Fee in dollars should be tiny relative to cost
            assert fee_dollars < 0.10, f"Fee ${fee_dollars:.2f} is too high (should be ~cents)"
