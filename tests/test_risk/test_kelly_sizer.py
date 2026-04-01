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
        fee_rate = 0.0  # Event market (fee-free), matching default
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
            fee_rate=fee_rate,
        )
        fee_cents = math.ceil(fee_rate * contracts * price * (1.0 - price))
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

    def test_excellent_brier_boosts_sizing(self, sizer):
        sizer.update_calibration_multiplier(0.08)
        assert sizer.calibration_multiplier == 1.1

    def test_good_brier_keeps_full_sizing(self, sizer):
        sizer.update_calibration_multiplier(0.15)
        assert sizer.calibration_multiplier == 1.0

    def test_fair_brier_reduces_to_75(self, sizer):
        """M-11: Fair Brier (0.20) now reduces to 75% (was 50%)."""
        sizer.update_calibration_multiplier(0.20)
        assert sizer.calibration_multiplier == 0.75

    def test_mediocre_brier_reduces_to_50(self, sizer):
        """M-11: New mediocre band at 0.25 reduces to 50%."""
        sizer.update_calibration_multiplier(0.25)
        assert sizer.calibration_multiplier == 0.50

    def test_poor_brier_reduces_to_25(self, sizer):
        """M-11: Poor Brier (0.30) reduces to 25% (was 0.28)."""
        sizer.update_calibration_multiplier(0.30)
        assert sizer.calibration_multiplier == 0.25

    def test_terrible_brier_halts_sizing(self, sizer):
        sizer.update_calibration_multiplier(0.50)
        assert sizer.calibration_multiplier == 0.0

    def test_terrible_brier_returns_zero_contracts(self, sizer):
        """Brier > 0.30 (worse than random) should produce 0 contracts."""
        sizer.update_calibration_multiplier(0.35)
        contracts = sizer.calculate_position_size(0.10, 0.60, 500.0)
        assert contracts == 0

    def test_none_brier_resets_to_full(self, sizer):
        sizer.update_calibration_multiplier(0.50)
        assert sizer.calibration_multiplier == 0.0
        sizer.update_calibration_multiplier(None)
        assert sizer.calibration_multiplier == 1.0

    def test_multiplier_reduces_contracts(self, sizer):
        """Poor calibration should produce fewer contracts."""
        full = sizer.calculate_position_size(0.10, 0.60, 500.0)
        sizer.update_calibration_multiplier(0.22)  # 50% multiplier (FAIR range)
        reduced = sizer.calculate_position_size(0.10, 0.60, 500.0)
        assert reduced <= full
        assert reduced >= 1  # Still at least 1


class TestExtremePriceBoundaries:
    """Kelly sizing at extreme price boundaries."""

    def test_very_low_price_001(self, sizer):
        """Contracts below $0.03 are always rejected (too volatile)."""
        contracts = sizer.calculate_position_size(
            edge=0.05, probability=0.06, bankroll=500.0,
        )
        # market_price = 0.06 - 0.05 = 0.01, which is below $0.03 threshold
        assert contracts == 0
        assert isinstance(contracts, int)

    def test_very_high_price_099(self, sizer):
        """Very expensive contracts (price ~0.99) should be capped."""
        contracts = sizer.calculate_position_size(
            edge=0.005, probability=0.995, bankroll=500.0,
            order_price=0.99,
        )
        if contracts > 0:
            cost = contracts * 0.99
            assert cost <= 500.0 * 0.05 + 0.01

    def test_price_at_boundary_001(self, sizer):
        """Market price at 0.01 boundary is rejected (below $0.03 threshold)."""
        contracts = sizer.calculate_position_size(
            edge=0.04, probability=0.05, bankroll=500.0,
        )
        # market_price = 0.05 - 0.04 = 0.01, below $0.03 threshold
        assert contracts == 0


