"""Export trade data in tax-reporting-friendly CSV format (Schedule D / Form 8949).

Usage:
    python -m scripts.export_tax_report --year 2026
    python -m scripts.export_tax_report --year 2026 --output trades_2026.csv
    python -m scripts.export_tax_report --start 2026-01-01 --end 2026-06-30
    python -m scripts.export_tax_report --year 2026 --db data/markets.db --live-only

Queries all realized trades from the database, computes cost basis, proceeds,
and gains/losses per closed position, and writes a CSV suitable for
Schedule D / Form 8949 filing. All prediction-market contracts are treated
as short-term capital gains (held < 1 year) unless the holding period
exceeds 365 days.

Audit item: M-14
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.database import Database


@dataclass
class TaxLot:
    """A matched buy/sell pair for tax reporting."""

    market_id: str
    description: str
    side: str  # "YES" or "NO" — the token side
    strategy: str
    contracts: float
    cost_basis: float  # Total cost (price * size + fee)
    proceeds: float  # Total proceeds (price * size - fee)
    date_acquired: str  # ISO date
    date_sold: str  # ISO date
    gain_loss: float = 0.0
    term: str = "Short-term"  # "Short-term" or "Long-term"

    def __post_init__(self):
        self.gain_loss = self.proceeds - self.cost_basis
        # Determine holding period
        try:
            acquired = datetime.fromisoformat(self.date_acquired)
            sold = datetime.fromisoformat(self.date_sold)
            days_held = (sold - acquired).days
            self.term = "Long-term" if days_held > 365 else "Short-term"
        except (ValueError, TypeError):
            self.term = "Short-term"


def query_trades(
    db: Database,
    start_date: str | None = None,
    end_date: str | None = None,
    live_only: bool = False,
) -> list[dict]:
    """Query trades from the database within a date range.

    Returns trades ordered by timestamp ascending for FIFO matching.
    """
    conn = db._get_conn()

    conditions = []
    params: list = []

    if start_date:
        conditions.append("t.timestamp >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("t.timestamp < ?")
        params.append(end_date)
    if live_only:
        conditions.append("t.paper = 0")

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT
            t.id,
            t.order_id,
            t.market_id,
            t.token_id,
            t.side,
            t.price,
            t.size,
            t.fee,
            t.realized_pnl,
            t.strategy,
            t.paper,
            t.timestamp,
            COALESCE(m.question, t.market_id) as market_question
        FROM trades t
        LEFT JOIN markets m ON t.market_id = m.ticker
        {where_clause}
        ORDER BY t.timestamp ASC
    """

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def match_trades_fifo(trades: list[dict]) -> list[TaxLot]:
    """Match buy and sell trades using FIFO (First In, First Out) method.

    Groups trades by (market_id, token_id) and matches buys against sells
    in chronological order. Unmatched buys represent open positions and
    are not included in the tax report.
    """
    # Group trades by position key
    positions: dict[str, dict[str, list[dict]]] = {}
    for trade in trades:
        key = (trade["market_id"], trade["token_id"])
        if key not in positions:
            positions[key] = {"buys": [], "sells": []}
        if trade["side"] == "BUY":
            positions[key]["buys"].append(trade)
        else:  # SELL
            positions[key]["sells"].append(trade)

    tax_lots: list[TaxLot] = []

    for (market_id, token_id), pos in positions.items():
        buys = list(pos["buys"])  # Copy to avoid mutating
        sells = list(pos["sells"])

        buy_idx = 0
        buy_remaining = 0.0

        for sell in sells:
            sell_remaining = sell["size"]

            while sell_remaining > 1e-9 and buy_idx < len(buys):
                buy = buys[buy_idx]

                if buy_remaining <= 1e-9:
                    buy_remaining = buy["size"]

                match_size = min(buy_remaining, sell_remaining)

                # Proportional cost basis and proceeds
                buy_fee_share = buy["fee"] * (match_size / buy["size"]) if buy["size"] > 0 else 0
                sell_fee_share = sell["fee"] * (match_size / sell["size"]) if sell["size"] > 0 else 0

                cost_basis = round(buy["price"] * match_size + buy_fee_share, 4)
                proceeds = round(sell["price"] * match_size - sell_fee_share, 4)

                # Determine token side from token_id for description
                token_side = "YES" if "yes" in token_id.lower() else "NO"
                description = f"{sell.get('market_question', market_id)} ({token_side})"

                lot = TaxLot(
                    market_id=market_id,
                    description=description,
                    side=token_side,
                    strategy=sell.get("strategy", "unknown"),
                    contracts=match_size,
                    cost_basis=cost_basis,
                    proceeds=proceeds,
                    date_acquired=buy["timestamp"][:10] if buy["timestamp"] else "",
                    date_sold=sell["timestamp"][:10] if sell["timestamp"] else "",
                )
                tax_lots.append(lot)

                buy_remaining -= match_size
                sell_remaining -= match_size

                if buy_remaining <= 1e-9:
                    buy_idx += 1
                    buy_remaining = 0.0

    return tax_lots


