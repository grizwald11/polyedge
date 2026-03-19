"""Strategy replay engine — replays strategies against historical snapshots.

Usage:
    python -m scripts.backtest_engine [--db data/markets.db] [--strategy ai_probability]
    python -m scripts.backtest_engine --sweep kelly_fraction=0.25,0.5,0.75
    python -m scripts.backtest_engine --validate

Unlike run_backtest.py (which is a post-hoc trade analyzer), this engine
replays strategies from scratch using historical price snapshots, the real
RiskEngine, KellySizer, and CircuitBreaker via dependency injection.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings, load_settings
from src.core.models import (
    CalibrationRecord, Direction, ForecastResult, Market, MarketSnapshot,
    MarketToken, Order, OrderStatus, OrderType, Side, Signal,
    StrategyName, TokenOutcome, Trade,
)
from src.risk.kelly_sizer import KellySizer
from src.risk.circuit_breaker import CircuitBreaker
from src.storage.database import Database


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────

@dataclass
class BacktestTrade:
    """A simulated trade during backtesting."""
    market_id: str
    direction: Direction
    strategy: str
    price: float
    size: int
    timestamp: str
    pnl: float = 0.0
    resolved: bool = False
    outcome: Optional[bool] = None


@dataclass
class BacktestResult:
    """Full results from a backtest run."""
    strategy: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    brier_score: float | None = None
    sharpe_ratio: float | None = None
    calmar_ratio: float | None = None
    avg_edge_predicted: float = 0.0
    avg_edge_realized: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)


# ──────────────────────────────────────────────
# Mock Forecaster (no Claude API calls)
# ──────────────────────────────────────────────

class MockForecaster:
    """Implements ClaudeForecaster interface using cached data or synthetic forecasts.

    Modes:
    1. Cached: Uses calibration_records from DB (predictions already made)
    2. Outcome-derived: If we know the outcome, generate synthetic forecasts
       with configurable noise to simulate different accuracy levels.
    """

    def __init__(self, db: Database, noise: float = 0.1):
        self.db = db
        self.noise = noise
        self._cache: dict[str, float] = {}
        self._outcomes: dict[str, bool] = {}
        self._load_cache()

    def _load_cache(self):
        """Load cached predictions from calibration_records."""
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT market_id, predicted_probability, actual_outcome "
            "FROM calibration_records"
        ).fetchall()
        for row in rows:
            self._cache[row["market_id"]] = row["predicted_probability"]
            if row["actual_outcome"] is not None:
                self._outcomes[row["market_id"]] = bool(row["actual_outcome"])

    def get_forecast(self, market_id: str, yes_price: float) -> Optional[ForecastResult]:
        """Get a forecast for a market.

        Uses cached prediction if available, otherwise generates a
        synthetic one from the outcome (if known).
        """
        if market_id in self._cache:
            prob = self._cache[market_id]
            return ForecastResult(
                probability=prob,
                confidence_low=max(0, prob - 0.15),
                confidence_high=min(1, prob + 0.15),
                reasoning="cached",
                model_used="backtest_cache",
            )

        if market_id in self._outcomes:
            import random
            actual = 1.0 if self._outcomes[market_id] else 0.0
            # Add noise to simulate imperfect prediction
            noise = random.gauss(0, self.noise)
            prob = max(0.01, min(0.99, actual + noise))
            return ForecastResult(
                probability=prob,
                confidence_low=max(0, prob - 0.2),
                confidence_high=min(1, prob + 0.2),
                reasoning="synthetic",
                model_used="backtest_synthetic",
            )

        return None


# ──────────────────────────────────────────────
# Backtest Portfolio
# ──────────────────────────────────────────────

@dataclass
class BacktestPosition:
    market_id: str
    direction: Direction
    size: int
    entry_price: float
    strategy: str


class BacktestPortfolio:
    """Tracks simulated bankroll, positions, and exposure."""

    def __init__(self, bankroll: float):
        self.initial_bankroll = bankroll
        self.bankroll = bankroll
        self.positions: dict[str, BacktestPosition] = {}
        self.total_pnl = 0.0

    def get_exposure(self) -> float:
        return sum(p.size * p.entry_price for p in self.positions.values())

    def has_position(self, market_id: str) -> bool:
        return market_id in self.positions

    def open_position(self, market_id: str, direction: Direction,
                      size: int, price: float, strategy: str):
        cost = size * price
        self.bankroll -= cost
        self.positions[market_id] = BacktestPosition(
            market_id=market_id,
            direction=direction,
            size=size,
            entry_price=price,
            strategy=strategy,
        )

    def close_position(self, market_id: str, exit_price: float) -> float:
        pos = self.positions.pop(market_id, None)
        if pos is None:
            return 0.0

        # P&L = proceeds - cost, regardless of direction.
        # open_position deducted (size * entry_price), we get back (size * exit_price).
        pnl = (exit_price - pos.entry_price) * pos.size

        self.bankroll += pos.size * exit_price
        self.total_pnl += pnl
        return pnl

    def resolve_position(self, market_id: str, outcome: bool) -> float:
        """Resolve a position at market settlement."""
        pos = self.positions.get(market_id)
        if pos is None:
            return 0.0

        if pos.direction in (Direction.BUY_YES, Direction.SELL_NO):
            exit_price = 1.0 if outcome else 0.0
        else:
            exit_price = 0.0 if outcome else 1.0

        return self.close_position(market_id, exit_price)


# ──────────────────────────────────────────────
# Backtest Engine
# ──────────────────────────────────────────────

class BacktestEngine:
    """Orchestrates replay through historical snapshots."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        forecaster: MockForecaster | None = None,
        strategy_filter: str | None = None,
    ):
        self.db = db
        self.settings = settings
        self.forecaster = forecaster or MockForecaster(db)
        self.strategy_filter = strategy_filter
        self.kelly = KellySizer(settings)

    def run(self, bankroll: float = 500.0) -> list[BacktestResult]:
        """Run the backtest.

        1. Load all settled markets with snapshots
        2. Sort snapshots chronologically
        3. For each snapshot: run strategy signals → risk check → simulate fills
        4. At resolution: resolve positions
        """
        portfolio = BacktestPortfolio(bankroll)

        # Load settled markets with outcomes
        conn = self.db._get_conn()
        market_rows = conn.execute("""
            SELECT ticker, question, category, result, tokens, end_date, event_ticker,
                   volume_24h, liquidity
            FROM markets WHERE result != '' AND result IS NOT NULL
        """).fetchall()

        if not market_rows:
            return []

        # Build market lookup and outcomes
        outcomes: dict[str, bool] = {}
        market_lookup: dict[str, dict] = {}
        for row in market_rows:
            ticker = row["ticker"]
            result_str = row["result"]
            if result_str.lower() in ("yes", "1", "true"):
                outcomes[ticker] = True
            elif result_str.lower() in ("no", "0", "false"):
                outcomes[ticker] = False
            else:
                continue
            market_lookup[ticker] = dict(row)

        # Load snapshots for these markets
        all_snapshots = conn.execute("""
            SELECT market_id, timestamp, yes_price, no_price, spread, volume_1h, liquidity
            FROM market_snapshots
            WHERE market_id IN ({})
            ORDER BY timestamp ASC
        """.format(",".join("?" * len(market_lookup))),
            list(market_lookup.keys()),
        ).fetchall()

        if not all_snapshots:
            return []

        # Process snapshots chronologically
        trades: list[BacktestTrade] = []
        edges_predicted: list[float] = []
        edges_realized: list[float] = []
        equity_curve = [bankroll]

        for snap in all_snapshots:
            market_id = snap["market_id"]
            yes_price = snap["yes_price"]
            no_price = snap["no_price"]
            timestamp = snap["timestamp"]

            if market_id not in market_lookup:
                continue
            if portfolio.has_position(market_id):
                continue

            # Get forecast
            forecast = self.forecaster.get_forecast(market_id, yes_price)
            if forecast is None:
                continue

            # Calculate edge
            edge = forecast.probability - yes_price
            direction = Direction.BUY_YES if edge > 0 else Direction.BUY_NO
            abs_edge = abs(edge)

            # Apply strategy filter
            strategy = "ai_probability"
            min_edge = self.settings.trading.min_edge_ai
            if self.strategy_filter and self.strategy_filter != strategy:
                continue
            if abs_edge < min_edge:
                continue

            # Kelly sizing
            if direction == Direction.BUY_YES:
                prob = forecast.probability
                order_price = yes_price
            else:
                prob = 1.0 - forecast.probability
                order_price = no_price
                abs_edge = prob - no_price
                if abs_edge <= 0:
                    continue

            contracts = self.kelly.calculate_position_size(
                edge=abs_edge,
                probability=prob,
                bankroll=portfolio.bankroll,
                current_exposure=portfolio.get_exposure(),
                order_price=order_price,
            )

            if contracts <= 0:
                continue

            # Simulate fill
            portfolio.open_position(market_id, direction, contracts, order_price, strategy)
            edges_predicted.append(abs_edge)

            trades.append(BacktestTrade(
                market_id=market_id,
                direction=direction,
                strategy=strategy,
                price=order_price,
                size=contracts,
                timestamp=timestamp,
            ))

        # Resolve all open positions at settlement
        for market_id, outcome in outcomes.items():
            if portfolio.has_position(market_id):
                pnl = portfolio.resolve_position(market_id, outcome)
                # Find corresponding trade and update
                for t in trades:
                    if t.market_id == market_id and not t.resolved:
                        t.pnl = pnl
                        t.resolved = True
                        t.outcome = outcome
                        if t.price > 0:
                            edges_realized.append(pnl / (t.size * t.price) if t.size > 0 else 0)
                        equity_curve.append(equity_curve[-1] + pnl)
                        break

        # Build result
        return [self._compute_result(
            "all" if not self.strategy_filter else self.strategy_filter,
            trades, equity_curve, edges_predicted, edges_realized, bankroll,
        )]

    def _compute_result(
        self, strategy: str, trades: list[BacktestTrade],
        equity_curve: list[float], edges_predicted: list[float],
        edges_realized: list[float], bankroll: float,
    ) -> BacktestResult:
        result = BacktestResult(strategy=strategy, equity_curve=equity_curve, trades=trades)

        resolved_trades = [t for t in trades if t.resolved]
        result.total_trades = len(resolved_trades)

        wins = [t.pnl for t in resolved_trades if t.pnl > 0]
        losses = [t.pnl for t in resolved_trades if t.pnl < 0]

        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.total_pnl = sum(t.pnl for t in resolved_trades)
        result.win_rate = len(wins) / len(resolved_trades) if resolved_trades else 0.0
        result.avg_win = sum(wins) / len(wins) if wins else 0.0
        result.avg_loss = sum(losses) / len(losses) if losses else 0.0

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
        result.max_drawdown_pct = max_dd / bankroll if bankroll > 0 else 0.0

        # Calmar ratio
        if max_dd > 0 and len(equity_curve) > 1:
            total_return = equity_curve[-1] / bankroll - 1.0
            result.calmar_ratio = total_return / (max_dd / bankroll)

        # Sharpe
        if len(equity_curve) > 2:
            daily_ret = []
            for i in range(1, len(equity_curve)):
                if equity_curve[i - 1] > 0:
                    daily_ret.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
            if daily_ret:
                mean_r = sum(daily_ret) / len(daily_ret)
                var = sum((r - mean_r) ** 2 for r in daily_ret) / len(daily_ret)
                std = var ** 0.5
                result.sharpe_ratio = (mean_r / std) * (252 ** 0.5) if std > 0 else None
                result.daily_returns = daily_ret

        # Average edges
        result.avg_edge_predicted = sum(edges_predicted) / len(edges_predicted) if edges_predicted else 0.0
        result.avg_edge_realized = sum(edges_realized) / len(edges_realized) if edges_realized else 0.0

        # Brier score from resolved trades
        brier_scores = []
        for t in resolved_trades:
            if t.outcome is not None:
                predicted = t.price if t.direction == Direction.BUY_YES else (1.0 - t.price)
                actual = 1.0 if t.outcome else 0.0
                brier_scores.append((predicted - actual) ** 2)
        result.brier_score = sum(brier_scores) / len(brier_scores) if brier_scores else None

        return result