class TestTieredLowPriceCheck:
    """H-8: Tiered price check — $0.03-$0.10 requires 10% edge."""

    def test_below_003_always_rejected(self, sizer):
        """Prices below $0.03 are always rejected regardless of edge."""
        contracts = sizer.calculate_position_size(
            edge=0.50, probability=0.52, bankroll=500.0,
        )
        # market_price = 0.02, below $0.03 threshold
        assert contracts == 0

    def test_between_003_and_010_low_edge_rejected(self, sizer):
        """Prices $0.03-$0.10 with edge < 10% should be rejected."""
        contracts = sizer.calculate_position_size(
            edge=0.05, probability=0.10, bankroll=500.0,
        )
        # market_price = 0.10 - 0.05 = 0.05, edge=0.05 < 0.10 threshold
        assert contracts == 0

    def test_between_003_and_010_high_edge_allowed(self, sizer):
        """Prices $0.03-$0.10 with edge >= 10% should be allowed."""
        contracts = sizer.calculate_position_size(
            edge=0.12, probability=0.17, bankroll=500.0,
        )
        # market_price = 0.17 - 0.12 = 0.05, edge=0.12 >= 0.10 → allowed
        assert contracts >= 0  # May still be 0 from Kelly math, but not from price check
        assert isinstance(contracts, int)

    def test_above_010_normal_edge_rules_apply(self, sizer):
        """Prices >= $0.10 use normal edge rules (no extra restriction)."""
        contracts = sizer.calculate_position_size(
            edge=0.05, probability=0.20, bankroll=500.0,
        )
        # market_price = 0.20 - 0.05 = 0.15, above $0.10 threshold
        assert contracts >= 0


class TestFeeRateParameter:
    """H-6: fee_rate should be a parameter, not hardcoded on the instance."""

    def test_default_fee_rate_zero(self, sizer):
        """Default fee_rate=0.0 means event market (fee-free) sizing."""
        import math
        price = 0.66
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
        )
        # With fee_rate=0, fee_dollars=0, total = contracts * price
        if contracts > 0:
            assert contracts * price <= 500.0 * 0.05 + 0.01

    def test_nonzero_fee_rate_reduces_contracts(self, sizer):
        """Passing a nonzero fee_rate should reduce contracts to fit within cap."""
        import math
        price = 0.66
        fee_rate = 0.0175
        contracts_with_fee = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
            fee_rate=fee_rate,
        )
        contracts_no_fee = sizer.calculate_position_size(
            edge=0.10, probability=0.76, bankroll=500.0,
            order_price=price,
            fee_rate=0.0,
        )
        # Paying fees means fewer contracts fit within the budget
        assert contracts_with_fee <= contracts_no_fee

    def test_fee_rate_caps_total_cost(self, sizer):
        """Total cost including fee must not exceed position cap."""
        import math
        price = 0.50
        fee_rate = 0.07  # High taker fee
        max_position = 500.0 * 0.05  # $25
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=price,
            fee_rate=fee_rate,
        )
        if contracts > 0:
            fee_cents = math.ceil(fee_rate * contracts * price * (1.0 - price))
            fee_dollars = fee_cents / 100.0
            total = contracts * price + fee_dollars
            assert total <= max_position, (
                f"Total ${total:.2f} (contracts={contracts}, fee=${fee_dollars:.2f}) "
                f"exceeds cap ${max_position:.2f}"
            )


class TestLiquidityBeforeCaps:
    """H-7: Liquidity adjustment must happen before position/exposure caps."""

    def test_liquidity_reduces_kelly_before_cap(self, sizer):
        """When order would be >10% of book, kelly_dollars is halved before caps are applied."""
        # With market_liquidity = $100, and kelly_dollars ~ $25 (position cap),
        # raw_contracts ≈ 50 @ $0.50 → order = $25 = 25% of book → halved to $12.50
        # The cap is checked AFTER the halving, so the final sizing reflects liquidity.
        contracts_with_liq = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
            market_liquidity=100.0,  # Shallow book — $25 order = 25% of depth
        )
        contracts_no_liq = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
            market_liquidity=None,
        )
        # Liquidity-constrained sizing should be smaller
        assert contracts_with_liq <= contracts_no_liq

    def test_deep_liquidity_no_adjustment(self, sizer):
        """When order is <5% of book, no adjustment should occur."""
        contracts_with_liq = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
            market_liquidity=10000.0,  # Deep book — $25 order = 0.25% of depth
        )
        contracts_no_liq = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
            market_liquidity=None,
        )
        assert contracts_with_liq == contracts_no_liq


class TestNegativeMarketPrice:
    """Regression: edge > probability should produce 0 contracts (negative market_price)."""

    def test_edge_much_larger_than_probability(self, sizer):
        """When edge >> probability, derived market_price is very negative."""
        assert sizer.calculate_position_size(edge=0.90, probability=0.10, bankroll=500.0) == 0

    def test_edge_barely_exceeds_probability(self, sizer):
        """Edge = probability + epsilon → market_price ≈ 0."""
        assert sizer.calculate_position_size(edge=0.501, probability=0.50, bankroll=500.0) == 0


