"""PolyEdge — Main entry point.

Thin wrapper that imports from src.orchestrator modules:
  - src/orchestrator/startup.py  — component initialization and health checks
  - src/orchestrator/scan_cycle.py — the scan -> assess -> signal loop
  - src/orchestrator/trade_cycle.py — risk check -> size -> execute flow
  - src/orchestrator/lifecycle.py — shutdown, signal handling, daily reporting
"""

from __future__ import annotations

import asyncio

from src.orchestrator.lifecycle import main, run_trading_loop
from src.orchestrator.scan_cycle import scan_and_trade

# Re-export public API so existing imports (e.g. tests) continue to work.
from src.orchestrator.startup import setup_logging

__all__ = [
    "setup_logging",
    "scan_and_trade",
    "run_trading_loop",
    "main",
]

if __name__ == "__main__":
    asyncio.run(main())