def generate_settlement_lots(
    trades: list[dict],
    db: Database,
) -> list[TaxLot]:
    """Generate tax lots for positions that resolved (settled) rather than sold.

    For prediction markets, many positions are held to resolution. The
    'proceeds' for a resolved market is either $1.00/contract (YES resolved YES,
    NO resolved NO) or $0.00/contract (wrong side). We check the market's
    'result' field to determine the outcome.
    """
    # Find buy trades that have no corresponding sell
    positions: dict[tuple, list[dict]] = {}
    for trade in trades:
        key = (trade["market_id"], trade["token_id"])
        if key not in positions:
            positions[key] = {"buys": [], "sells": []}
        if trade["side"] == "BUY":
            positions[key]["buys"].append(trade)
        else:
            positions[key]["sells"].append(trade)

    tax_lots: list[TaxLot] = []

    for (market_id, token_id), pos in positions.items():
        total_bought = sum(t["size"] for t in pos["buys"])
        total_sold = sum(t["size"] for t in pos["sells"])
        unsold = total_bought - total_sold

        if unsold <= 1e-9:
            continue

        # Check if market has resolved
        market = db.get_market(market_id)
        if not market or not market.get("result"):
            continue  # Still open, skip

        result = market["result"].lower()
        token_side = "yes" if "yes" in token_id.lower() else "no"

        # Determine proceeds per contract based on resolution
        if result == "yes":
            proceeds_per_contract = 1.0 if token_side == "yes" else 0.0
        elif result == "no":
            proceeds_per_contract = 1.0 if token_side == "no" else 0.0
        else:
            continue  # Ambiguous result, skip

        # Use end_date as the settlement date
        settlement_date = market.get("end_date", "")
        if settlement_date:
            settlement_date = settlement_date[:10]

        # Match remaining buys FIFO for the unsold portion
        remaining = unsold
        buy_idx = 0
        sell_offset = total_sold  # Skip buys already matched to sells

        # Walk through buys, skipping those already matched
        skip = total_sold
        for buy in pos["buys"]:
            if skip >= buy["size"]:
                skip -= buy["size"]
                continue

            available = buy["size"] - skip
            skip = 0
            match_size = min(available, remaining)

            buy_fee_share = buy["fee"] * (match_size / buy["size"]) if buy["size"] > 0 else 0
            cost_basis = round(buy["price"] * match_size + buy_fee_share, 4)
            proceeds = round(proceeds_per_contract * match_size, 4)

            description = f"{buy.get('market_question', market_id)} ({token_side.upper()}) [settled]"

            lot = TaxLot(
                market_id=market_id,
                description=description,
                side=token_side.upper(),
                strategy=buy.get("strategy", "unknown"),
                contracts=match_size,
                cost_basis=cost_basis,
                proceeds=proceeds,
                date_acquired=buy["timestamp"][:10] if buy["timestamp"] else "",
                date_sold=settlement_date,
            )
            tax_lots.append(lot)

            remaining -= match_size
            if remaining <= 1e-9:
                break

    return tax_lots


