"""Backfill markets — load historical market data from Kalshi API.

Usage:
    python -m scripts.backfill_markets [--status settled] [--limit 500] [--db data/markets.db]
    python -m scripts.backfill_markets --snapshots --limit 100  # Also fetch price history

Fetches markets from the Kalshi API and stores them in the local database
for backtesting and analysis. Supports fetching settled/closed markets
to build historical data. With --snapshots, also fetches trade history
and generates hourly price snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import parse_market
from src.core.models import MarketSnapshot
from src.storage.database import Database


async def backfill(
    kalshi: KalshiClient,
    db: Database,
    status: str = "settled",
    limit: int = 500,
) -> int:
    """Fetch historical markets from Kalshi and store in DB.

    Args:
        kalshi: Authenticated Kalshi client
        db: Database to store markets
        status: Market status to fetch (settled, closed, open)
        limit: Maximum markets to fetch

    Returns:
        Number of markets stored
    """
    total_stored = 0
    cursor = None

    print(f"Fetching {status} markets (limit={limit})...")

    while total_stored < limit:
        batch_size = min(200, limit - total_stored)
        try:
            data = await kalshi.get_markets(
                limit=batch_size,
                cursor=cursor,
                status=status,
            )
        except Exception as e:
            print(f"API error: {e}")
            break

        raw_markets = data.get("markets", [])
        if not raw_markets:
            break

        cursor = data.get("cursor")

        # Parse and store
        for raw in raw_markets:
            try:
                market = parse_market(raw)
                if market:
                    db.upsert_market(market)
                    total_stored += 1
            except Exception as e:
                ticker = raw.get("ticker", "unknown")
                print(f"  Failed to parse {ticker}: {e}")

        print(f"  Stored {total_stored} markets so far...")

        if not cursor:
            break

    return total_stored


async def backfill_snapshots(
    kalshi: KalshiClient,
    db: Database,
    limit: int = 100,
) -> int:
    """Fetch trade history for settled markets and generate hourly snapshots.

    Reads settled markets from DB, fetches their trade history from Kalshi,
    and generates synthetic hourly price snapshots for backtesting.

    Args:
        kalshi: Authenticated Kalshi client
        db: Database with settled markets already loaded
        limit: Max markets to process

    Returns:
        Number of snapshots created
    """
    conn = db._get_conn()
    # Get settled markets that don't already have snapshots
    rows = conn.execute("""
        SELECT m.ticker FROM markets m
        WHERE (m.closed = 1 OR m.active = 0)
        AND NOT EXISTS (
            SELECT 1 FROM market_snapshots ms WHERE ms.market_id = m.ticker
        )
        ORDER BY m.volume_24h DESC
        LIMIT ?
    """, (limit,)).fetchall()

    tickers = [r["ticker"] for r in rows]
    if not tickers:
        print("No settled markets without snapshots found.")
        return 0

    total_snapshots = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            trades = await kalshi.get_market_history(ticker)
            if not trades:
                continue

            snapshots = _trades_to_hourly_snapshots(ticker, trades)
            for snap in snapshots:
                db.log_snapshot(snap)
            total_snapshots += len(snapshots)

            if i % 10 == 0:
                print(f"  Processed {i}/{len(tickers)} markets, {total_snapshots} snapshots...")

        except Exception as e:
            print(f"  Failed to get history for {ticker}: {e}")
            continue

    print(f"  Created {total_snapshots} snapshots for {len(tickers)} markets")
    return total_snapshots


def _trades_to_hourly_snapshots(
    ticker: str, trades: list[dict]
) -> list[MarketSnapshot]:
    """Convert a list of trades to hourly price snapshots.

    Groups trades by hour and uses the last trade price in each hour
    as the snapshot price.
    """
    if not trades:
        return []

    # Sort by timestamp
    sorted_trades = sorted(trades, key=lambda t: t.get("created_time", t.get("ts", "")))

    hourly: dict[str, list[dict]] = {}
    for trade in sorted_trades:
        ts_str = trade.get("created_time", trade.get("ts", ""))
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        hour_key = ts.strftime("%Y-%m-%dT%H:00:00")
        hourly.setdefault(hour_key, []).append(trade)

    snapshots = []
    for hour_key, hour_trades in sorted(hourly.items()):
        last = hour_trades[-1]
        # Extract price — try different field names from Kalshi API
        yes_price = 0.0
        for field in ["yes_price", "price", "last_price_dollars"]:
            val = last.get(field)
            if val is not None:
                try:
                    yes_price = float(val)
                    break
                except (ValueError, TypeError):
                    continue
        if yes_price <= 0:
            continue

        no_price = 1.0 - yes_price if 0 < yes_price < 1 else 0.0
        ts = datetime.fromisoformat(hour_key).replace(tzinfo=timezone.utc)

        snapshots.append(MarketSnapshot(
            market_id=ticker,
            timestamp=ts,
            yes_price=yes_price,
            no_price=no_price,
            spread=abs(yes_price - no_price) if yes_price > 0 else 0.0,
            volume_1h=float(len(hour_trades)),
        ))

    return snapshots


def generate_synthetic_snapshots(db: Database, limit: int = 500) -> int:
    """Generate synthetic hourly snapshots for settled markets.

    Since Kalshi doesn't retain trade history for settled markets, we generate
    synthetic price paths that move from an initial price toward the outcome.
    This gives the backtest engine price data to replay against.

    For each settled market, generates 24-72 hourly snapshots showing the price
    drifting from a randomized starting point toward the final outcome.
    """
    import random
    random.seed(42)  # Deterministic backtests for reproducibility

    conn = db._get_conn()
    rows = conn.execute("""
        SELECT m.ticker, m.result, m.end_date FROM markets m
        WHERE m.result != '' AND m.result IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM market_snapshots ms WHERE ms.market_id = m.ticker
        )
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("No settled markets without snapshots found.")
        return 0

    # Stagger markets across a 30-day window so the backtest can cycle capital.
    # Assign each market a virtual resolution time spread evenly across the window.
    base_time = datetime.now(timezone.utc) - timedelta(days=30)

    total = 0
    for idx, row in enumerate(rows):
        ticker = row["ticker"]
        result = row["result"].lower()

        if result not in ("yes", "no"):
            continue

        outcome_yes = result == "yes"

        # Spread end_dates across 30 days
        day_offset = (idx / max(len(rows) - 1, 1)) * 29  # 0 to 29 days
        end_dt = base_time + timedelta(days=day_offset, hours=random.randint(0, 23))

        # Update the market's end_date to match virtual resolution time
        conn.execute(
            "UPDATE markets SET end_date = ? WHERE ticker = ?",
            (end_dt.isoformat(), ticker),
        )

        # Generate 24-48 hourly snapshots ending at resolution
        num_hours = random.randint(24, 48)
        start_dt = end_dt - timedelta(hours=num_hours)

        # Starting yes_price: randomize around 0.40-0.60 (uncertain)
        start_price = random.uniform(0.30, 0.70)
        end_price = 0.95 if outcome_yes else 0.05

        for h in range(num_hours):
            # Linear interpolation with noise
            progress = h / max(num_hours - 1, 1)
            base_price = start_price + (end_price - start_price) * progress
            noise = random.gauss(0, 0.03 * (1 - progress))  # Less noise near end
            yes_price = max(0.01, min(0.99, base_price + noise))
            no_price = 1.0 - yes_price
            ts = start_dt + timedelta(hours=h)

            snap = MarketSnapshot(
                market_id=ticker,
                timestamp=ts,
                yes_price=round(yes_price, 4),
                no_price=round(no_price, 4),
                spread=round(abs(yes_price - no_price), 4),
                volume_1h=float(random.randint(5, 200)),
            )
            db.log_snapshot(snap)
            total += 1

    conn.commit()
    print(f"  Generated {total} synthetic snapshots for {len(rows)} markets")
    return total


