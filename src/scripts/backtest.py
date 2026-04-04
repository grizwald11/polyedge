"""Backtest Claude against already-resolved Kalshi markets for calibration data.

L-1: This is the LIVE-API backtester (calls Claude on settled markets).
See also scripts/backtest_engine.py for the offline replay engine (no API calls).

Fetches settled events from Kalshi, runs Claude's assessment pipeline blind
(without knowing the outcome), then scores predictions against actual results.

Usage:
    python -m src.scripts.backtest --limit 50 --delay 2
    python -m src.scripts.backtest --dry-run
    python -m src.scripts.backtest --limit 10 --delay 1 --db data/backtest.db
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, Optional

from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.news_researcher import NewsResearcher
from src.config import Settings, load_settings
from src.core.kalshi_client import KalshiClient
from src.core.market_discovery import parse_market
from src.core.models import Market, MarketToken
from src.scripts.calibration_report import format_report
from src.storage.database import Database

logger = logging.getLogger(__name__)


async def fetch_settled_events(
    kalshi: KalshiClient,
    max_events: int = 200,
) -> list[dict]:
    """Fetch settled events from Kalshi for backtesting.

    Returns raw market dicts tagged with event category (same format as
    MarketDiscovery.get_all_active_markets).
    """
    all_markets: list[dict] = []
    cursor = None
    pages = 0
    max_pages = (max_events // 200) + 1

    while pages < max_pages and len(all_markets) < max_events:
        remaining = max_events - len(all_markets)
        params: dict[str, Any] = {
            "limit": min(200, max(1, remaining)),
            "status": "settled",
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor

        data = await kalshi._request("GET", "/events", params=params)
        if data is None:
            break

        events = data.get("events", [])
        if not events:
            break

        for event in events:
            event_category = event.get("category", "")
            for m in event.get("markets", []):
                m["_event_category"] = event_category
                all_markets.append(m)

        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break

    logger.info(f"Fetched {len(all_markets)} settled markets from {pages} pages")
    return all_markets


def filter_backtestable_markets(raw_markets: list[dict]) -> list[dict]:
    """Filter to binary markets with a clear yes/no result and meaningful volume."""
    filtered = []
    for raw in raw_markets:
        # Must have a result
        result = raw.get("result", "")
        if result not in ("yes", "no"):
            continue

        # Must have a title/question
        title = raw.get("title") or raw.get("question", "")
        if not title:
            continue

        # Must have meaningful volume (at least 100 contracts)
        volume = 0.0
        for key in ("volume_fp", "volume", "volume_24h_fp", "volume_24h"):
            try:
                volume = max(volume, float(raw.get(key, 0)))
            except (ValueError, TypeError):
                continue  # Expected for missing/malformed fields, try next key
        if volume < 100:
            continue

        filtered.append(raw)

    logger.info(
        f"Filtered to {len(filtered)} backtestable markets "
        f"(from {len(raw_markets)} total)"
    )
    return filtered


def build_settled_market(raw: dict) -> Optional[Market]:
    """Build a Market object from a settled Kalshi market response.

    Uses the existing parse_market() but ensures the market has valid
    price data even though it's settled (uses last_price as fallback).
    """
    event_category = raw.get("_event_category", "")
    market = parse_market(raw, event_category=event_category)
    if market is None:
        return None

    # For settled markets, if prices are zero (book cleared), use last_price
    if market.yes_price == 0.0:
        last_price = 0.0
        for key in ("last_price_dollars", "last_price"):
            try:
                last_price = float(raw.get(key, 0))
                if last_price > 0:
                    break
            except (ValueError, TypeError):
                continue  # Expected for missing/malformed fields, try next key

        if last_price > 0:
            market.tokens = [
                MarketToken(
                    token_id=f"{market.ticker}_yes",
                    outcome="Yes",
                    price=last_price,
                ),
                MarketToken(
                    token_id=f"{market.ticker}_no",
                    outcome="No",
                    price=round(1.0 - last_price, 4),
                ),
            ]

    return market


def get_actual_outcome(raw: dict) -> bool:
    """Extract the actual outcome from a settled market. True = YES won."""
    return raw.get("result", "").lower() == "yes"


async def run_backtest(
    settings: Settings,
    db: Database,
    limit: int = 50,
    delay: float = 2.0,
    dry_run: bool = False,
) -> None:
    """Run blind Claude assessments on settled markets and calculate accuracy."""
    # Initialize clients
    kalshi = KalshiClient(
        host=settings.kalshi.active_host,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )

    try:
        # Step 1: Fetch settled events
        logger.info("Fetching settled events from Kalshi...")
        raw_markets = await fetch_settled_events(kalshi, max_events=200)

        # Step 2: Filter to backtestable markets
        backtestable = filter_backtestable_markets(raw_markets)

        if not backtestable:
            logger.info("No backtestable markets found.")
            return

        # Cap at limit
        backtestable = backtestable[:limit]

        # Dry run: just list markets and exit
        if dry_run:
            logger.info(f"\n{'='*60}")
            logger.info(f"  DRY RUN — {len(backtestable)} settled markets available")
            logger.info(f"{'='*60}\n")
            for i, raw in enumerate(backtestable, 1):
                title = raw.get("title") or raw.get("question", "?")
                result = raw.get("result", "?")
                ticker = raw.get("ticker", "?")
                volume = 0.0
                for key in ("volume_fp", "volume"):
                    try:
                        volume = max(volume, float(raw.get(key, 0)))
                    except (ValueError, TypeError):
                        continue  # Expected for missing/malformed fields
                logger.info(f"  {i:3d}. [{result.upper():>3s}] {ticker:<30s} vol={volume:>10,.0f}  {title[:60]}")
            logger.info(f"\nRun without --dry-run to backtest these markets.")
            return

        # Step 3: Initialize Claude forecaster and news researcher
        forecaster = ClaudeForecaster(settings)
        news_researcher = NewsResearcher(
            serper_api_key=settings.serper_api_key,
            searxng_url=settings.searxng_url,
            serper_url=settings.news.serper_url,
            staleness_thresholds=settings.news.staleness_thresholds,
        )

        # Step 4: Process each market
        logger.info(f"\nBacktesting {len(backtestable)} markets...")
        logger.info(f"{'='*60}\n")

        processed = 0
        errors = 0
        brier_sum = 0.0

        for i, raw in enumerate(backtestable, 1):
            ticker = raw.get("ticker", "?")
            title = (raw.get("title") or raw.get("question", "?"))[:60]
            actual_yes = get_actual_outcome(raw)
            actual_outcome_int = 1 if actual_yes else 0

            # Build Market object for Claude (blind — no outcome info)
            market = build_settled_market(raw)
            if market is None:
                logger.info(f"  [{i:3d}/{len(backtestable)}] SKIP  {ticker} — failed to parse")
                errors += 1
                continue

            try:
                # Get news context
                news_context = await news_researcher.get_context(market.question)

                # Run Claude assessment (blind — doesn't know the outcome)
                forecast = await forecaster.assess_market(
                    market=market,
                    news_context=news_context,
                )

                predicted_prob = forecast.probability
                market_price = market.yes_price

                # Calculate Brier score
                brier = (predicted_prob - actual_outcome_int) ** 2
                brier_sum += brier

                # Upsert market first (calibration_records has FK to markets)
                db.upsert_market(market)

                # Store prediction
                db.store_prediction(
                    market_ticker=ticker,
                    predicted_probability=predicted_prob,
                    predicted_side="YES" if predicted_prob > 0.5 else "NO",
                    market_price=market_price,
                    strategy="ai_probability",
                    confidence_low=forecast.confidence_low,
                    confidence_high=forecast.confidence_high,
                    market_question=market.question,
                )

                # Immediately resolve (we know the outcome)
                db.update_resolution(
                    market_id=ticker,
                    actual_outcome=actual_outcome_int,
                    brier_score=brier,
                )

                processed += 1
                correct = (predicted_prob > 0.5) == actual_yes
                status = "OK" if correct else "MISS"
                avg_brier = brier_sum / processed

                logger.info(
                    f"  [{i:3d}/{len(backtestable)}] {status:4s}  "
                    f"pred={predicted_prob:.0%} actual={'YES' if actual_yes else 'NO':>3s}  "
                    f"brier={brier:.3f}  avg={avg_brier:.3f}  "
                    f"{title}"
                )

            except Exception as e:
                errors += 1
                logger.info(f"  [{i:3d}/{len(backtestable)}] ERR   {ticker} — {e}")
                logger.exception(f"Error backtesting {ticker}")

            # Rate limit delay
            if i < len(backtestable):
                await asyncio.sleep(delay)

        # Step 5: Print summary and calibration report
        logger.info(f"\n{'='*60}")
        logger.info(f"  BACKTEST COMPLETE")
        logger.info(f"{'='*60}")
        logger.info(f"  Processed: {processed}")
        logger.info(f"  Errors:    {errors}")
        if processed > 0:
            logger.info(f"  Avg Brier: {brier_sum / processed:.4f}")
        logger.info()

        # Generate full calibration report
        analyzer = CalibrationAnalyzer(db)
        logger.info(format_report(analyzer))

    finally:
        await kalshi.close()


def main():
    """Run backtesting pipeline: fetch settled markets, assess with Claude, score accuracy."""
    parser = argparse.ArgumentParser(
        description="Backtest Claude against settled Kalshi markets"
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of markets to backtest (default: 50)",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds between API calls to avoid rate limits (default: 2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Just list settled markets without running Claude",
    )
    parser.add_argument(
        "--db", default="data/markets.db",
        help="Database path (default: data/markets.db)",
    )
    args = parser.parse_args()

    # Load settings
    settings = load_settings()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )

    # Validate required keys (not needed for dry-run)
    if not args.dry_run:
        if not settings.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY not set in environment")
            sys.exit(1)

    # Initialize database
    db = Database(db_path=args.db, wal_mode=True)

    # Run
    asyncio.run(run_backtest(
        settings=settings,
        db=db,
        limit=args.limit,
        delay=args.delay,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