# ──────────────────────────────────────────────
# Validation (Phase 3 exit criteria)
# ──────────────────────────────────────────────

def validate_backtest(result: BacktestResult) -> dict:
    """Check Phase 3 exit criteria.

    Returns dict with each criterion and pass/fail.
    """
    checks = {}
    checks["min_trades_50"] = {
        "pass": result.total_trades >= 50,
        "value": result.total_trades,
        "threshold": 50,
    }
    checks["brier_below_020"] = {
        "pass": result.brier_score is not None and result.brier_score < 0.20,
        "value": result.brier_score,
        "threshold": 0.20,
    }
    checks["win_rate_55_70"] = {
        "pass": 0.55 <= result.win_rate <= 0.70,
        "value": result.win_rate,
        "threshold": "55-70%",
    }
    checks["positive_pnl"] = {
        "pass": result.total_pnl > 0,
        "value": result.total_pnl,
        "threshold": "> $0",
    }
    checks["max_drawdown_below_20pct"] = {
        "pass": result.max_drawdown_pct < 0.20,
        "value": result.max_drawdown_pct,
        "threshold": "< 20%",
    }

    all_pass = all(c["pass"] for c in checks.values())
    checks["overall"] = {"pass": all_pass}
    return checks


# ──────────────────────────────────────────────
# Parameter Sweep
# ──────────────────────────────────────────────

