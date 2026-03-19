"""Backtest framework — replay strategies against historical market data.

Usage:
    python -m scripts.run_backtest [--db data/markets.db] [--days 30] [--strategy ai_probability]

Loads resolved predictions and trade history from the database, then
simulates what the portfolio would look like if those signals had been
followed with current risk parameters.

This is NOT a full market replay (we don't re-run Claude). It evaluates:
1. Signal quality: Were the signals that were generated actually profitable?
2. Sizing quality: Did Kelly sizing and risk caps protect capital?
3. Strategy comparison: Which strategies performed best?
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings
from src.storage.database import Database
from src.core.models import StrategyName


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    strategy: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    brier_score: float | None = None
    sharpe_ratio: float | None = None
    trades: list[dict] = field(default_factory=list)


def run_backtest(
    db: Database,
    days: int = 30,
    strategy: str | None = None,
    bankroll: float = 500.0,
) -> list[BacktestResult]:
    """Run backtest against historical trades and calibration data.

    Args:
        db: Database with historical data
        days: Number of days to look back
        strategy: Filter to specific strategy (None = all)
        bankroll: Starting bankroll for P&L calculations

    Returns:
        List of BacktestResult per strategy
    """
    conn = db._get_conn()

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Load trades
        query = """
            SELECT market_id, side, price, size, fee, realized_pnl,
                   strategy, paper, timestamp
            FROM trades WHERE timestamp >= ?
        """
        params: list = [cutoff]
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        query += " ORDER BY timestamp ASC"

        trades = [dict(r) for r in conn.execute(query, params).fetchall()]

        # Load resolved calibration records
        cal_query = """
            SELECT market_id, strategy, predicted_probability,
                   market_price_at_prediction, actual_outcome, predicted_at
            FROM calibration_records
            WHERE actual_outcome IS NOT NULL AND predicted_at >= ?
        """
        cal_params: list = [cutoff]
        if strategy:
            cal_query += " AND strategy = ?"
            cal_params.append(strategy)

        calibrations = [dict(r) for r in conn.execute(cal_query, cal_params).fetchall()]

    finally:
        pass  # Don't close db._get_conn() — it's the persistent connection

    # Group by strategy
    strategies: set[str] = set()
    for t in trades:
        strategies.add(t["strategy"])
    for c in calibrations:
        strategies.add(c["strategy"])

    if not strategies:
        return []

    results: list[BacktestResult] = []
    for strat in sorted(strategies):
        strat_trades = [t for t in trades if t["strategy"] == strat]
        strat_cals = [c for c in calibrations if c["strategy"] == strat]

        result = _analyze_strategy(strat, strat_trades, strat_cals, bankroll)
        results.append(result)

    return results


def _analyze_strategy(
    strategy: str,
    trades: list[dict],
    calibrations: list[dict],
    bankroll: float,
) -> BacktestResult:
    """Analyze performance for a single strategy."""
    result = BacktestResult(strategy=strategy, trades=trades)
    result.total_trades = len(trades)

    if not trades:
        # Can still compute Brier from calibration data
        result.brier_score = _compute_brier(calibrations)
        return result

    # Compute P&L from trade history
    equity_curve: list[float] = [bankroll]
    wins: list[float] = []
    losses: list[float] = []

    for trade in trades:
        pnl = trade.get("realized_pnl", 0.0)
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)
        equity_curve.append(equity_curve[-1] + pnl)

    result.winning_trades = len(wins)
    result.losing_trades = len(losses)
    result.total_pnl = sum(t.get("realized_pnl", 0) for t in trades)
    result.win_rate = len(wins) / len(trades) if trades else 0.0
    result.avg_win = sum(wins) / len(wins) if wins else 0.0
    result.avg_loss = sum(losses) / len(losses) if losses else 0.0

    # Profit factor = gross profit / gross loss
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown
    peak = bankroll
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
    result.max_drawdown = max_dd

    # Brier score from calibration data
    result.brier_score = _compute_brier(calibrations)

    # Simple Sharpe approximation (daily returns)
    if len(equity_curve) > 2:
        daily_returns: list[float] = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                daily_returns.append(
                    (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                )
        if daily_returns:
            mean_r = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
            std_r = variance ** 0.5
            result.sharpe_ratio = (mean_r / std_r) * (252 ** 0.5) if std_r > 0 else None

    return result


def _compute_brier(calibrations: list[dict]) -> float | None:
    """Compute Brier score from resolved calibration records."""
    if not calibrations:
        return None

    scores: list[float] = []
    for c in calibrations:
        outcome = c.get("actual_outcome")
        if outcome is None:
            continue
        prob = c["predicted_probability"]
        actual = 1.0 if outcome else 0.0
        scores.append((prob - actual) ** 2)

    return sum(scores) / len(scores) if scores else None


def format_report(results: list[BacktestResult], days: int) -> str:
    """Format backtest results as a readable report."""
    lines = [
        f"{'=' * 60}",
        f"BACKTEST REPORT — Last {days} days",
        f"{'=' * 60}",
        "",
    ]

    for r in results:
        lines.append(f"Strategy: {r.strategy}")
        lines.append(f"  Trades:        {r.total_trades} ({r.winning_trades}W / {r.losing_trades}L)")
        lines.append(f"  Win Rate:      {r.win_rate:.1%}")
        lines.append(f"  Total P&L:     ${r.total_pnl:,.2f}")
        lines.append(f"  Avg Win:       ${r.avg_win:,.2f}")
        lines.append(f"  Avg Loss:      ${r.avg_loss:,.2f}")
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        lines.append(f"  Profit Factor: {pf_str}")
        lines.append(f"  Max Drawdown:  ${r.max_drawdown:,.2f}")
        if r.brier_score is not None:
            lines.append(f"  Brier Score:   {r.brier_score:.3f}")
        if r.sharpe_ratio is not None:
            lines.append(f"  Sharpe Ratio:  {r.sharpe_ratio:.2f}")
        lines.append("")

    # Overall
    total_pnl = sum(r.total_pnl for r in results)
    total_trades = sum(r.total_trades for r in results)
    total_wins = sum(r.winning_trades for r in results)
    lines.append(f"{'─' * 60}")
    lines.append(f"OVERALL: {total_trades} trades, ${total_pnl:,.2f} P&L, "
                 f"{total_wins/total_trades:.1%} win rate" if total_trades > 0 else "OVERALL: No trades")
    lines.append(f"{'=' * 60}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PolyEdge Backtest")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--days", type=int, default=30, help="Lookback period in days")
    parser.add_argument("--strategy", default=None, help="Filter to strategy name")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll")
    args = parser.parse_args()

    db = Database(args.db)
    results = run_backtest(db, days=args.days, strategy=args.strategy, bankroll=args.bankroll)

    if not results:
        print("No data found for backtest. Run some trading cycles first.")
        return

    print(format_report(results, args.days))


if __name__ == "__main__":
    main()
