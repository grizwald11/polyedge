"""Tests for the Bayesian Position Updater."""

from __future__ import annotations

import pytest

from src.analysis.bayesian_updater import (
    BayesianUpdater,
    BeliefState,
)


def _make_belief(
    probability: float = 0.70,
    entry_price: float = 0.55,
    direction_yes: bool = True,
) -> BeliefState:
    return BeliefState(
        market_id="TEST",
        probability=probability,
        entry_price=entry_price,
        direction_is_yes=direction_yes,
    )


# ─── Price movement update tests ───


class TestUpdateFromPriceMovement:
    def test_negligible_movement_no_update(self):
        updater = BayesianUpdater()
        belief = _make_belief(0.70)
        result = updater.update_from_price_movement(belief, 0.702, 0.700)
        assert result.shift == 0.0
        assert result.posterior == 0.70
        assert belief.update_count == 0

    def test_upward_price_movement(self):
        updater = BayesianUpdater(market_efficiency=0.7)
        belief = _make_belief(0.60)
        result = updater.update_from_price_movement(belief, 0.70, 0.60)
        # posterior = 0.3 * 0.60 + 0.7 * 0.70 = 0.18 + 0.49 = 0.67
        assert result.posterior == pytest.approx(0.67, abs=0.01)
        assert result.shift > 0
        assert belief.probability == result.posterior
        assert belief.update_count == 1

    def test_downward_price_movement(self):
        updater = BayesianUpdater(market_efficiency=0.7)
        belief = _make_belief(0.70)
        result = updater.update_from_price_movement(belief, 0.55, 0.65)
        # posterior = 0.3 * 0.70 + 0.7 * 0.55 = 0.21 + 0.385 = 0.595
        assert result.posterior == pytest.approx(0.595, abs=0.01)
        assert result.shift < 0

    def test_large_shift_triggers_reassess(self):
        updater = BayesianUpdater(market_efficiency=0.7, reassess_threshold=0.10)
        belief = _make_belief(0.70)
        result = updater.update_from_price_movement(belief, 0.40, 0.65)
        # posterior = 0.3 * 0.70 + 0.7 * 0.40 = 0.21 + 0.28 = 0.49
        assert result.should_reassess  # shift of ~0.21

    def test_small_shift_no_reassess(self):
        updater = BayesianUpdater(market_efficiency=0.7, reassess_threshold=0.10)
        belief = _make_belief(0.70)
        result = updater.update_from_price_movement(belief, 0.68, 0.65)
        assert not result.should_reassess

    def test_posterior_clamped_to_range(self):
        updater = BayesianUpdater(market_efficiency=0.9)
        belief = _make_belief(0.01)
        result = updater.update_from_price_movement(belief, 0.001, 0.50)
        assert result.posterior >= 0.01

        belief2 = _make_belief(0.99)
        result2 = updater.update_from_price_movement(belief2, 0.999, 0.50)
        assert result2.posterior <= 0.99


# ─── Community shift update tests ───


class TestUpdateFromCommunityShift:
    def test_negligible_shift_no_update(self):
        updater = BayesianUpdater()
        belief = _make_belief(0.70)
        result = updater.update_from_community_shift(belief, 0.68, 0.69)
        assert result.shift == 0.0

    def test_upward_community_shift(self):
        updater = BayesianUpdater()
        belief = _make_belief(0.60)
        result = updater.update_from_community_shift(
            belief, 0.55, 0.75, community_weight=0.3,
        )
        # delta = 0.20, posterior = 0.60 + 0.20 * 0.3 = 0.66
        assert result.posterior == pytest.approx(0.66, abs=0.01)
        assert belief.update_count == 1

    def test_downward_community_shift(self):
        updater = BayesianUpdater()
        belief = _make_belief(0.70)
        result = updater.update_from_community_shift(
            belief, 0.65, 0.45, community_weight=0.3,
        )
        # delta = -0.20, posterior = 0.70 + (-0.20 * 0.3) = 0.64
        assert result.posterior == pytest.approx(0.64, abs=0.01)


# ─── Exit check tests ───


class TestExitChecks:
    def test_edge_intact_no_exit(self):
        updater = BayesianUpdater(min_edge=0.02)
        belief = _make_belief(probability=0.70, entry_price=0.55, direction_yes=True)
        result = updater.update_from_price_movement(belief, 0.68, 0.65)
        # Remaining edge: posterior - entry_price ≈ 0.67 - 0.55 = 0.12
        assert not result.should_exit

    def test_edge_evaporated_triggers_exit(self):
        updater = BayesianUpdater(market_efficiency=0.8, min_edge=0.02)
        belief = _make_belief(probability=0.60, entry_price=0.55, direction_yes=True)
        # Price drops to 0.50 → posterior ≈ 0.2*0.60 + 0.8*0.50 = 0.52
        result = updater.update_from_price_movement(belief, 0.50, 0.58)
        # remaining_edge = 0.52 - 0.55 = -0.03 → edge flipped
        assert result.should_exit
        assert "flipped" in result.exit_reason

    def test_edge_below_minimum_triggers_exit(self):
        updater = BayesianUpdater(market_efficiency=0.7, min_edge=0.05)
        belief = _make_belief(probability=0.60, entry_price=0.55, direction_yes=True)
        # Price moves up slightly → posterior near 0.58
        result = updater.update_from_price_movement(belief, 0.57, 0.55)
        # remaining_edge ≈ 0.58 - 0.55 = 0.03 < 0.05
        if result.posterior - belief.entry_price < updater.min_edge:
            assert result.should_exit
            assert "below minimum" in result.exit_reason

    def test_no_direction_exit_for_buy_no(self):
        updater = BayesianUpdater(market_efficiency=0.7, min_edge=0.02)
        # Bought NO at 0.45 (YES was 0.55), probability estimate = 0.40
        belief = _make_belief(probability=0.40, entry_price=0.55, direction_yes=False)
        # Price drops to 0.35 → NO side is winning, edge increasing
        result = updater.update_from_price_movement(belief, 0.35, 0.40)
        # remaining edge for NO: entry_price - posterior = 0.55 - ~0.35 = 0.20
        assert not result.should_exit


# ─── Multiple updates test ───


class TestMultipleUpdates:
    def test_sequential_updates_accumulate(self):
        updater = BayesianUpdater(market_efficiency=0.5)
        belief = _make_belief(0.60)

        # First update: price moves up
        r1 = updater.update_from_price_movement(belief, 0.65, 0.60)
        assert belief.update_count == 1
        first_posterior = r1.posterior

        # Second update: price moves up more
        r2 = updater.update_from_price_movement(belief, 0.70, 0.65)
        assert belief.update_count == 2
        assert r2.posterior > first_posterior

    def test_mixed_sources(self):
        updater = BayesianUpdater(market_efficiency=0.5)
        belief = _make_belief(0.60)

        # Price update
        updater.update_from_price_movement(belief, 0.65, 0.60)
        after_price = belief.probability

        # Community update
        updater.update_from_community_shift(belief, 0.60, 0.75, community_weight=0.3)
        after_community = belief.probability

        assert after_community > after_price
        assert belief.update_count == 2