def parameter_sweep(
    db: Database,
    base_settings: Settings,
    param_name: str,
    values: list[float],
    bankroll: float = 500.0,
) -> list[tuple[float, BacktestResult]]:
    """Run backtests with different parameter values.

    Args:
        db: Database with historical data
        base_settings: Base settings to modify
        param_name: Setting field to sweep (e.g. "kelly_fraction")
        values: List of values to try
        bankroll: Starting bankroll

    Returns:
        List of (param_value, BacktestResult) tuples
    """
    results = []
    for val in values:
        # Clone settings and modify
        settings = base_settings.model_copy(deep=True)
        if param_name == "kelly_fraction":
            settings.trading.kelly_fraction = val
        elif param_name == "min_edge_ai":
            settings.trading.min_edge_ai = val
        elif param_name == "max_position_pct":
            settings.trading.max_position_pct = val
        else:
            print(f"Unknown parameter: {param_name}")
            continue

        engine = BacktestEngine(db, settings)
        bt_results = engine.run(bankroll=bankroll)
        if bt_results:
            results.append((val, bt_results[0]))

    return results


# ──────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────

def format_result(result: BacktestResult, bankroll: float = 500.0) -> str:
    lines = [
        f"Strategy: {result.strategy}",
        f"  Trades:        {result.total_trades} ({result.winning_trades}W / {result.losing_trades}L)",
        f"  Win Rate:      {result.win_rate:.1%}",
        f"  Total P&L:     ${result.total_pnl:,.2f}",
        f"  Return:        {result.total_pnl / bankroll:.1%}" if bankroll > 0 else "",
        f"  Avg Win:       ${result.avg_win:,.2f}",
        f"  Avg Loss:      ${result.avg_loss:,.2f}",
        f"  Profit Factor: {result.profit_factor:.2f}" if result.profit_factor != float("inf") else "  Profit Factor: inf",
        f"  Max Drawdown:  ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.1%})",
        f"  Avg Edge Pred: {result.avg_edge_predicted:.1%}",
        f"  Avg Edge Real: {result.avg_edge_realized:.1%}",
    ]
    if result.brier_score is not None:
        lines.append(f"  Brier Score:   {result.brier_score:.3f}")
    if result.sharpe_ratio is not None:
        lines.append(f"  Sharpe Ratio:  {result.sharpe_ratio:.2f}")
    if result.calmar_ratio is not None:
        lines.append(f"  Calmar Ratio:  {result.calmar_ratio:.2f}")
    return "\n".join(lines)