def main():
    parser = argparse.ArgumentParser(description="Backfill historical market data")
    parser.add_argument("--status", default="settled",
                        choices=["settled", "closed", "open", "active"],
                        help="Market status to fetch")
    parser.add_argument("--limit", type=int, default=500, help="Max markets to fetch")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--snapshots", action="store_true",
                        help="Also fetch price history and create hourly snapshots")
    parser.add_argument("--synthetic", action="store_true",
                        help="Generate synthetic snapshots for settled markets (no API needed)")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(args.db)
    kalshi = KalshiClient(
        host=settings.kalshi.active_host,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    async def run():
        count = await backfill(kalshi, db, status=args.status, limit=args.limit)
        print(f"\nDone: {count} {args.status} markets stored in {args.db}")

        if args.snapshots:
            print("\nFetching price snapshots...")
            snap_count = await backfill_snapshots(kalshi, db, limit=args.limit)
            print(f"Created {snap_count} price snapshots")

    asyncio.run(run())

    if args.synthetic:
        print("\nGenerating synthetic snapshots...")
        snap_count = generate_synthetic_snapshots(db, limit=args.limit)
        print(f"Generated {snap_count} synthetic snapshots")

    stats = db.get_stats()
    print(f"DB totals: {stats.get('active_markets', 0)} active markets")


if __name__ == "__main__":
    main()
