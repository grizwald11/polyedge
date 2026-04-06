"""Parameter replay backtest — test different settings against historical signals.

Usage:
    python -m scripts.parameter_replay [--db data/markets.db] [--days 30]

Replays historical signals through the Kelly sizer and risk checks with
different parameter combinations, using actual market outcomes to simulate
what the portfolio would have looked like under each configuration.

This directly answers: "Would changing min_edge from 0.05 to 0.07 have
improved returns?" — without needing to re-run Claude.
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings
from src.storage.database import Database


@dataclass
class ParameterSet:
    """A set of parameters to test."""

    kelly_fraction: float = 0.25
    min_edge_ai: float = 0.05
    min_edge_news: float = 0.04
    min_confidence: float = 0.60
    max_position_pct: float = 0.05
    max_total_exposure_pct: float = 0.40
    stop_loss_pct: float = 0.20

    def label(self) -> str:
        return (
            f"kelly={self.kelly_fraction:.2f} "
            f"edge_ai={self.min_edge_ai:.2f} "
            f"edge_news={self.min_edge_news:.2f} "
            f"conf={self.min_confidence:.2f} "
            f"pos={self.max_position_pct:.0%} "
            f"stop={self.stop_loss_pct:.0%}"
        )


@dataclass
class ReplayResult:
    """Result of replaying signals through one parameter set."""

    params: ParameterSet
    total_signals: int = 0
    acted_signals: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_position_size: float = 0.0
    sharpe_ratio: float | None = None
    is_oos: bool = False  # True = out-of-sample result


def _simple_kelly_size(
    edge: float,
    probability: float,
    bankroll: float,
    current_exposure: float,
    params: ParameterSet,
) -> float:
    """Simplified Kelly sizing for replay (mirrors KellySizer logic)."""
    if edge <= 0 or probability <= 0 or probability >= 1 or bankroll <= 0:
        return 0.0

    market_price = probability - edge
    if market_price <= 0 or market_price >= 1:
        return 0.0

    # Raw Kelly
    b = (1.0 - market_price) / market_price
    q = 1.0 - probability
    kelly_f = (probability * b - q) / b
    if kelly_f <= 0:
        return 0.0

    # Apply Kelly fraction
    half_kelly = kelly_f * params.kelly_fraction
    kelly_dollars = half_kelly * bankroll

    # Position cap
    max_position = bankroll * params.max_position_pct
    kelly_dollars = min(kelly_dollars, max_position)

    # Exposure cap
    max_total = bankroll * params.max_total_exposure_pct
    remaining = max_total - current_exposure
    if remaining <= 0:
        return 0.0
    kelly_dollars = min(kelly_dollars, remaining)

    return kelly_dollars


def replay_signals(
    signals: list[dict],
    outcomes: dict[str, int],
    params: ParameterSet,
    bankroll: float = 500.0,
) -> ReplayResult:
    """Replay historical signals through a parameter set.

    Args:
        signals: List of signal dicts from DB (sorted by timestamp)
        outcomes: Dict of market_id -> actual_outcome (1=YES, 0=NO)
        params: Parameter set to test
        bankroll: Starting bankroll

    Returns:
        ReplayResult with simulated performance
    """
    result = ReplayResult(params=params, total_signals=len(signals))

    equity = bankroll
    peak_equity = bankroll
    max_dd = 0.0
    positions: dict[str, dict] = {}  # market_id -> {size, entry_price, direction}
    exposure = 0.0
    wins: list[float] = []
    losses: list[float] = []
    pnl_series: list[float] = []
    position_sizes: list[float] = []

    for sig in signals:
        strategy = sig.get("strategy", "")
        edge = sig.get("edge", 0)
        confidence = sig.get("confidence", 0)
        probability = sig.get("probability_estimate", 0)
        market_price = sig.get("market_price", 0)
        market_id = sig.get("market_id", "")
        direction = sig.get("direction", "")

        # Gate: minimum edge by strategy
        if "news" in strategy:
            min_edge = params.min_edge_news
        else:
            min_edge = params.min_edge_ai

        if abs(edge) < min_edge:
            continue

        # Gate: minimum confidence
        if confidence < params.min_confidence:
            continue

        # Gate: already have position in this market
        if market_id in positions:
            continue

        # Gate: need outcome to evaluate
        if market_id not in outcomes:
            continue

        # Size the position
        size_dollars = _simple_kelly_size(
            abs(edge), probability, equity, exposure, params,
        )
        if size_dollars <= 0:
            continue

        # Confidence scaling
        if 0 < confidence <= 1:
            size_dollars *= max(0.2, confidence ** 1.2)

        cost_price = market_price if market_price > 0 else probability - edge
        if cost_price <= 0 or cost_price >= 1:
            continue

        contracts = int(size_dollars / cost_price)
        if contracts <= 0:
            continue

        cost = contracts * cost_price
        result.acted_signals += 1
        position_sizes.append(cost)

        # Record the position
        positions[market_id] = {
            "contracts": contracts,
            "entry_price": cost_price,
            "cost": cost,
            "direction": direction,
        }
        exposure += cost

        # Resolve immediately (we have the outcome)
        actual = outcomes[market_id]
        is_yes = "YES" in direction.upper()
        won = (actual == 1 and is_yes) or (actual == 0 and not is_yes)

        if won:
            payout = contracts * 1.0  # Binary market: $1 per contract on win
            pnl = payout - cost
            wins.append(pnl)
        else:
            pnl = -cost  # Lose entire cost basis
            losses.append(pnl)

        # Apply stop-loss cap on losses
        max_loss = -cost * (1.0 + params.stop_loss_pct)
        pnl = max(pnl, max_loss)

        equity += pnl
        pnl_series.append(pnl)
        exposure -= cost  # Position resolved
        del positions[market_id]

        # Track drawdown
        peak_equity = max(peak_equity, equity)
        dd = peak_equity - equity
        max_dd = max(max_dd, dd)

    result.total_pnl = equity - bankroll
    result.max_drawdown = max_dd
    result.win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    result.avg_position_size = sum(position_sizes) / len(position_sizes) if position_sizes else 0

    # Sharpe ratio
    if len(pnl_series) > 2:
        mean_pnl = sum(pnl_series) / len(pnl_series)
        var = sum((p - mean_pnl) ** 2 for p in pnl_series) / len(pnl_series)
        std = var ** 0.5
        if std > 0:
            result.sharpe_ratio = (mean_pnl / std) * (252 ** 0.5)

    return result


def load_data(
    db: Database, days: int, train_pct: float = 0.60,
) -> tuple[list[dict], list[dict], dict[str, int], bool]:
    """Load signals and outcomes from database, split into train/test sets.

    Splits signals temporally: first train_pct% by time for in-sample
    parameter optimization, remaining for out-of-sample validation.

    Args:
        db: Database instance.
        days: Lookback period in days.
        train_pct: Fraction of signals for training (default 60%).

    Returns:
        (train_signals, test_signals, outcomes, has_synthetic)
        has_synthetic: True if any market in the dataset has synthetic snapshots.
    """
    conn = db._get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Load ALL signals (including risk-gated ones) for replay
    signals = [
        dict(r)
        for r in conn.execute(
            """SELECT market_id, strategy, direction, edge,
                      probability_estimate, market_price, confidence, timestamp
               FROM signals WHERE timestamp >= ?
               ORDER BY timestamp ASC""",
            (cutoff,),
        ).fetchall()
    ]

    # Load resolved outcomes from calibration records
    outcomes_raw = conn.execute(
        """SELECT market_id, actual_outcome
           FROM calibration_records
           WHERE actual_outcome IS NOT NULL AND predicted_at >= ?""",
        (cutoff,),
    ).fetchall()

    # Also check market results
    market_results = conn.execute(
        """SELECT ticker as market_id, result
           FROM markets
           WHERE result IS NOT NULL"""
    ).fetchall()

    outcomes: dict[str, int] = {}
    for row in outcomes_raw:
        outcomes[row["market_id"]] = int(row["actual_outcome"])
    for row in market_results:
        r = row["result"]
        if row["market_id"] not in outcomes and r is not None and r != "":
            try:
                outcomes[row["market_id"]] = int(r)
            except (ValueError, TypeError):
                pass

    # Check for synthetic snapshots in the dataset
    has_synthetic = False
    try:
        syn_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM market_snapshots WHERE is_synthetic = 1"
        ).fetchone()
        has_synthetic = (syn_count["cnt"] if syn_count else 0) > 0
    except Exception:
        pass  # Column may not exist yet in older DBs

    # Temporal train/test split
    split_idx = int(len(signals) * train_pct)
    train_signals = signals[:split_idx]
    test_signals = signals[split_idx:]

    return train_signals, test_signals, outcomes, has_synthetic


def generate_parameter_grid() -> list[ParameterSet]:
    """Generate parameter combinations to test."""
    grid = []
    for kelly in [0.15, 0.20, 0.25, 0.30]:
        for min_edge in [0.04, 0.05, 0.06, 0.07]:
            for min_conf in [0.55, 0.60, 0.65, 0.70]:
                for stop_loss in [0.15, 0.20, 0.25]:
                    grid.append(ParameterSet(
                        kelly_fraction=kelly,
                        min_edge_ai=min_edge,
                        min_edge_news=max(0.03, min_edge - 0.01),
                        min_confidence=min_conf,
                        stop_loss_pct=stop_loss,
                    ))
    return grid


def _format_result_table(
    results: list[ReplayResult], header: str, top_n: int = 20,
) -> list[str]:
    """Format a ranked table of replay results."""
    lines = [header, ""]
    ranked = sorted(results, key=lambda r: r.total_pnl, reverse=True)

    lines.append(
        f"{'Rank':>4}  {'P&L':>8}  {'Win%':>5}  {'PF':>5}  {'MaxDD':>7}  "
        f"{'Sharpe':>6}  {'Trades':>6}  {'AvgSz':>6}  Parameters"
    )
    lines.append("─" * 80)

    for i, r in enumerate(ranked[:top_n], 1):
        pf_str = f"{r.profit_factor:.1f}" if r.profit_factor < 100 else "∞"
        sharpe_str = f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio is not None else "N/A"
        lines.append(
            f"  {i:2d}  ${r.total_pnl:>7.2f}  {r.win_rate:>4.0%}  {pf_str:>5}  "
            f"${r.max_drawdown:>6.2f}  {sharpe_str:>6}  {r.acted_signals:>6}  "
            f"{r.params.label()}"
        )

    lines.append("")
    lines.append("─" * 80)

    # Worst performers
    if len(ranked) > 5:
        lines.append("Bottom 5:")
        for r in ranked[-5:]:
            pf_str = f"{r.profit_factor:.1f}" if r.profit_factor < 100 else "∞"
            lines.append(
                f"  ${r.total_pnl:>7.2f}  {r.win_rate:>4.0%}  PF={pf_str}  "
                f"{r.params.label()}"
            )
        lines.append("")

    return lines


def format_replay_report(
    is_results: list[ReplayResult],
    oos_results: list[ReplayResult],
    days: int,
    train_signals: int,
    test_signals: int,
    total_outcomes: int,
    has_synthetic: bool = False,
) -> str:
    """Format replay results as a report with in-sample and out-of-sample sections."""
    lines = [
        f"{'=' * 80}",
        f"PARAMETER REPLAY REPORT — Last {days} days",
        f"{'=' * 80}",
        f"Train signals: {train_signals} | Test signals: {test_signals} | "
        f"Resolved outcomes: {total_outcomes}",
        "",
    ]

    if has_synthetic:
        lines.extend([
            "WARNING: Dataset contains SYNTHETIC snapshots (generated from known outcomes).",
            "Results are an UPPER BOUND — do NOT use for live trading parameter selection.",
            "",
        ])

    # In-sample results
    lines.extend(_format_result_table(
        is_results,
        f"IN-SAMPLE (first {train_signals} signals — used for parameter fitting)",
    ))

    # Out-of-sample results
    if oos_results:
        lines.extend(_format_result_table(
            oos_results,
            f"OUT-OF-SAMPLE (last {test_signals} signals — validation, not used for fitting)",
        ))

        # Compare top IS params on OOS data
        is_ranked = sorted(is_results, key=lambda r: r.total_pnl, reverse=True)
        oos_by_label = {r.params.label(): r for r in oos_results}
        lines.append("TOP 5 IN-SAMPLE → OUT-OF-SAMPLE COMPARISON:")
        lines.append(
            f"  {'IS P&L':>9}  {'OOS P&L':>9}  {'IS Win%':>7}  {'OOS Win%':>8}  Parameters"
        )
        lines.append("─" * 80)
        for r in is_ranked[:5]:
            oos_r = oos_by_label.get(r.params.label())
            if oos_r:
                lines.append(
                    f"  ${r.total_pnl:>8.2f}  ${oos_r.total_pnl:>8.2f}  "
                    f"{r.win_rate:>6.0%}  {oos_r.win_rate:>7.0%}  "
                    f"{r.params.label()}"
                )
        lines.append("")

    # Current config comparison
    current = ParameterSet()
    all_results = is_results + oos_results
    is_ranked = sorted(is_results, key=lambda r: r.total_pnl, reverse=True)
    current_is = next((r for r in is_ranked if _params_match(r.params, current)), None)
    if current_is:
        rank = is_ranked.index(current_is) + 1
        lines.append(
            f"Current config IS rank #{rank}/{len(is_ranked)}: "
            f"P&L=${current_is.total_pnl:.2f}, "
            f"Win={current_is.win_rate:.0%}, "
            f"MaxDD=${current_is.max_drawdown:.2f}"
        )
        oos_ranked = sorted(oos_results, key=lambda r: r.total_pnl, reverse=True)
        current_oos = next((r for r in oos_ranked if _params_match(r.params, current)), None)
        if current_oos:
            oos_rank = oos_ranked.index(current_oos) + 1
            lines.append(
                f"Current config OOS rank #{oos_rank}/{len(oos_ranked)}: "
                f"P&L=${current_oos.total_pnl:.2f}, "
                f"Win={current_oos.win_rate:.0%}, "
                f"MaxDD=${current_oos.max_drawdown:.2f}"
            )

    lines.append(f"{'=' * 80}")
    return "\n".join(lines)


def _params_match(a: ParameterSet, b: ParameterSet) -> bool:
    return (
        a.kelly_fraction == b.kelly_fraction
        and a.min_edge_ai == b.min_edge_ai
        and a.min_confidence == b.min_confidence
        and a.stop_loss_pct == b.stop_loss_pct
    )


def main():
    parser = argparse.ArgumentParser(description="PolyEdge Parameter Replay Backtest")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--days", type=int, default=30, help="Lookback period")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll")
    parser.add_argument("--train-pct", type=float, default=0.60,
                        help="Fraction of signals for in-sample training (default 0.60)")
    args = parser.parse_args()

    db = Database(args.db)
    train_signals, test_signals, outcomes, has_synthetic = load_data(
        db, args.days, train_pct=args.train_pct,
    )

    all_signals = train_signals + test_signals
    if not all_signals:
        print("No signals found. Run some trading cycles first.")
        return
    if not outcomes:
        print(f"No resolved outcomes found in last {args.days} days. Need market resolutions.")
        return

    if has_synthetic:
        print(
            "WARNING: Synthetic snapshots detected in database. "
            "Results are an UPPER BOUND only."
        )

    print(
        f"Loaded {len(all_signals)} signals "
        f"(train={len(train_signals)}, test={len(test_signals)}), "
        f"{len(outcomes)} resolved outcomes"
    )

    grid = generate_parameter_grid()
    print(f"Testing {len(grid)} parameter combinations...")

    # Run in-sample (training set)
    is_results = []
    for params in grid:
        result = replay_signals(train_signals, outcomes, params, args.bankroll)
        is_results.append(result)

    # Run out-of-sample (test set) with same parameter grid
    oos_results = []
    for params in grid:
        result = replay_signals(test_signals, outcomes, params, args.bankroll)
        result.is_oos = True
        oos_results.append(result)

    print(format_replay_report(
        is_results, oos_results, args.days,
        len(train_signals), len(test_signals), len(outcomes),
        has_synthetic=has_synthetic,
    ))


if __name__ == "__main__":
    main()