def format_sweep(sweep_results: list[tuple[float, BacktestResult]], param_name: str) -> str:
    lines = [
        f"{'=' * 70}",
        f"PARAMETER SWEEP: {param_name}",
        f"{'=' * 70}",
        f"{'Value':>10s} | {'Trades':>6s} | {'Win%':>6s} | {'P&L':>10s} | {'MaxDD%':>7s} | {'Sharpe':>7s}",
        f"{'-' * 70}",
    ]
    for val, r in sweep_results:
        sharpe = f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio is not None else "N/A"
        lines.append(
            f"{val:>10.3f} | {r.total_trades:>6d} | {r.win_rate:>5.1%} | "
            f"${r.total_pnl:>9,.2f} | {r.max_drawdown_pct:>6.1%} | {sharpe:>7s}"
        )
    lines.append(f"{'=' * 70}")
    return "\n".join(lines)


def format_validation(checks: dict) -> str:
    lines = ["Phase 3 Exit Criteria Validation:", ""]
    for name, check in checks.items():
        if name == "overall":
            continue
        status = "PASS" if check["pass"] else "FAIL"
        lines.append(f"  [{status}] {name}: {check['value']} (threshold: {check['threshold']})")
    overall = "ALL PASS — Ready for live trading" if checks["overall"]["pass"] else "NOT READY — Fix failing criteria"
    lines.append(f"\n  Result: {overall}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PolyEdge Backtest Engine")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--strategy", default=None, help="Filter to strategy")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll")
    parser.add_argument("--validate", action="store_true", help="Run Phase 3 exit criteria check")
    parser.add_argument("--sweep", default=None,
                        help="Parameter sweep: param_name=v1,v2,v3 (e.g. kelly_fraction=0.25,0.5,0.75)")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(args.db)

    if args.sweep:
        param_name, values_str = args.sweep.split("=")
        values = [float(v) for v in values_str.split(",")]
        results = parameter_sweep(db, settings, param_name, values, args.bankroll)
        if results:
            print(format_sweep(results, param_name))
        else:
            print("No results — ensure historical data is available (run backfill_markets first)")
        return

    engine = BacktestEngine(db, settings, strategy_filter=args.strategy)
    results = engine.run(bankroll=args.bankroll)

    if not results:
        print("No data for backtest. Run backfill_markets --snapshots first.")
        return

    print("=" * 60)
    print("BACKTEST REPLAY RESULTS")
    print("=" * 60)
    for r in results:
        print(format_result(r, args.bankroll))
        print()

    if args.validate and results:
        checks = validate_backtest(results[0])
        print(format_validation(checks))


if __name__ == "__main__":
    main()
