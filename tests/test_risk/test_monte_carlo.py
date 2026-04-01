"""Tests for Monte Carlo risk simulator."""

from __future__ import annotations

import pytest

from src.risk.monte_carlo import MonteCarloResult, MonteCarloSimulator


@pytest.fixture
def sim() -> MonteCarloSimulator:
    return MonteCarloSimulator(seed=42)


# --- Basic operation ---


def test_basic_simulation_runs(sim: MonteCarloSimulator) -> None:
    """Simulation completes without error and returns valid result."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.60,
        avg_win=0.50,
        avg_loss=0.40,
        kelly_fraction=0.5,
        num_simulations=100,
        trades_per_sim=50,
    )
    assert isinstance(result, MonteCarloResult)
    assert result.simulations == 100
    assert result.trades_per_sim == 50
    assert result.starting_bankroll == 1000.0
    assert result.median_final_bankroll > 0
    assert result.mean_final_bankroll > 0
    assert 0.0 <= result.probability_of_ruin <= 1.0
    assert 0.0 <= result.drawdown_95th <= 1.0
    assert 0.0 <= result.drawdown_99th <= 1.0


def test_deterministic_seed() -> None:
    """Same seed produces identical results."""
    kwargs = dict(
        starting_bankroll=1000.0,
        win_rate=0.60,
        avg_win=0.50,
        avg_loss=0.40,
        kelly_fraction=0.5,
        num_simulations=500,
        trades_per_sim=100,
    )
    r1 = MonteCarloSimulator(seed=99).run(**kwargs)
    r2 = MonteCarloSimulator(seed=99).run(**kwargs)
    assert r1.median_final_bankroll == r2.median_final_bankroll
    assert r1.drawdown_95th == r2.drawdown_95th
    assert r1.probability_of_ruin == r2.probability_of_ruin


def test_different_seeds_differ() -> None:
    """Different seeds produce different results (with high probability)."""
    kwargs = dict(
        starting_bankroll=1000.0,
        win_rate=0.55,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=1000,
        trades_per_sim=100,
    )
    r1 = MonteCarloSimulator(seed=1).run(**kwargs)
    r2 = MonteCarloSimulator(seed=2).run(**kwargs)
    # Very unlikely for both mean and median to be exactly equal with different seeds
    assert (
        r1.mean_final_bankroll != r2.mean_final_bankroll
        or r1.median_final_bankroll != r2.median_final_bankroll
    )


# --- Win rate effects ---


def test_higher_win_rate_better_outcomes() -> None:
    """Higher win rate produces better median final bankroll."""
    common = dict(
        starting_bankroll=1000.0,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=2000,
        trades_per_sim=100,
    )
    low = MonteCarloSimulator(seed=42).run(win_rate=0.50, **common)
    high = MonteCarloSimulator(seed=42).run(win_rate=0.65, **common)
    assert high.median_final_bankroll > low.median_final_bankroll


def test_higher_win_rate_less_ruin() -> None:
    """Higher win rate reduces probability of ruin."""
    common = dict(
        starting_bankroll=1000.0,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=2000,
        trades_per_sim=200,
    )
    low = MonteCarloSimulator(seed=42).run(win_rate=0.45, **common)
    high = MonteCarloSimulator(seed=42).run(win_rate=0.65, **common)
    assert high.probability_of_ruin <= low.probability_of_ruin


# --- Kelly fraction effects ---


def test_higher_kelly_more_variance() -> None:
    """Larger Kelly fraction produces wider spread between p10 and p90."""
    common = dict(
        starting_bankroll=1000.0,
        win_rate=0.60,
        avg_win=0.50,
        avg_loss=0.40,
        num_simulations=2000,
        trades_per_sim=100,
    )
    small = MonteCarloSimulator(seed=42).run(kelly_fraction=0.25, **common)
    large = MonteCarloSimulator(seed=42).run(kelly_fraction=1.0, **common)
    spread_small = small.percentile_90 - small.percentile_10
    spread_large = large.percentile_90 - large.percentile_10
    assert spread_large > spread_small


def test_higher_kelly_more_ruin() -> None:
    """Larger Kelly fraction increases probability of ruin."""
    common = dict(
        starting_bankroll=1000.0,
        win_rate=0.55,
        avg_win=0.60,
        avg_loss=0.50,
        num_simulations=3000,
        trades_per_sim=200,
    )
    conservative = MonteCarloSimulator(seed=42).run(kelly_fraction=0.25, **common)
    aggressive = MonteCarloSimulator(seed=42).run(kelly_fraction=1.5, **common)
    assert aggressive.probability_of_ruin >= conservative.probability_of_ruin


def test_compare_kelly_fractions(sim: MonteCarloSimulator) -> None:
    """compare_kelly_fractions returns one result per fraction."""
    fractions = [0.25, 0.5, 0.75, 1.0]
    results = sim.compare_kelly_fractions(
        starting_bankroll=1000.0,
        win_rate=0.60,
        avg_win=0.50,
        avg_loss=0.40,
        fractions=fractions,
        num_simulations=500,
        trades_per_sim=50,
    )
    assert len(results) == len(fractions)
    for r, f in zip(results, fractions):
        assert r.kelly_fraction == f
        assert r.simulations == 500


# --- Drawdown ordering ---


def test_drawdown_percentiles_ordered(sim: MonteCarloSimulator) -> None:
    """99th percentile drawdown >= 95th percentile drawdown."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.55,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=5000,
        trades_per_sim=200,
    )
    assert result.drawdown_99th >= result.drawdown_95th