def write_csv(tax_lots: list[TaxLot], output_path: str) -> None:
    """Write tax lots to CSV in Form 8949 format."""
    headers = [
        "Description",
        "Date Acquired",
        "Date Sold",
        "Proceeds",
        "Cost Basis",
        "Gain/Loss",
        "Term",
        "Market ID",
        "Strategy",
        "Contracts",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for lot in sorted(tax_lots, key=lambda l: l.date_sold):
            writer.writerow([
                lot.description,
                lot.date_acquired,
                lot.date_sold,
                f"{lot.proceeds:.4f}",
                f"{lot.cost_basis:.4f}",
                f"{lot.gain_loss:.4f}",
                lot.term,
                lot.market_id,
                lot.strategy,
                f"{lot.contracts:.4f}",
            ])


def compute_summary(tax_lots: list[TaxLot]) -> dict:
    """Compute summary statistics for the tax report."""
    if not tax_lots:
        return {
            "total_proceeds": 0.0,
            "total_cost_basis": 0.0,
            "total_gain_loss": 0.0,
            "short_term_gain_loss": 0.0,
            "long_term_gain_loss": 0.0,
            "num_lots": 0,
            "num_winning": 0,
            "num_losing": 0,
        }

    total_proceeds = sum(lot.proceeds for lot in tax_lots)
    total_cost_basis = sum(lot.cost_basis for lot in tax_lots)
    total_gain_loss = sum(lot.gain_loss for lot in tax_lots)
    short_term = sum(lot.gain_loss for lot in tax_lots if lot.term == "Short-term")
    long_term = sum(lot.gain_loss for lot in tax_lots if lot.term == "Long-term")
    num_winning = sum(1 for lot in tax_lots if lot.gain_loss > 0)
    num_losing = sum(1 for lot in tax_lots if lot.gain_loss < 0)

    return {
        "total_proceeds": round(total_proceeds, 4),
        "total_cost_basis": round(total_cost_basis, 4),
        "total_gain_loss": round(total_gain_loss, 4),
        "short_term_gain_loss": round(short_term, 4),
        "long_term_gain_loss": round(long_term, 4),
        "num_lots": len(tax_lots),
        "num_winning": num_winning,
        "num_losing": num_losing,
    }


def print_summary(summary: dict, year: str | None = None) -> None:
    """Print a human-readable summary to stdout."""
    period = f"Tax Year {year}" if year else "Selected Period"
    print(f"\n{'='*60}")
    print(f"  PolyEdge Tax Report — {period}")
    print(f"{'='*60}")
    print(f"  Total Lots:           {summary['num_lots']}")
    print(f"  Winning / Losing:     {summary['num_winning']} / {summary['num_losing']}")
    print(f"  Total Proceeds:       ${summary['total_proceeds']:,.4f}")
    print(f"  Total Cost Basis:     ${summary['total_cost_basis']:,.4f}")
    print(f"  Net Gain/Loss:        ${summary['total_gain_loss']:,.4f}")
    print(f"    Short-term:         ${summary['short_term_gain_loss']:,.4f}")
    print(f"    Long-term:          ${summary['long_term_gain_loss']:,.4f}")
    print(f"{'='*60}\n")


def export_tax_report(
    db_path: str,
    year: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    output: str | None = None,
    live_only: bool = False,
    include_settlements: bool = True,
) -> tuple[list[TaxLot], dict]:
    """Main entry point: query trades, match, and export.

    Returns (tax_lots, summary) for programmatic use.
    """
    db = Database(db_path=db_path, wal_mode=False)

    # Resolve date range
    if year and not start_date:
        start_date = f"{year}-01-01"
    if year and not end_date:
        end_date = f"{int(year) + 1}-01-01"

    trades = query_trades(db, start_date=start_date, end_date=end_date, live_only=live_only)

    # FIFO matching for buy/sell pairs
    tax_lots = match_trades_fifo(trades)

    # Also include positions that settled (resolved) without a sell
    if include_settlements:
        settlement_lots = generate_settlement_lots(trades, db)
        tax_lots.extend(settlement_lots)

    summary = compute_summary(tax_lots)

    if output:
        write_csv(tax_lots, output)
        print(f"Wrote {len(tax_lots)} tax lots to {output}")

    print_summary(summary, year=year)

    return tax_lots, summary


def main():
    parser = argparse.ArgumentParser(
        description="Export PolyEdge trades for tax reporting (Schedule D / Form 8949)",
    )
    parser.add_argument(
        "--year",
        type=str,
        default=None,
        help="Tax year to export (e.g., 2026). Sets start/end to Jan 1 - Dec 31.",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD). Overrides --year start.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date (YYYY-MM-DD, exclusive). Overrides --year end.",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output CSV file path. If omitted, only prints summary.",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/markets.db",
        help="Path to SQLite database (default: data/markets.db).",
    )
    parser.add_argument(
        "--live-only",
        action="store_true",
        default=False,
        help="Only include live trades (exclude paper trades).",
    )
    parser.add_argument(
        "--no-settlements",
        action="store_true",
        default=False,
        help="Exclude settled/resolved positions (only include explicit sell trades).",
    )

    args = parser.parse_args()

    if not args.year and not args.start:
        parser.error("Must specify either --year or --start (and optionally --end)")

    # Allow --start/--end to override --year
    start = args.start if args.start else None
    end = args.end if args.end else None

    export_tax_report(
        db_path=args.db_path,
        year=args.year,
        start_date=start,
        end_date=end,
        output=args.output,
        live_only=args.live_only,
        include_settlements=not args.no_settlements,
    )


if __name__ == "__main__":
    main()
