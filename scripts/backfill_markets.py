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
from src.core.market_discovery import MarketDiscovery
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
    discovery = MarketDiscovery(kalshi)
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
                market = discovery.parse_market(raw)
                if market:
                    db.upsert_market(market)
                    total_stored += 1
            except Exception as e:
                ticker = raw.get("ticker", "unknown")
                print(f"  Failed to parse {ticker}: {e}")

        print(f"  Stored {total_stored} markets so far...")

        if not cursor:
            break

    await discovery.close()
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
        WHERE m.closed = 1 OR m.active = 0
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


def main():
    parser = argparse.ArgumentParser(description="Backfill historical market data")
    parser.add_argument("--status", default="settled",
                        choices=["settled", "closed", "open", "active"],
                        help="Market status to fetch")
    parser.add_argument("--limit", type=int, default=500, help="Max markets to fetch")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    parser.add_argument("--snapshots", action="store_true",
                        help="Also fetch price history and create hourly snapshots")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(args.db)
    kalshi = KalshiClient(
        host=settings.kalshi.active_host,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    count = asyncio.run(backfill(kalshi, db, status=args.status, limit=args.limit))
    print(f"\nDone: {count} {args.status} markets stored in {args.db}")

    if args.snapshots:
        print("\nFetching price snapshots...")
        snap_count = asyncio.run(backfill_snapshots(kalshi, db, limit=args.limit))
        print(f"Created {snap_count} price snapshots")

    stats = db.get_stats()
    print(f"DB totals: {stats.get('active_markets', 0)} active markets")


if __name__ == "__main__":
    main()