# --- Edge cases ---


def test_zero_win_rate(sim: MonteCarloSimulator) -> None:
    """0% win rate: all sims should lose money. Edge is negative so no trades."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.0,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=100,
        trades_per_sim=50,
    )
    # With 0% win rate, edge = -0.50, so no positions are taken.
    # Bankroll should remain at starting value.
    assert result.median_final_bankroll == pytest.approx(1000.0)
    assert result.probability_of_ruin == 0.0


def test_100_win_rate(sim: MonteCarloSimulator) -> None:
    """100% win rate: bankroll should grow, no ruin."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=1.0,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=100,
        trades_per_sim=50,
    )
    assert result.median_final_bankroll > 1000.0
    assert result.probability_of_ruin == 0.0
    assert result.drawdown_95th == 0.0  # Never goes down


def test_zero_kelly_fraction(sim: MonteCarloSimulator) -> None:
    """Kelly fraction of 0 means no trading: bankroll unchanged."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.60,
        avg_win=0.50,
        avg_loss=0.40,
        kelly_fraction=0.0,
        num_simulations=100,
        trades_per_sim=50,
    )
    assert result.median_final_bankroll == pytest.approx(1000.0)
    assert result.probability_of_ruin == 0.0


def test_time_to_double_with_strong_edge(sim: MonteCarloSimulator) -> None:
    """With a strong edge, most sims should double. time_to_double should not be None."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.70,
        avg_win=0.60,
        avg_loss=0.30,
        kelly_fraction=0.5,
        num_simulations=1000,
        trades_per_sim=200,
    )
    assert result.time_to_double_median is not None
    assert result.time_to_double_median > 0
    assert result.time_to_double_median < 200  # Should double well before 200 trades


def test_time_to_double_none_with_no_edge(sim: MonteCarloSimulator) -> None:
    """With negative edge and no trading, time_to_double should be None."""
    result = sim.run(
        starting_bankroll=1000.0,
        win_rate=0.40,
        avg_win=0.50,
        avg_loss=0.50,
        kelly_fraction=0.5,
        num_simulations=100,
        trades_per_sim=50,
    )
    # Edge = 0.4*0.5 - 0.6*0.5 = -0.10, so no positions taken, bankroll stays flat
    assert result.time_to_double_median is None


# --- Input validation ---


def test_invalid_win_rate(sim: MonteCarloSimulator) -> None:
    """Win rate outside [0, 1] raises ValueError."""
    with pytest.raises(ValueError, match="win_rate"):
        sim.run(1000.0, win_rate=1.5, avg_win=0.5, avg_loss=0.5, kelly_fraction=0.5)
    with pytest.raises(ValueError, match="win_rate"):
        sim.run(1000.0, win_rate=-0.1, avg_win=0.5, avg_loss=0.5, kelly_fraction=0.5)


def test_invalid_starting_bankroll(sim: MonteCarloSimulator) -> None:
    """Non-positive starting bankroll raises ValueError."""
    with pytest.raises(ValueError, match="starting_bankroll"):
        sim.run(0.0, win_rate=0.5, avg_win=0.5, avg_loss=0.5, kelly_fraction=0.5)
    with pytest.raises(ValueError, match="starting_bankroll"):
        sim.run(-100.0, win_rate=0.5, avg_win=0.5, avg_loss=0.5, kelly_fraction=0.5)


def test_negative_kelly_raises(sim: MonteCarloSimulator) -> None:
    """Negative Kelly fraction raises ValueError."""
    with pytest.raises(ValueError, match="kelly_fraction"):
        sim.run(1000.0, win_rate=0.5, avg_win=0.5, avg_loss=0.5, kelly_fraction=-0.1)
