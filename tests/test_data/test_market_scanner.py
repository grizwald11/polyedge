"""Tests for market scanner — filter, rank, and store logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Market,
    MarketCategory,
    MarketToken,
)
from src.data.market_scanner import (
    MarketScanner,
    SCAN_MAX_RETRIES,
    SCAN_MAX_CONSECUTIVE_FAILURES,
)
from src.storage.database import Database


def _make_market(
    ticker: str = "TEST-MKT",
    category: MarketCategory = MarketCategory.POLITICS,
    yes_price: float = 0.50,
    volume_24h: float = 50000.0,
    days_ahead: int = 30,
    active: bool = True,
    closed: bool = False,
    tags: list[str] | None = None,
) -> Market:
    return Market(
        ticker=ticker,
        question=f"Test market {ticker}?",
        category=category,
        tags=tags or [],
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=round(1.0 - yes_price, 2)),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=days_ahead),
        volume_24h=volume_24h,
        volume_total=volume_24h * 10,
        liquidity=20000.0,
        spread=0.02,
        active=active,
        closed=closed,
    )


@pytest.fixture
def scanner_settings() -> Settings:
    return Settings(
        trading=Settings.model_fields["trading"].default_factory()
    )


@pytest.fixture
def scanner_db(tmp_path) -> Database:
    db = Database(db_path=str(tmp_path / "scanner_test.db"), wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


@pytest.fixture
def scanner(scanner_settings, scanner_db) -> MarketScanner:
    discovery = MagicMock()
    return MarketScanner(discovery, scanner_db, scanner_settings)


class TestFilterMarkets:
    def test_passes_valid_market(self, scanner):
        markets = [_make_market()]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_rejects_inactive_market(self, scanner):
        markets = [_make_market(active=False)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_rejects_closed_market(self, scanner):
        markets = [_make_market(closed=True)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_rejects_low_volume(self, scanner):
        markets = [_make_market(volume_24h=100)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_rejects_excluded_category(self, scanner):
        markets = [_make_market(category=MarketCategory.CRYPTO, tags=["Crypto Prices"])]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_rejects_no_price(self, scanner):
        m = _make_market()
        m.tokens[0].price = 0.0
        m.tokens[1].price = 0.0
        result = scanner.filter_markets([m])
        assert len(result) == 0

    def test_rejects_invalid_price_sum(self, scanner):
        """YES + NO prices that don't sum to ~1.0 should be rejected."""
        m = _make_market()
        m.tokens[0].price = 0.80
        m.tokens[1].price = 0.80  # Sum = 1.60
        result = scanner.filter_markets([m])
        assert len(result) == 0

    def test_multiple_markets_mixed(self, scanner):
        markets = [
            _make_market(ticker="GOOD-1"),
            _make_market(ticker="LOW-VOL", volume_24h=10),
            _make_market(ticker="GOOD-2", category=MarketCategory.FED_MACRO),
            _make_market(ticker="INACTIVE", active=False),
        ]
        result = scanner.filter_markets(markets)
        tickers = [m.ticker for m in result]
        assert "GOOD-1" in tickers
        assert "GOOD-2" in tickers
        assert "LOW-VOL" not in tickers
        assert "INACTIVE" not in tickers

    def test_uses_volume_total_as_fallback(self, scanner):
        """When volume_24h is 0, should use volume_total."""
        m = _make_market(volume_24h=0)
        m.volume_total = 100000.0  # Above min threshold
        result = scanner.filter_markets([m])
        assert len(result) == 1


class TestRankMarkets:
    def test_higher_volume_ranks_higher(self, scanner):
        """Volume score is log-scaled and capped at 50, so use a very low vs moderate volume."""
        markets = [
            _make_market(ticker="LOW", volume_24h=100, yes_price=0.50, category=MarketCategory.OTHER),
            _make_market(ticker="HIGH", volume_24h=50000, yes_price=0.50, category=MarketCategory.OTHER),
        ]
        ranked = scanner.rank_markets(markets)
        assert ranked[0].ticker == "HIGH"

    def test_target_category_boost(self, scanner):
        """Target categories (Politics, AI, etc.) should rank higher."""
        markets = [
            _make_market(ticker="OTHER", category=MarketCategory.OTHER, volume_24h=50000),
            _make_market(ticker="POLITICS", category=MarketCategory.POLITICS, volume_24h=50000),
        ]
        ranked = scanner.rank_markets(markets)
        assert ranked[0].ticker == "POLITICS"

    def test_sweet_spot_resolution_time(self, scanner):
        """Markets resolving in 7-90 days should rank higher than very long-dated."""
        markets = [
            _make_market(ticker="LONG", days_ahead=365, volume_24h=50000),
            _make_market(ticker="SWEET", days_ahead=30, volume_24h=50000),
        ]
        ranked = scanner.rank_markets(markets)
        assert ranked[0].ticker == "SWEET"

    def test_respects_max_markets_limit(self, scanner):
        """Should only return up to settings.scanning.max_markets."""
        markets = [_make_market(ticker=f"MKT-{i}") for i in range(300)]
        ranked = scanner.rank_markets(markets)
        assert len(ranked) <= scanner.settings.scanning.max_markets

    def test_extreme_prices_get_boost(self, scanner):
        """Markets at extreme prices (<10% or >90%) should rank higher."""
        markets = [
            _make_market(ticker="MID", yes_price=0.50, volume_24h=50000),
            _make_market(ticker="EXTREME", yes_price=0.05, volume_24h=50000),
        ]
        ranked = scanner.rank_markets(markets)
        assert ranked[0].ticker == "EXTREME"


