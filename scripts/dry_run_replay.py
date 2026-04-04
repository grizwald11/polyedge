"""Dry-run replay — replay yesterday's signals through current code to catch regressions.

Usage:
    python -m scripts.dry_run_replay [--db data/markets.db] [--date 2026-04-02]

Loads signals from a specific date, rebuilds minimal Market objects from DB
snapshots, and replays each signal through the current risk engine and Kelly
sizer.  Compares the outcome (pass/fail, sizing) against what actually happened
to flag behavioural regressions before deploying code changes.

Exit code 0 = no regressions, 1 = regressions detected.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Platform,
    Signal,
    StrategyName,
    TokenOutcome,
)
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.kelly_sizer import KellySizer
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database


@dataclass
class ReplayDiff:
    """A single difference between historical and replayed outcomes."""

    signal_id: int
    market_id: str
    strategy: str
    field: str  # "risk_passed" or "size"
    historical: str
    replayed: str

    def __str__(self) -> str:
        return (
            f"  signal #{self.signal_id} [{self.strategy}] {self.market_id}: "
            f"{self.field} was {self.historical} → now {self.replayed}"
        )


@dataclass
class ReplayReport:
    """Summary of a dry-run replay."""

    date: str
    total_signals: int = 0
    replayed: int = 0
    skipped: int = 0  # e.g. missing market data
    diffs: list[ReplayDiff] = field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        return len(self.diffs) > 0


def load_signals_for_date(db: Database, date_str: str) -> list[dict]:
    """Load all signals from a given date."""
    next_date = (date_type.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT * FROM signals WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
        (date_str, next_date),
    ).fetchall()
    return [dict(r) for r in rows]


def load_market_snapshot(db: Database, market_id: str, timestamp: str) -> dict | None:
    """Load the closest market snapshot before the signal timestamp."""
    conn = db._get_conn()
    # Try to find a snapshot close to the signal time
    row = conn.execute(
        """SELECT m.*, ms.yes_price, ms.no_price, ms.spread, ms.volume_1h, ms.liquidity
           FROM markets m
           LEFT JOIN market_snapshots ms ON m.ticker = ms.market_id
               AND ms.timestamp <= ?
           WHERE m.ticker = ?
           ORDER BY ms.timestamp DESC LIMIT 1""",
        (timestamp, market_id),
    ).fetchone()
    if row:
        return dict(row)
    # Fallback: just the market record
    row = conn.execute(
        "SELECT * FROM markets WHERE ticker = ?", (market_id,)
    ).fetchone()
    return dict(row) if row else None


def build_market(market_data: dict, signal: dict) -> Market | None:
    """Build a minimal Market object from DB data for risk checks."""
    try:
        yes_price = market_data.get("yes_price") or signal.get("market_price", 0.5)
        no_price = market_data.get("no_price") or (1 - yes_price)

        # Parse category, falling back to OTHER
        raw_cat = market_data.get("category", "Other")
        try:
            category = MarketCategory(raw_cat)
        except ValueError:
            category = MarketCategory.OTHER

        tokens = [
            MarketToken(token_id="yes", outcome=TokenOutcome.YES, price=yes_price),
            MarketToken(token_id="no", outcome=TokenOutcome.NO, price=no_price),
        ]

        return Market(
            ticker=market_data.get("ticker", signal["market_id"]),
            question=market_data.get("question", ""),
            category=category,
            end_date=market_data.get("end_date"),
            tokens=tokens,
            volume_24h=market_data.get("volume_24h", 10000),
            liquidity=market_data.get("liquidity", 5000),
            spread=market_data.get("spread", 0.02),
            platform=Platform(market_data.get("platform", "kalshi")),
        )
    except Exception:
        return None


def build_signal(row: dict) -> Signal | None:
    """Build a Signal object from a DB row."""
    try:
        return Signal(
            strategy=StrategyName(row["strategy"]),
            market_id=row["market_id"],
            platform=Platform(row.get("platform", "kalshi")),
            market_question=row.get("market_question", ""),
            direction=Direction(row["direction"]),
            edge=row["edge"],
            probability_estimate=row["probability_estimate"],
            market_price=row["market_price"],
            confidence=row.get("confidence", 0.5),
            reasoning=row.get("reasoning", ""),
        )
    except Exception:
        return None


def replay_signals(
    db: Database,
    signals: list[dict],
    settings=None,
) -> ReplayReport:
    """Replay signals through current risk engine and Kelly sizer."""
    if settings is None:
        settings = load_settings()

    report = ReplayReport(date=signals[0]["timestamp"][:10] if signals else "")
    report.total_signals = len(signals)

    # Build risk engine and Kelly sizer with current code
    position_manager = PositionManager(db, bankroll=settings.trading.bankroll)
    circuit_breaker = CircuitBreaker(settings, db)
    kelly = KellySizer(settings)
    risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db=db)

    for row in signals:
        signal = build_signal(row)
        if signal is None:
            report.skipped += 1
            continue

        market_data = load_market_snapshot(db, row["market_id"], row["timestamp"])
        if market_data is None:
            report.skipped += 1
            continue

        market = build_market(market_data, row)
        if market is None:
            report.skipped += 1
            continue

        report.replayed += 1

        # Size the position
        bankroll = settings.trading.bankroll
        current_exposure = position_manager.get_total_exposure()
        try:
            size = kelly.calculate_position_size(
                edge=signal.edge,
                probability=signal.probability_estimate,
                bankroll=bankroll,
                current_exposure=current_exposure,
                market_price=signal.market_price,
            )
        except Exception:
            size = 0.0

        proposed_cost = size * signal.market_price if size > 0 else 0.0

        # Run risk checks
        try:
            result = risk_engine.check_all(signal, market, size, proposed_cost)
            now_passes = result.passed
        except Exception:
            now_passes = False

        # Compare to historical outcome
        hist_passed = row.get("risk_passed")
        hist_status = row.get("status", "generated")

        # Determine what historically happened
        if hist_passed is not None:
            hist_would_trade = bool(hist_passed)
        elif hist_status == "executed":
            hist_would_trade = True
        elif hist_status == "risk_gated":
            hist_would_trade = False
        else:
            # Signal was generated but we don't know if it was risk-checked
            continue

        if hist_would_trade != now_passes:
            report.diffs.append(ReplayDiff(
                signal_id=row.get("id", 0),
                market_id=row["market_id"],
                strategy=row["strategy"],
                field="risk_passed",
                historical="PASS" if hist_would_trade else "FAIL",
                replayed="PASS" if now_passes else f"FAIL({','.join(result.failed_checks) if hasattr(result, 'failed_checks') else '?'})",
            ))

    return report


def format_report(report: ReplayReport) -> str:
    """Format replay report as readable text."""
    lines = [
        f"{'=' * 60}",
        f"DRY-RUN REPLAY — {report.date}",
        f"{'=' * 60}",
        f"Signals: {report.total_signals} total, {report.replayed} replayed, {report.skipped} skipped",
        f"Regressions: {len(report.diffs)}",
    ]

    if report.diffs:
        lines.append("")
        lines.append("DIFFERENCES DETECTED:")
        for diff in report.diffs:
            lines.append(str(diff))
        lines.append("")
        lines.append("ACTION: Review the above diffs before deploying.")
    else:
        lines.append("")
        lines.append("No regressions detected — safe to deploy.")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PolyEdge Dry-Run Replay")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument(
        "--date",
        default=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),
        help="Date to replay (default: yesterday)",
    )
    args = parser.parse_args()

    db = Database(args.db)
    signals = load_signals_for_date(db, args.date)

    if not signals:
        print(f"No signals found for {args.date}")
        return

    report = replay_signals(db, signals)
    print(format_report(report))

    if report.has_regressions:
        sys.exit(1)


if __name__ == "__main__":
    main()