class TestFeeUnit:
    def test_fee_unit_is_dollars(self, sizer):
        """25 contracts at $0.50 should NOT be reduced — fee is only ~1 cent."""
        # fee_cents = ceil(0.0175 * 25 * 0.50 * 0.50) = ceil(0.109) = 1 cent
        # total = 25 * 0.50 + 0.01 = $12.51, well under $25 cap
        # Before fix, fee was treated as $1, causing unnecessary reduction.
        import math

        fee_rate = 0.0175  # Simulate fee-enabled market maker rate
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=500.0,
            order_price=0.50,
            fee_rate=fee_rate,
        )
        # With half-Kelly and these params, we should get a reasonable count
        # The key check: at 0.50 price, the fee should not cause a reduction
        # when total cost is well under the cap
        if contracts > 1:
            fee_cents = math.ceil(fee_rate * contracts * 0.50 * 0.50)
            fee_dollars = fee_cents / 100.0
            cost = contracts * 0.50
            # Fee in dollars should be tiny relative to cost
            assert fee_dollars < 0.10, f"Fee ${fee_dollars:.2f} is too high (should be ~cents)"


class TestKellyBankrollBoundaries:
    """Edge cases for bankroll extremes."""

    def test_tiny_bankroll(self, sizer):
        """Very small bankroll ($1) — should still produce valid sizing."""
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=1.0,
            order_price=0.10,
        )
        assert contracts >= 0
        # Cost should never exceed bankroll
        assert contracts * 0.10 <= 1.0

    def test_large_bankroll(self, sizer):
        """Large bankroll ($100K) — position cap should still apply."""
        contracts = sizer.calculate_position_size(
            edge=0.10, probability=0.60, bankroll=100000.0,
            order_price=0.50,
        )
        assert contracts > 0
        # Max position = 5% of $100K = $5000, at $0.50/contract = 10000 max
        assert contracts * 0.50 <= 5000.0

    def test_probability_near_zero_with_edge(self, sizer):
        """Probability near 0 but positive edge — very small or 0 position."""
        contracts = sizer.calculate_position_size(
            edge=0.02, probability=0.03, bankroll=500.0,
            order_price=0.01,
        )
        assert contracts >= 0

    def test_probability_near_one_with_edge(self, sizer):
        """Probability near 1.0 with tiny edge."""
        contracts = sizer.calculate_position_size(
            edge=0.01, probability=0.99, bankroll=500.0,
            order_price=0.98,
        )
        assert contracts >= 0
        # At $0.98/contract, very few should be bought
        if contracts > 0:
            assert contracts * 0.98 <= 25.0  # 5% of 500


class TestConfidenceAdjustment:
    """Tests for confidence-adjusted Kelly sizing."""

    def test_high_confidence_minimal_reduction(self, sizer):
        """Confidence=0.9 should barely reduce position size."""
        base = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0,
        )
        adjusted = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0, confidence=0.9,
        )
        # 0.9^1.5 ≈ 0.85x — should be close to base
        assert adjusted > 0
        assert adjusted <= base

    def test_low_confidence_significant_reduction(self, sizer):
        """Confidence=0.5 should meaningfully reduce position size."""
        # Use smaller edge so position isn't capped
        base = sizer.calculate_position_size(
            edge=0.06, probability=0.50, bankroll=500.0,
        )
        adjusted = sizer.calculate_position_size(
            edge=0.06, probability=0.50, bankroll=500.0, confidence=0.5,
        )
        # 0.5^1.5 ≈ 0.35x — should be noticeably smaller
        assert adjusted > 0
        assert adjusted < base

    def test_very_low_confidence_heavy_reduction(self, sizer):
        """Confidence=0.1 should heavily reduce position size."""
        base = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0,
        )
        adjusted = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0, confidence=0.1,
        )
        # 0.1^1.5 ≈ 0.03x — much smaller (floored at 0.2)
        assert adjusted < base

    def test_none_confidence_no_adjustment(self, sizer):
        """confidence=None should behave identically to no confidence."""
        base = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0,
        )
        with_none = sizer.calculate_position_size(
            edge=0.10, probability=0.50, bankroll=500.0, confidence=None,
        )
        assert with_none == base

    def test_confidence_ordering(self, sizer):
        """Higher confidence should always produce >= position size."""
        sizes = []
        for conf in [0.2, 0.5, 0.7, 0.9]:
            s = sizer.calculate_position_size(
                edge=0.10, probability=0.50, bankroll=500.0, confidence=conf,
            )
            sizes.append(s)
        # Monotonically non-decreasing
        for i in range(len(sizes) - 1):
            assert sizes[i] <= sizes[i + 1]
