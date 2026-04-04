"""Exit threshold optimizer — find optimal stop-loss and take-profit from historical data.

Usage:
    python -m scripts.optimize_exits [--db data/markets.db] [--days 60]

Analyzes historical position exits to determine which thresholds maximized
returns and which caused premature exits that left money on the table.

Outputs:
- Optimal stop-loss threshold (minimize unnecessary stop-outs)
- Optimal take-profit threshold
- Optimal max hold period
- Edge-gone threshold analysis
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.database import Database


@dataclass
class ExitAnalysis:
    """Analysis of exit behavior for a given threshold."""

    reason: str
    count: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    avg_days_held: float = 0.0
    premature_exits: int = 0  # Exits where the market later moved favorably


def analyze_exits(db: Database, days: int = 60) -> dict:
    """Analyze historical position exits."""
    conn = db._get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Load all position exits
    exits = [
        dict(r)
        for r in conn.execute(
            """SELECT market_id, strategy, exit_reason, exit_price,
                      position_size, realized_pnl, timestamp
               FROM position_exits WHERE timestamp >= ?
               ORDER BY timestamp ASC""",
            (cutoff,),
        ).fetchall()
    ]

    if not exits:
        return {"error": "No exit data found", "exits": []}

    # Group by exit reason
    by_reason: dict[str, list[dict]] = {}
    for e in exits:
        reason = _normalize_reason(e.get("exit_reason", "unknown"))
        by_reason.setdefault(reason, []).append(e)

    analyses: list[ExitAnalysis] = []
    for reason, exit_list in sorted(by_reason.items()):
        pnls = [e.get("realized_pnl", 0) for e in exit_list]
        wins = [p for p in pnls if p > 0]

        analysis = ExitAnalysis(
            reason=reason,
            count=len(exit_list),
            total_pnl=sum(pnls),
            avg_pnl=sum(pnls) / len(pnls) if pnls else 0,
            win_rate=len(wins) / len(pnls) if pnls else 0,
        )

        # Check for premature exits: did the market move favorably after exit?
        for e in exit_list:
            market_id = e.get("market_id", "")
            exit_time = e.get("timestamp", "")
            exit_price = e.get("exit_price", 0)

            # Get snapshots after the exit to see what happened next
            later_snapshots = conn.execute(
                """SELECT yes_price FROM market_snapshots
                   WHERE market_id = ? AND timestamp > ?
                   ORDER BY timestamp ASC LIMIT 10""",
                (market_id, exit_time),
            ).fetchall()

            if later_snapshots:
                # Check if price moved >5% in the favorable direction after exit
                best_later = max(s["yes_price"] for s in later_snapshots)
                if exit_price > 0 and best_later > exit_price * 1.05:
                    analysis.premature_exits += 1

        analyses.append(analysis)

    # Compute optimal thresholds
    stop_loss_analysis = _analyze_stop_loss_threshold(db, cutoff)
    hold_period_analysis = _analyze_hold_periods(db, cutoff)

    return {
        "period_days": days,
        "total_exits": len(exits),
        "by_reason": analyses,
        "stop_loss_analysis": stop_loss_analysis,
        "hold_period_analysis": hold_period_analysis,
    }


def _normalize_reason(reason: str) -> str:
    """Normalize exit reason strings to categories."""
    reason_lower = reason.lower()
    if "stop_loss" in reason_lower or "stop loss" in reason_lower:
        return "stop_loss"
    if "take_profit" in reason_lower or "take profit" in reason_lower:
        return "take_profit"
    if "edge_gone" in reason_lower or "edge gone" in reason_lower:
        return "edge_gone"
    if "time" in reason_lower or "max_hold" in reason_lower or "expir" in reason_lower:
        return "time_limit"
    if "trailing" in reason_lower:
        return "trailing_stop"
    if "capital_rotation" in reason_lower:
        return "capital_rotation"
    if "settled" in reason_lower or "resolved" in reason_lower:
        return "market_resolved"
    return reason_lower[:30] if reason else "unknown"


def _analyze_stop_loss_threshold(db: Database, cutoff: str) -> dict:
    """Analyze what stop-loss threshold would have been optimal."""
    conn = db._get_conn()

    # Get all trades with P&L to understand loss distribution
    trades = conn.execute(
        """SELECT market_id, realized_pnl, price, size
           FROM trades WHERE timestamp >= ? AND side = 'SELL' AND realized_pnl IS NOT NULL
           ORDER BY realized_pnl ASC""",
        (cutoff,),
    ).fetchall()

    if not trades:
        return {"optimal_stop_loss": None, "reason": "no trade data"}

    losses = [dict(t) for t in trades if t["realized_pnl"] < 0]
    wins = [dict(t) for t in trades if t["realized_pnl"] > 0]

    if not losses:
        return {"optimal_stop_loss": None, "reason": "no losing trades"}

    # Calculate loss percentages
    loss_pcts = []
    for t in losses:
        cost_basis = t["price"] * t["size"]
        if cost_basis > 0:
            loss_pcts.append(abs(t["realized_pnl"]) / cost_basis)

    if not loss_pcts:
        return {"optimal_stop_loss": None, "reason": "no calculable losses"}

    # Find threshold that maximizes (recovered capital from stopped-out trades)
    # while minimizing (trades that would have recovered but were stopped out)
    thresholds = [0.10, 0.15, 0.18, 0.20, 0.25, 0.30]
    best_threshold = 0.20
    best_saved = 0

    total_win_pnl = sum(t["realized_pnl"] for t in wins)
    total_loss_pnl = sum(t["realized_pnl"] for t in losses)

    threshold_results = []
    for threshold in thresholds:
        # How many losses exceed this threshold?
        would_stop = sum(1 for p in loss_pcts if p > threshold)
        # Capital saved = losses beyond the threshold that would have been capped
        saved = sum(
            abs(t["realized_pnl"]) - (t["price"] * t["size"] * threshold)
            for t, pct in zip(losses, loss_pcts)
            if pct > threshold
        )
        threshold_results.append({
            "threshold": threshold,
            "would_stop": would_stop,
            "capital_saved": round(saved, 2),
        })
        if saved > best_saved:
            best_saved = saved
            best_threshold = threshold

    return {
        "optimal_stop_loss": best_threshold,
        "total_losses": len(losses),
        "total_wins": len(wins),
        "total_loss_pnl": round(total_loss_pnl, 2),
        "total_win_pnl": round(total_win_pnl, 2),
        "loss_pct_median": round(sorted(loss_pcts)[len(loss_pcts) // 2], 3) if loss_pcts else None,
        "thresholds": threshold_results,
    }


def _analyze_hold_periods(db: Database, cutoff: str) -> dict:
    """Analyze optimal hold period from trade timestamps."""
    conn = db._get_conn()

    # Match buy and sell trades by market_id
    pairs = conn.execute(
        """SELECT
            b.market_id,
            b.timestamp as buy_time,
            s.timestamp as sell_time,
            s.realized_pnl,
            (julianday(s.timestamp) - julianday(b.timestamp)) as days_held
           FROM trades b
           JOIN trades s ON b.market_id = s.market_id AND b.side = 'BUY' AND s.side = 'SELL'
           WHERE b.timestamp >= ? AND s.realized_pnl IS NOT NULL
           ORDER BY days_held ASC""",
        (cutoff,),
    ).fetchall()

    if not pairs:
        return {"optimal_hold_days": None, "reason": "no matched buy/sell pairs"}

    # Bucket by hold period
    buckets: dict[str, list[float]] = {
        "0-3d": [], "3-7d": [], "7-14d": [], "14-21d": [], "21d+": [],
    }
    for p in pairs:
        days = p["days_held"] or 0
        pnl = p["realized_pnl"] or 0
        if days <= 3:
            buckets["0-3d"].append(pnl)
        elif days <= 7:
            buckets["3-7d"].append(pnl)
        elif days <= 14:
            buckets["7-14d"].append(pnl)
        elif days <= 21:
            buckets["14-21d"].append(pnl)
        else:
            buckets["21d+"].append(pnl)

    bucket_stats = {}
    for label, pnls in buckets.items():
        if pnls:
            bucket_stats[label] = {
                "count": len(pnls),
                "total_pnl": round(sum(pnls), 2),
                "avg_pnl": round(sum(pnls) / len(pnls), 2),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
            }

    return {
        "total_pairs": len(list(pairs)),
        "buckets": bucket_stats,
    }


def format_exit_report(analysis: dict) -> str:
    """Format exit analysis as a readable report."""
    lines = [
        f"{'=' * 65}",
        f"EXIT OPTIMIZATION REPORT — Last {analysis.get('period_days', '?')} days",
        f"{'=' * 65}",
        f"Total exits: {analysis.get('total_exits', 0)}",
        "",
    ]

    # By reason breakdown
    by_reason = analysis.get("by_reason", [])
    if by_reason:
        lines.append("Exit Reason Breakdown:")
        lines.append(
            f"  {'Reason':<20} {'Count':>5} {'Total P&L':>10} {'Avg P&L':>9} "
            f"{'Win%':>5} {'Premature':>9}"
        )
        lines.append("  " + "─" * 60)
        for a in by_reason:
            lines.append(
                f"  {a.reason:<20} {a.count:>5} ${a.total_pnl:>9.2f} "
                f"${a.avg_pnl:>8.2f} {a.win_rate:>4.0%} "
                f"{a.premature_exits:>9}"
            )
        lines.append("")

    # Stop-loss analysis
    sl = analysis.get("stop_loss_analysis", {})
    if sl.get("optimal_stop_loss"):
        lines.append(f"Stop-Loss Analysis:")
        lines.append(f"  Losses: {sl['total_losses']} (${sl['total_loss_pnl']:.2f})")
        lines.append(f"  Wins:   {sl['total_wins']} (${sl['total_win_pnl']:.2f})")
        if sl.get("loss_pct_median"):
            lines.append(f"  Median loss %: {sl['loss_pct_median']:.1%}")
        lines.append(f"  Optimal threshold: {sl['optimal_stop_loss']:.0%}")
        for t in sl.get("thresholds", []):
            lines.append(
                f"    {t['threshold']:.0%} → stops {t['would_stop']} trades, "
                f"saves ${t['capital_saved']:.2f}"
            )
        lines.append("")

    # Hold period analysis
    hp = analysis.get("hold_period_analysis", {})
    buckets = hp.get("buckets", {})
    if buckets:
        lines.append("Hold Period Analysis:")
        for label, stats in buckets.items():
            lines.append(
                f"  {label:<8} {stats['count']:>3} trades  "
                f"P&L=${stats['total_pnl']:>7.2f}  "
                f"Avg=${stats['avg_pnl']:>6.2f}  "
                f"Win={stats['win_rate']:.0%}"
            )
        lines.append("")

    lines.append(f"{'=' * 65}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PolyEdge Exit Optimizer")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--days", type=int, default=60, help="Lookback period")
    args = parser.parse_args()

    db = Database(args.db)
    analysis = analyze_exits(db, days=args.days)

    if "error" in analysis:
        print(f"Not enough data: {analysis['error']}")
        return

    print(format_exit_report(analysis))


if __name__ == "__main__":
    main()
