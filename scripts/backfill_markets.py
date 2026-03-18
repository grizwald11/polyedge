"""Backfill markets — load historical market data from Kalshi API.

Usage:
    python -m scripts.backfill_markets [--status settled] [--limit 500] [--db data/markets.db]

Fetches markets from the Kalshi API and stores them in the local database
for backtesting and analysis. Supports fetching settled/closed markets
to build historical data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import MarketDiscovery
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


def main():
    parser = argparse.ArgumentParser(description="Backfill historical market data")
    parser.add_argument("--status", default="settled",
                        choices=["settled", "closed", "open", "active"],
                        help="Market status to fetch")
    parser.add_argument("--limit", type=int, default=500, help="Max markets to fetch")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
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

    stats = db.get_stats()
    print(f"DB totals: {stats.get('total_markets', 0)} markets, "
          f"{stats.get('active_markets', 0)} active")


if __name__ == "__main__":
    main()