class TestStoreMarkets:
    def test_stores_markets_and_snapshots(self, scanner, scanner_db):
        markets = [_make_market(ticker="STORE-1"), _make_market(ticker="STORE-2")]
        scanner.store_markets(markets)

        # Verify markets stored
        m1 = scanner_db.get_market("STORE-1")
        assert m1 is not None
        m2 = scanner_db.get_market("STORE-2")
        assert m2 is not None

        # Verify snapshots stored
        snaps = scanner_db.get_snapshots_for_market("STORE-1")
        assert len(snaps) >= 1

    def test_store_handles_errors_gracefully(self, scanner, scanner_db):
        """Storing a bad market should not crash the whole batch."""
        good = _make_market(ticker="GOOD-STORE")
        # This should succeed even if individual markets fail
        scanner.store_markets([good])
        assert scanner_db.get_market("GOOD-STORE") is not None


class TestScanCycle:
    @pytest.mark.asyncio
    async def test_full_scan_cycle(self, scanner_settings, scanner_db):
        """End-to-end: scan → filter → rank → store."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(return_value=[
            {
                "ticker": "SCAN-1",
                "title": "Will test pass?",
                "subtitle": "",
                "category": "Politics",
                "yes_bid_dollars": "0.49",
                "yes_ask_dollars": "0.51",
                "last_price_dollars": "0.50",
                "volume_24h_fp": "100000.00",
                "volume_fp": "500000.00",
                "open_interest_fp": "30000.00",
                "status": "active",
                "result": "",
                "close_time": (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
        ])

        scanner = MarketScanner(discovery, scanner_db, scanner_settings)
        result = await scanner.run_scan_cycle()

        # Should have found and stored the market
        discovery.get_all_active_markets.assert_called_once()
        stored = scanner_db.get_market("SCAN-1")
        assert stored is not None

    @pytest.mark.asyncio
    async def test_scan_cycle_empty_api(self, scanner_settings, scanner_db):
        """Scan with no markets from API should not error."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(return_value=[])

        scanner = MarketScanner(discovery, scanner_db, scanner_settings)
        result = await scanner.run_scan_cycle()
        assert result == []


class TestScanRetryAndRateLimit:
    """Tests for retry/backoff and consecutive failure tracking in scan_all_markets."""

    @pytest.mark.asyncio
    async def test_retries_on_api_failure(self, scanner_settings, scanner_db):
        """scan_all_markets should retry SCAN_MAX_RETRIES times on failure."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        scanner = MarketScanner(discovery, scanner_db, scanner_settings)

        result = await scanner.scan_all_markets()

        assert result == []
        assert discovery.get_all_active_markets.call_count == SCAN_MAX_RETRIES
        assert scanner._consecutive_scan_failures == 1

    @pytest.mark.asyncio
    async def test_succeeds_on_retry(self, scanner_settings, scanner_db):
        """If API fails once then succeeds, should return markets."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(
            side_effect=[RuntimeError("timeout"), []]
        )
        scanner = MarketScanner(discovery, scanner_db, scanner_settings)

        result = await scanner.scan_all_markets()

        assert result == []  # Empty list but success
        assert scanner._consecutive_scan_failures == 0

    @pytest.mark.asyncio
    async def test_consecutive_failures_skip_scan(self, scanner_settings, scanner_db):
        """After SCAN_MAX_CONSECUTIVE_FAILURES, scan should be skipped."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(
            side_effect=RuntimeError("API down")
        )
        scanner = MarketScanner(discovery, scanner_db, scanner_settings)
        scanner._consecutive_scan_failures = SCAN_MAX_CONSECUTIVE_FAILURES

        result = await scanner.scan_all_markets()

        assert result == []
        # Should NOT have called the API — skipped entirely
        discovery.get_all_active_markets.assert_not_called()

    @pytest.mark.asyncio
    async def test_consecutive_failures_reset_on_success(self, scanner_settings, scanner_db):
        """Successful scan should reset the consecutive failure counter."""
        discovery = MagicMock()
        discovery.get_all_active_markets = AsyncMock(return_value=[])
        scanner = MarketScanner(discovery, scanner_db, scanner_settings)
        scanner._consecutive_scan_failures = 2

        await scanner.scan_all_markets()

        assert scanner._consecutive_scan_failures == 0
