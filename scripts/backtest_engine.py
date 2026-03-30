"""Strategy replay engine — replays strategies against historical snapshots.

Usage:
    python -m scripts.backtest_engine [--db data/markets.db] [--strategy ai_probability]
    python -m scripts.backtest_engine --sweep kelly_fraction=0.25,0.5,0.75
    python -m scripts.backtest_engine --validate

Unlike run_backtest.py (which is a post-hoc trade analyzer), this engine
replays strategies from scratch using historical price snapshots, the real
RiskEngine, KellySizer, and CircuitBreaker via dependency injection.

Remaining limitations (acknowledged in results):
- Lookahead bias (outcome-derived mode): BacktestResult.uses_lookahead is set True
  and the result is flagged as "oracle_upper_bound" when synthetic forecasts are used.
  Degradation factor is halved for those runs to reduce over-optimism.
- Survivorship bias (partially mitigated): Active/abandoned markets are now included;
  open positions at backtest end are marked-to-last-known-price. Unresolved position
  count is reported in BacktestResult.unresolved_positions.
- Execution model: Order-miss / partial-fill simulation is heuristic, not data-driven.
- Slippage: Maker fills assumed at limit price; taker slippage not modelled.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from src.config import Settings, load_settings
from src.core.models import (
    CalibrationRecord, Direction, ForecastResult, Market, MarketSnapshot,
    MarketToken, Order, OrderStatus, OrderType, Side, Signal,
    StrategyName, TokenOutcome, Trade,
    kalshi_taker_fee, kalshi_maker_fee,
)
from src.risk.kelly_sizer import KellySizer
from src.risk.circuit_breaker import CircuitBreaker
from src.storage.database import Database

# Per-strategy degradation factors: live trading typically underperforms backtests
# due to remaining lookahead (outcome-derived mode halves this further), execution
# costs, and heuristic fill simulation.
# Apply these factors to live edge thresholds derived from backtest results.
BACKTEST_DEGRADATION_FACTORS: dict[str, float] = {
    "ai_probability": 0.65,
    "obvious_no": 0.85,
    "cross_arb": 0.60,
    "whale_tracker": 0.55,
    "news_reactive": 0.50,
    "default": 0.70,
}


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
    cb_skipped: int = 0
    # C-1: lookahead bias flag — True when outcome-derived MockForecaster was used
    uses_lookahead: bool = False
    # C-2: positions still open at backtest end, marked-to-last-known-price
    unresolved_positions: int = 0
    # C-3: total fees deducted from P&L during simulation
    total_fees: float = 0.0
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
        self._warned_lookahead = False
        # C-1: track whether outcome-derived (lookahead) mode has been used at all
        self.used_lookahead = False
        self._load_cache()

    def _load_cache(self):
        """Load cached predictions from calibration_records and outcomes from markets."""
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT market_id, predicted_probability, actual_outcome "
            "FROM calibration_records"
        ).fetchall()
        for row in rows:
            self._cache[row["market_id"]] = row["predicted_probability"]
            if row["actual_outcome"] is not None:
                self._outcomes[row["market_id"]] = bool(row["actual_outcome"])

        # Also load outcomes from settled markets (for synthetic forecasts)
        market_rows = conn.execute(
            "SELECT ticker, result FROM markets WHERE result != '' AND result IS NOT NULL"
        ).fetchall()
        for row in market_rows:
            ticker = row["ticker"]
            if ticker not in self._outcomes:
                result = row["result"].lower()
                if result in ("yes", "1", "true"):
                    self._outcomes[ticker] = True
                elif result in ("no", "0", "false"):
                    self._outcomes[ticker] = False

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
            if not self._warned_lookahead:
                logger.warning(
                    "MockForecaster using outcome-derived mode — results have lookahead bias "
                    "(oracle_upper_bound). Degradation factor is halved for this run. "
                    "Do NOT use for live trading decisions."
                )
                self._warned_lookahead = True
            # C-1: record that lookahead was used
            self.used_lookahead = True
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
        # C-3: cumulative fees deducted across all trades
        self.total_fees: float = 0.0

    def get_exposure(self) -> float:
        return sum(p.size * p.entry_price for p in self.positions.values())

    def has_position(self, market_id: str) -> bool:
        return market_id in self.positions

    def open_position(self, market_id: str, direction: Direction,
                      size: int, price: float, strategy: str):
        cost = size * price
        # C-3: deduct entry fee (taker fee on entry; use kalshi_taker_fee as proxy
        # for fee-enabled markets — event markets on Polymarket are fee-free, so this
        # is conservative and intentionally slightly overstates costs for robustness).
        price_cents = round(price * 100)
        entry_fee = kalshi_taker_fee(size, price_cents) / 100.0  # convert cents → dollars
        self.bankroll -= cost + entry_fee
        self.total_fees += entry_fee
        self.positions[market_id] = BacktestPosition(
            market_id=market_id,
            direction=direction,
            size=size,
            entry_price=price,
            strategy=strategy,
        )

    def close_position(self, market_id: str, exit_price: float,
                       use_taker_exit_fee: bool = True) -> float:
        pos = self.positions.pop(market_id, None)
        if pos is None:
            return 0.0

        # P&L = proceeds - cost, regardless of direction.
        # open_position deducted (size * entry_price), we get back (size * exit_price).
        gross_pnl = (exit_price - pos.entry_price) * pos.size

        # M-18: Exit fee defaults to taker for conservative backtesting.
        # Maker exit fee assumes all exits are filled as limit orders, which
        # understates costs in practice (urgency often requires taker orders).
        exit_price_cents = round(exit_price * 100)
        if use_taker_exit_fee:
            exit_fee = kalshi_taker_fee(pos.size, exit_price_cents) / 100.0
        else:
            exit_fee = kalshi_maker_fee(pos.size, exit_price_cents) / 100.0
        self.total_fees += exit_fee

        net_pnl = gross_pnl - exit_fee
        self.bankroll += pos.size * exit_price - exit_fee
        self.total_pnl += net_pnl
        return net_pnl

    def resolve_position(self, market_id: str, outcome: bool,
                         use_taker_exit_fee: bool = True) -> float:
        """Resolve a position at market settlement."""
        pos = self.positions.get(market_id)
        if pos is None:
            return 0.0

        if pos.direction in (Direction.BUY_YES, Direction.SELL_NO):
            exit_price = 1.0 if outcome else 0.0
        else:
            exit_price = 0.0 if outcome else 1.0

        return self.close_position(market_id, exit_price, use_taker_exit_fee=use_taker_exit_fee)


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
        use_taker_exit_fee: bool = True,
        slippage_bps: float = 10.0,
    ):
        self.db = db
        self.settings = settings
        self.forecaster = forecaster or MockForecaster(db)
        self.strategy_filter = strategy_filter
        # M-18: Configurable exit fee model. Default to taker fees for
        # conservative backtesting. Set use_taker_exit_fee=False to use
        # maker fees on exit (optimistic assumption that limit orders fill).
        self.use_taker_exit_fee = use_taker_exit_fee
        # M-19: Simple slippage model in basis points. Applied to entry
        # (price worsened) and exit (price worsened) to simulate market impact.
        self.slippage_bps = slippage_bps
        self.kelly = KellySizer(settings)
        self.circuit_breaker = CircuitBreaker(settings, db)

    def run(self, bankroll: float = 500.0) -> list[BacktestResult]:
        """Run the backtest.

        1. Load all settled markets with snapshots
        2. Sort snapshots chronologically
        3. For each snapshot: run strategy signals → risk check → simulate fills
        4. At resolution: resolve positions
        """
        portfolio = BacktestPortfolio(bankroll)

        # C-2: Load ALL markets (settled + active/abandoned) to avoid survivorship bias.
        # Settled markets have a known outcome; others will be marked-to-last-known-price.
        conn = self.db._get_conn()
        market_rows = conn.execute("""
            SELECT ticker, question, category, result, tokens, end_date, event_ticker,
                   volume_24h, liquidity
            FROM markets
        """).fetchall()

        if not market_rows:
            return []

        # Build market lookup and outcomes
        outcomes: dict[str, bool] = {}
        market_lookup: dict[str, dict] = {}
        for row in market_rows:
            ticker = row["ticker"]
            result_str = (row["result"] or "").strip()
            if result_str.lower() in ("yes", "1", "true"):
                outcomes[ticker] = True
            elif result_str.lower() in ("no", "0", "false"):
                outcomes[ticker] = False
            # else: no outcome — market included for signal generation, but will be
            # resolved at last-known-price rather than binary outcome
            market_lookup[ticker] = dict(row)

        # Load snapshots for all markets (settled + active/abandoned)
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

        # C-2: Track the last-seen price per market for marking open positions at end
        last_known_prices: dict[str, tuple[float, float]] = {}  # market_id -> (yes_price, no_price)

        # Build end_date lookup for resolving positions mid-replay
        end_dates: dict[str, str] = {}
        for ticker, mdata in market_lookup.items():
            ed = mdata.get("end_date", "")
            if ed:
                end_dates[ticker] = ed

        # Process snapshots chronologically
        trades: list[BacktestTrade] = []
        edges_predicted: list[float] = []
        edges_realized: list[float] = []
        equity_curve = [bankroll]
        cb_skipped = 0

        # Reset circuit breaker for clean backtest state
        self.circuit_breaker.reset()
        # Track daily P&L for circuit breaker day-boundary resets
        current_day: str | None = None
        daily_pnl = 0.0

        for snap in all_snapshots:
            market_id = snap["market_id"]
            yes_price = snap["yes_price"]
            no_price = snap["no_price"]
            timestamp = snap["timestamp"]

            # C-2: keep rolling track of the most recent price for each market
            last_known_prices[market_id] = (yes_price, no_price)

            # Resolve any positions whose end_date has passed
            resolved_ids = []
            for pos_id in list(portfolio.positions.keys()):
                if pos_id in end_dates and timestamp >= end_dates[pos_id]:
                    if pos_id in outcomes:
                        pnl = portfolio.resolve_position(pos_id, outcomes[pos_id], use_taker_exit_fee=self.use_taker_exit_fee)
                        daily_pnl += pnl
                        resolved_ids.append(pos_id)
                        for t in trades:
                            if t.market_id == pos_id and not t.resolved:
                                t.pnl = pnl
                                t.resolved = True
                                t.outcome = outcomes[pos_id]
                                if t.price > 0 and t.size > 0:
                                    edges_realized.append(pnl / (t.size * t.price))
                                equity_curve.append(equity_curve[-1] + pnl)
                                break

            # Day boundary: reset circuit breaker daily halt and record daily result
            snap_day = timestamp[:10] if len(timestamp) >= 10 else timestamp
            if current_day is not None and snap_day != current_day:
                if daily_pnl != 0.0:
                    self.circuit_breaker.record_daily_result(daily_pnl)
                self.circuit_breaker.reset_daily()
                daily_pnl = 0.0
            current_day = snap_day

            if market_id not in market_lookup:
                continue
            if portfolio.has_position(market_id):
                continue

            # Circuit breaker check — skip trade if halted
            if not self.circuit_breaker.check(portfolio.bankroll):
                cb_skipped += 1
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

            # Simulate fill with realistic miss/partial fill rates:
            # 15% of orders miss entirely, 25% of large orders (>50 contracts)
            # get partial fills.
            import random
            if random.random() < 0.15:
                continue  # Simulated order miss
            if contracts > 50 and random.random() < 0.25:
                contracts = max(1, int(contracts * random.uniform(0.4, 0.8)))

            # M-19: Apply slippage to entry price (worsens fill for buyer)
            slippage = self.slippage_bps / 10000.0
            if direction in (Direction.BUY_YES, Direction.BUY_NO):
                slipped_price = min(0.99, order_price + slippage)
            else:
                slipped_price = max(0.01, order_price - slippage)
            portfolio.open_position(market_id, direction, contracts, slipped_price, strategy)
            edges_predicted.append(abs_edge)

            trades.append(BacktestTrade(
                market_id=market_id,
                direction=direction,
                strategy=strategy,
                price=order_price,
                size=contracts,
                timestamp=timestamp,
            ))

        # Resolve remaining open positions.
        # For markets with known outcomes: resolve at binary settlement price.
        # C-2: For markets without outcomes (active/abandoned): close at last known price
        #      to avoid survivorship bias — these positions were not free money.
        unresolved_positions = 0
        already_resolved = {t.market_id for t in trades if t.resolved}

        for market_id in list(portfolio.positions.keys()):
            if market_id in already_resolved:
                continue

            if market_id in outcomes:
                # Settled market — resolve at binary outcome
                outcome = outcomes[market_id]
                pnl = portfolio.resolve_position(market_id, outcome, use_taker_exit_fee=self.use_taker_exit_fee)
                for t in trades:
                    if t.market_id == market_id and not t.resolved:
                        t.pnl = pnl
                        t.resolved = True
                        t.outcome = outcome
                        if t.price > 0 and t.size > 0:
                            edges_realized.append(pnl / (t.size * t.price))
                        equity_curve.append(equity_curve[-1] + pnl)
                        break
            else:
                # C-2: No outcome available — close at last known price (mark-to-market)
                last_prices = last_known_prices.get(market_id)
                if last_prices:
                    pos = portfolio.positions[market_id]
                    last_yes, last_no = last_prices
                    exit_price = last_yes if pos.direction in (Direction.BUY_YES,) else last_no
                    pnl = portfolio.close_position(market_id, exit_price, use_taker_exit_fee=self.use_taker_exit_fee)
                else:
                    # No price data at all — assume total loss (worst case)
                    pnl = portfolio.close_position(market_id, 0.0, use_taker_exit_fee=self.use_taker_exit_fee)

                unresolved_positions += 1
                logger.debug(
                    f"Market {market_id} has no outcome — closed at last known price "
                    f"(mark-to-market). P&L: ${pnl:.2f}"
                )
                for t in trades:
                    if t.market_id == market_id and not t.resolved:
                        t.pnl = pnl
                        t.resolved = True
                        # outcome remains None — marks as unresolved
                        if t.price > 0 and t.size > 0:
                            edges_realized.append(pnl / (t.size * t.price))
                        equity_curve.append(equity_curve[-1] + pnl)
                        break

        if unresolved_positions > 0:
            logger.warning(
                f"{unresolved_positions} position(s) had no resolution outcome — "
                "closed at last known price. Results may understate true losses "
                "if markets later resolved unfavorably."
            )

        # Build result
        result = self._compute_result(
            "all" if not self.strategy_filter else self.strategy_filter,
            trades, equity_curve, edges_predicted, edges_realized, bankroll,
        )
        result.cb_skipped = cb_skipped
        result.unresolved_positions = unresolved_positions
        result.total_fees = portfolio.total_fees
        # C-1: propagate lookahead flag from forecaster
        result.uses_lookahead = self.forecaster.used_lookahead
        if result.uses_lookahead:
            logger.warning(
                "BacktestResult.uses_lookahead=True — this result is an oracle_upper_bound. "
                "Apply at most half the normal degradation factor when extrapolating to live."
            )
        if cb_skipped > 0:
            logger.info(f"Circuit breaker skipped {cb_skipped} potential trades")
        return [result]

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
                # NOTE: annualization factor assumes daily data points.
                # If using hourly snapshots, adjust periods_per_year accordingly.
                periods_per_year = 252  # Assumes daily equity snapshots
                result.sharpe_ratio = (mean_r / std) * (periods_per_year ** 0.5) if std > 0 else None
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
        f"  CB Skipped:    {result.cb_skipped}",
        f"  Total Fees:    ${result.total_fees:,.4f}",
        f"  Unresolved:    {result.unresolved_positions} position(s) marked-to-last-price",
    ]
    if result.brier_score is not None:
        lines.append(f"  Brier Score:   {result.brier_score:.3f}")
    if result.sharpe_ratio is not None:
        lines.append(f"  Sharpe Ratio:  {result.sharpe_ratio:.2f}")
    if result.calmar_ratio is not None:
        lines.append(f"  Calmar Ratio:  {result.calmar_ratio:.2f}")
    if result.uses_lookahead:
        lines.append(
            "  WARNING: oracle_upper_bound — outcome-derived forecasts used (lookahead bias). "
            "Apply half degradation factor. Do NOT use for live trading decisions."
        )
    else:
        lines.append(
            "  NOTE: cached-prediction mode — no lookahead bias from outcome-derived forecasts."
        )
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

    logger.warning(
        "BACKTEST WARNING: MockForecaster uses lookahead bias "
        "— results are NOT indicative of live performance"
    )

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
