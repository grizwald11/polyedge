"""Monte Carlo risk simulator — validates Kelly fraction choice.

Runs thousands of random trade sequences using historical win rate and
average win/loss sizes to estimate drawdown distributions, ruin probability,
and expected time to double bankroll. Used to sanity-check the Kelly fraction
before deploying real capital.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MonteCarloResult:
    """Results from a Monte Carlo simulation run."""

    simulations: int
    trades_per_sim: int
    starting_bankroll: float
    win_rate: float
    avg_win: float
    avg_loss: float
    kelly_fraction: float
    # Results
    median_final_bankroll: float
    mean_final_bankroll: float
    drawdown_95th: float  # 95th percentile max drawdown (as fraction, e.g. 0.30 = 30%)
    drawdown_99th: float  # 99th percentile max drawdown
    probability_of_ruin: float  # Fraction of sims that hit <10% of start
    time_to_double_median: Optional[int]  # Median trades to 2x (None if >50% never reach)
    percentile_10: float  # 10th percentile final bankroll
    percentile_90: float  # 90th percentile final bankroll


class MonteCarloSimulator:
    """Run Monte Carlo simulations of trade sequences.

    Uses vectorized numpy operations for performance. All simulations
    within a single run() call are computed in parallel across the
    simulation axis.
    """

    # Cap individual position at this fraction of current bankroll
    MAX_POSITION_FRACTION = 0.05

    def __init__(self, seed: int = 42):
        self.rng = np.random.default_rng(seed)

    def run(
        self,
        starting_bankroll: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        kelly_fraction: float,
        num_simulations: int = 10_000,
        trades_per_sim: int = 200,
    ) -> MonteCarloResult:
        """Run Monte Carlo simulation.

        For each simulation:
        1. Start with starting_bankroll.
        2. For each trade, determine win/loss randomly based on win_rate.
        3. Position size = kelly_fraction * edge * current_bankroll,
           capped at MAX_POSITION_FRACTION * current_bankroll.
        4. Win: bankroll += position_size * avg_win
           Loss: bankroll -= position_size * avg_loss
        5. Track max drawdown, ruin, and time to double.

        Args:
            starting_bankroll: Initial bankroll in dollars.
            win_rate: Probability of winning each trade (0-1).
            avg_win: Average win as a multiple of position size (e.g. 0.5 = 50% return).
            avg_loss: Average loss as a multiple of position size (e.g. 0.3 = 30% loss).
            kelly_fraction: Fraction of Kelly to use (e.g. 0.5 = half-Kelly).
            num_simulations: Number of simulation paths.
            trades_per_sim: Number of trades per simulation path.

        Returns:
            MonteCarloResult with aggregated statistics.
        """
        if not (0.0 <= win_rate <= 1.0):
            raise ValueError(f"win_rate must be in [0, 1], got {win_rate}")
        if starting_bankroll <= 0:
            raise ValueError(f"starting_bankroll must be positive, got {starting_bankroll}")
        if avg_win < 0 or avg_loss < 0:
            raise ValueError("avg_win and avg_loss must be non-negative")
        if kelly_fraction < 0:
            raise ValueError(f"kelly_fraction must be non-negative, got {kelly_fraction}")

        # Calculate edge: expected value per dollar risked
        edge = win_rate * avg_win - (1.0 - win_rate) * avg_loss

        # Generate all random outcomes at once: shape (num_simulations, trades_per_sim)
        outcomes = self.rng.random((num_simulations, trades_per_sim))
        wins = outcomes < win_rate  # True where trade is a win

        # Simulate trade-by-trade (must be sequential due to bankroll dependency)
        bankrolls = np.full(num_simulations, starting_bankroll, dtype=np.float64)
        peak_bankrolls = bankrolls.copy()
        max_drawdowns = np.zeros(num_simulations, dtype=np.float64)
        ruined = np.zeros(num_simulations, dtype=bool)
        time_to_double = np.full(num_simulations, -1, dtype=np.int64)  # -1 = never
        ruin_threshold = starting_bankroll * 0.10
        double_threshold = starting_bankroll * 2.0

        for t in range(trades_per_sim):
            # Position size: kelly_fraction * edge * bankroll, capped
            # Only size positively when edge > 0; otherwise no trade
            if edge > 0:
                position_sizes = kelly_fraction * edge * bankrolls
                max_positions = self.MAX_POSITION_FRACTION * bankrolls
                position_sizes = np.minimum(position_sizes, max_positions)
                # Don't trade if already ruined
                position_sizes = np.where(bankrolls > ruin_threshold, position_sizes, 0.0)
            else:
                position_sizes = np.zeros(num_simulations)

            # Apply outcomes
            pnl = np.where(wins[:, t], position_sizes * avg_win, -position_sizes * avg_loss)
            bankrolls = bankrolls + pnl

            # Floor at zero (can't go negative)
            bankrolls = np.maximum(bankrolls, 0.0)

            # Track peaks and drawdowns
            peak_bankrolls = np.maximum(peak_bankrolls, bankrolls)
            current_drawdowns = np.where(
                peak_bankrolls > 0,
                (peak_bankrolls - bankrolls) / peak_bankrolls,
                0.0,
            )
            max_drawdowns = np.maximum(max_drawdowns, current_drawdowns)

            # Track ruin
            ruined |= bankrolls < ruin_threshold

            # Track time to double (first time only)
            just_doubled = (bankrolls >= double_threshold) & (time_to_double == -1)
            time_to_double = np.where(just_doubled, t + 1, time_to_double)

        # Compute statistics
        final_bankrolls = bankrolls

        # Drawdown percentiles
        drawdown_95th = float(np.percentile(max_drawdowns, 95))
        drawdown_99th = float(np.percentile(max_drawdowns, 99))

        # Probability of ruin
        probability_of_ruin = float(np.mean(ruined))

        # Time to double: median of sims that actually doubled
        doubled_mask = time_to_double > 0
        if np.sum(doubled_mask) > num_simulations * 0.5:
            time_to_double_median: Optional[int] = int(np.median(time_to_double[doubled_mask]))
        else:
            time_to_double_median = None

        result = MonteCarloResult(
            simulations=num_simulations,
            trades_per_sim=trades_per_sim,
            starting_bankroll=starting_bankroll,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            kelly_fraction=kelly_fraction,
            median_final_bankroll=float(np.median(final_bankrolls)),
            mean_final_bankroll=float(np.mean(final_bankrolls)),
            drawdown_95th=drawdown_95th,
            drawdown_99th=drawdown_99th,
            probability_of_ruin=probability_of_ruin,
            time_to_double_median=time_to_double_median,
            percentile_10=float(np.percentile(final_bankrolls, 10)),
            percentile_90=float(np.percentile(final_bankrolls, 90)),
        )

        logger.info(
            f"Monte Carlo complete: {num_simulations} sims x {trades_per_sim} trades | "
            f"kelly={kelly_fraction:.2f}, edge={edge:.3f}, wr={win_rate:.1%} | "
            f"median=${result.median_final_bankroll:.2f}, "
            f"dd95={result.drawdown_95th:.1%}, dd99={result.drawdown_99th:.1%}, "
            f"ruin={result.probability_of_ruin:.1%}"
        )

        return result

    def compare_kelly_fractions(
        self,
        starting_bankroll: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        fractions: list[float],
        num_simulations: int = 10_000,
        trades_per_sim: int = 200,
    ) -> list[MonteCarloResult]:
        """Run simulations across multiple Kelly fractions for comparison.

        Uses the same random seed base for each fraction so differences
        are due to the fraction, not randomness.

        Args:
            fractions: List of Kelly fractions to test (e.g. [0.25, 0.5, 0.75, 1.0]).

        Returns:
            List of MonteCarloResult, one per fraction, in same order as input.
        """
        # Save current RNG state and reseed for each fraction from a known base
        base_seed = self.rng.integers(0, 2**31)
        results = []

        for fraction in fractions:
            self.rng = np.random.default_rng(int(base_seed))
            result = self.run(
                starting_bankroll=starting_bankroll,
                win_rate=win_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                kelly_fraction=fraction,
                num_simulations=num_simulations,
                trades_per_sim=trades_per_sim,
            )
            results.append(result)

        # Restore RNG to avoid side effects
        self.rng = np.random.default_rng(int(base_seed))

        logger.info(
            f"Kelly comparison: fractions={fractions} | "
            f"medians=[{', '.join(f'${r.median_final_bankroll:.0f}' for r in results)}] | "
            f"ruin=[{', '.join(f'{r.probability_of_ruin:.1%}' for r in results)}]"
        )

        return results
