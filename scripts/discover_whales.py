"""Whale discovery script — identifies profitable traders for the whale basket.

Usage:
    python -m scripts.discover_whales

Outputs a whale_basket.yaml file to config/ with curated wallet entries.
This is a one-time/periodic script, not part of the trading loop.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from scripts.leaderboard import LeaderboardEntry, LeaderboardScraper


def main():
    """Discover and curate whale basket."""
    print("Whale Discovery Tool")
    print("=" * 40)
    print()
    print("Kalshi does not expose public user positions via API.")
    print("This tool helps you maintain a curated whale basket.")
    print()
    print("To build your basket:")
    print("1. Visit Kalshi leaderboard: https://kalshi.com/leaderboard")
    print("2. Identify top traders with 50+ trades and 55%+ win rate")
    print("3. Add them to config/whale_basket.yaml")
    print()

    # Create template if it doesn't exist
    basket_path = Path("config/whale_basket.yaml")
    if not basket_path.exists():
        template = {
            "wallets": [
                {
                    "username": "example_trader_1",
                    "alias": "Top Trader 1",
                    "win_rate": 0.62,
                    "total_pnl": 15000.0,
                    "total_trades": 250,
                    "categories": ["Politics", "Fed/Macro"],
                },
                {
                    "username": "example_trader_2",
                    "alias": "Top Trader 2",
                    "win_rate": 0.58,
                    "total_pnl": 8000.0,
                    "total_trades": 180,
                    "categories": ["Geopolitics", "Tech/AI"],
                },
            ],
        }
        basket_path.parent.mkdir(parents=True, exist_ok=True)
        with open(basket_path, "w") as f:
            yaml.dump(template, f, default_flow_style=False)
        print(f"Template created at {basket_path}")
        print("Edit this file with real trader data.")
    else:
        with open(basket_path) as f:
            data = yaml.safe_load(f) or {}
        wallets = data.get("wallets", [])
        print(f"Current basket: {len(wallets)} traders")
        for w in wallets:
            print(f"  - {w.get('alias', w.get('username', 'unknown'))}: "
                  f"WR={w.get('win_rate', 0):.0%}, P&L=${w.get('total_pnl', 0):,.0f}")


if __name__ == "__main__":
    main()
