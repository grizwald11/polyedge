"""PolyEdge — Main orchestrator.

Initializes all components and runs the market scanning loop.
Phase 1: Scanner only. Later phases add strategies, execution, and alerts.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from src.config import load_settings
from src.core.gamma_client import GammaClient
from src.core.polymarket_client import PolymarketClient
from src.data.market_scanner import MarketScanner
from src.storage.database import Database


def setup_logging(level: str = "INFO", log_file: str = "data/logs/polyedge.log"):
    """Configure structured logging to both console and file."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(console)

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def run_scanner_loop(scanner: MarketScanner, interval: int):
    """Run the market scanner on a loop."""
    logger = logging.getLogger("polyedge.main")

    while True:
        try:
            markets = await scanner.run_scan_cycle()
            stats = scanner.db.get_stats()
            logger.info(
                f"DB stats: {stats['active_markets']} markets, "
                f"{stats['total_signals']} signals, "
                f"{stats['total_trades']} trades, "
                f"P&L: ${stats['total_pnl']:.2f}"
            )
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Scan cycle failed: {e}", exc_info=True)

        logger.info(f"Next scan in {interval} seconds...")
        await asyncio.sleep(interval)


async def main():
    """Main entry point."""
    # Load config
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.file)
    logger = logging.getLogger("polyedge.main")

    logger.info("=" * 60)
    logger.info("PolyEdge Starting")
    logger.info(f"  Mode: {settings.trading.mode}")
    logger.info(f"  Bankroll: ${settings.trading.bankroll:,.2f}")
    logger.info(f"  Scan interval: {settings.scanning.interval_seconds}s")
    logger.info(f"  Min volume: ${settings.scanning.min_volume_24h:,.0f}")
    logger.info("=" * 60)

    # Initialize components
    db = Database(settings.database.path, settings.database.wal_mode)
    gamma = GammaClient(settings.polymarket.gamma_host)

    # Initialize CLOB client (may not have credentials in Phase 1)
    clob = PolymarketClient(
        host=settings.polymarket.clob_host,
        chain_id=settings.polymarket.chain_id,
        private_key=settings.private_key,
        funder=settings.funder_address,
        signature_type=settings.polymarket.signature_type,
    )

    # Check CLOB health
    healthy = await clob.health_check()
    if healthy:
        logger.info("CLOB API: healthy")
    else:
        logger.warning("CLOB API: unreachable (continuing with Gamma API only)")

    # Check balance if authenticated
    if settings.private_key:
        balance = await clob.get_balance()
        if balance is not None:
            logger.info(f"Wallet balance: ${balance:,.2f} USDC")
        else:
            logger.warning("Could not fetch balance (auth may not be configured)")

    # Create scanner
    scanner = MarketScanner(gamma, db, settings)

    # Run initial scan
    logger.info("Running initial market scan...")
    markets = await scanner.run_scan_cycle()
    logger.info(f"Initial scan found {len(markets)} qualifying markets")

    if not markets:
        logger.warning("No qualifying markets found. Check your filter settings.")

    # Print top 10 markets
    for i, m in enumerate(markets[:10], 1):
        logger.info(
            f"  #{i:2d} [{m.category.value:12s}] "
            f"YES={m.yes_price:.2f} NO={m.no_price:.2f} "
            f"vol=${m.volume_24h:>10,.0f} | "
            f"{m.question[:65]}"
        )

    # Enter scanning loop
    logger.info(f"\nEntering scan loop (every {settings.scanning.interval_seconds}s)...")
    try:
        await run_scanner_loop(scanner, settings.scanning.interval_seconds)
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await gamma.close()
        logger.info("PolyEdge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
