"""Tests for MarketsMixin methods in src/storage/db_markets.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Market, MarketCategory, MarketSnapshot, MarketToken, Platform
from src.storage.database import Database


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────


def _make_market(ticker="TEST-MKT", **kwargs) -> Market:
    defaults = dict(
        ticker=ticker,
        question="Will X happen?",
        description="Test market",
        category=MarketCategory.POLITICS,
        tags=["test"],
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=0.60),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=0.40),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        volume_total=1_000_000.0,
        liquidity=20000.0,
        spread=0.02,
        active=True,
        closed=False,
        resolution_source="https://example.com",
        slug=ticker.lower(),
    )
    defaults.update(kwargs)
    return Market(**defaults)


def _make_snapshot(market_id="TEST-MKT", **kwargs) -> MarketSnapshot:
    defaults = dict(
        market_id=market_id,
        yes_price=0.60,
        no_price=0.40,
        spread=0.02,
        volume_1h=5000.0,
        liquidity=20000.0,
    )
    defaults.update(kwargs)
    return MarketSnapshot(**defaults)


# ──────────────────────────────────────
# upsert_market
# ──────────────────────────────────────


class TestUpsertMarket:
    """Tests for upsert_market — insert and update."""

    def test_insert_new_market(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)

        row = tmp_db.get_market("TEST-MKT")
        assert row is not None
        assert row["ticker"] == "TEST-MKT"
        assert row["question"] == "Will X happen?"
        assert row["category"] == "Politics"
        assert row["active"] == 1
        assert row["closed"] == 0
        assert row["volume_24h"] == 50000.0

    def test_update_existing_market(self, tmp_db: Database):
        market = _make_market(volume_24h=50000.0)
        tmp_db.upsert_market(market)

        updated = _make_market(volume_24h=75000.0, question="Will X really happen?")
        tmp_db.upsert_market(updated)

        row = tmp_db.get_market("TEST-MKT")
        assert row["volume_24h"] == 75000.0
        assert row["question"] == "Will X really happen?"

    def test_upsert_preserves_first_seen(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        row1 = tmp_db.get_market("TEST-MKT")
        first_seen = row1["first_seen"]

        updated = _make_market(volume_24h=99999.0)
        tmp_db.upsert_market(updated)
        row2 = tmp_db.get_market("TEST-MKT")

        # first_seen should NOT change on update (not in the UPDATE SET clause)
        assert row2["first_seen"] == first_seen

    def test_upsert_updates_last_updated(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        row1 = tmp_db.get_market("TEST-MKT")

        updated = _make_market(volume_24h=99999.0)
        tmp_db.upsert_market(updated)
        row2 = tmp_db.get_market("TEST-MKT")

        assert row2["last_updated"] >= row1["last_updated"]

    def test_upsert_stores_tokens_as_json(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)

        row = tmp_db.get_market("TEST-MKT")
        import json
        tokens = json.loads(row["tokens"])
        assert len(tokens) == 2
        assert tokens[0]["token_id"] == "TEST-MKT_yes"
        assert tokens[1]["outcome"] == "No"

    def test_upsert_stores_tags_as_json(self, tmp_db: Database):
        market = _make_market(tags=["politics", "elections"])
        tmp_db.upsert_market(market)

        row = tmp_db.get_market("TEST-MKT")
        import json
        tags = json.loads(row["tags"])
        assert tags == ["politics", "elections"]

    def test_upsert_closed_market(self, tmp_db: Database):
        market = _make_market(active=False, closed=True)
        tmp_db.upsert_market(market)

        row = tmp_db.get_market("TEST-MKT")
        assert row["active"] == 0
        assert row["closed"] == 1

    def test_upsert_with_platform(self, tmp_db: Database):
        market = _make_market(platform=Platform.KALSHI)
        tmp_db.upsert_market(market)

        row = tmp_db.get_market("TEST-MKT")
        assert row["platform"] == "kalshi"


# ──────────────────────────────────────
# upsert_markets (bulk)
# ──────────────────────────────────────


class TestUpsertMarkets:
    """Tests for upsert_markets — bulk insert."""

    def test_bulk_insert_multiple(self, tmp_db: Database):
        markets = [
            _make_market(ticker="MKT-A", volume_24h=10000.0),
            _make_market(ticker="MKT-B", volume_24h=20000.0),
            _make_market(ticker="MKT-C", volume_24h=30000.0),
        ]
        tmp_db.upsert_markets(markets)

        assert tmp_db.get_market("MKT-A") is not None
        assert tmp_db.get_market("MKT-B") is not None
        assert tmp_db.get_market("MKT-C") is not None

    def test_bulk_insert_empty_list(self, tmp_db: Database):
        # Should not raise
        tmp_db.upsert_markets([])
        assert tmp_db.get_market_count() == 0

    def test_bulk_upsert_updates_existing(self, tmp_db: Database):
        markets = [_make_market(ticker="MKT-A", volume_24h=10000.0)]
        tmp_db.upsert_markets(markets)

        updated = [_make_market(ticker="MKT-A", volume_24h=99999.0)]
        tmp_db.upsert_markets(updated)

        row = tmp_db.get_market("MKT-A")
        assert row["volume_24h"] == 99999.0

    def test_bulk_insert_is_atomic(self, tmp_db: Database):
        """All markets in a single bulk call share one commit."""
        markets = [
            _make_market(ticker=f"BULK-{i}", volume_24h=float(i * 1000))
            for i in range(10)
        ]
        tmp_db.upsert_markets(markets)
        assert tmp_db.get_market_count() == 10


# ──────────────────────────────────────
# get_active_markets
# ──────────────────────────────────────


class TestGetActiveMarkets:
    """Tests for get_active_markets — active=1, closed=0, ordered by volume DESC."""

    def test_returns_only_active_open_markets(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="ACTIVE", active=True, closed=False))
        tmp_db.upsert_market(_make_market(ticker="INACTIVE", active=False, closed=False))
        tmp_db.upsert_market(_make_market(ticker="CLOSED", active=True, closed=True))
        tmp_db.upsert_market(_make_market(ticker="BOTH", active=False, closed=True))

        results = tmp_db.get_active_markets()
        tickers = [r["ticker"] for r in results]
        assert tickers == ["ACTIVE"]

    def test_ordered_by_volume_desc(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="LOW", volume_24h=1000.0))
        tmp_db.upsert_market(_make_market(ticker="HIGH", volume_24h=90000.0))
        tmp_db.upsert_market(_make_market(ticker="MID", volume_24h=50000.0))

        results = tmp_db.get_active_markets()
        tickers = [r["ticker"] for r in results]
        assert tickers == ["HIGH", "MID", "LOW"]

    def test_returns_empty_when_none_active(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="CLOSED", active=False, closed=True))
        assert tmp_db.get_active_markets() == []

    def test_returns_dicts(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        results = tmp_db.get_active_markets()
        assert isinstance(results[0], dict)
        assert "ticker" in results[0]


# ──────────────────────────────────────
# get_market
# ──────────────────────────────────────


class TestGetMarket:
    """Tests for get_market — single lookup with optional platform filter."""

    def test_get_existing_market(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="FIND-ME"))
        row = tmp_db.get_market("FIND-ME")
        assert row is not None
        assert row["ticker"] == "FIND-ME"

    def test_get_nonexistent_market(self, tmp_db: Database):
        result = tmp_db.get_market("DOES-NOT-EXIST")
        assert result is None

    def test_get_market_with_platform_filter(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="PLAT-TEST", platform=Platform.KALSHI))

        found = tmp_db.get_market("PLAT-TEST", platform="kalshi")
        assert found is not None

        not_found = tmp_db.get_market("PLAT-TEST", platform="polymarket")
        assert not_found is None

    def test_get_market_without_platform_returns_first(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="ANY-PLAT"))
        row = tmp_db.get_market("ANY-PLAT")
        assert row is not None


# ──────────────────────────────────────
# get_market_count
# ──────────────────────────────────────


class TestGetMarketCount:
    """Tests for get_market_count — count of active markets."""

    def test_count_active_only(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="A1", active=True))
        tmp_db.upsert_market(_make_market(ticker="A2", active=True))
        tmp_db.upsert_market(_make_market(ticker="I1", active=False))

        assert tmp_db.get_market_count() == 2

    def test_count_zero_when_empty(self, tmp_db: Database):
        assert tmp_db.get_market_count() == 0

    def test_count_includes_closed_if_still_active(self, tmp_db: Database):
        """get_market_count checks active=1 only (not closed)."""
        tmp_db.upsert_market(_make_market(ticker="AC", active=True, closed=True))
        # active=1 but closed=1 still counts as active in this method
        assert tmp_db.get_market_count() == 1


# ──────────────────────────────────────
# log_snapshot
# ──────────────────────────────────────


class TestLogSnapshot:
    """Tests for log_snapshot — stores snapshot, replaces on duplicate."""

    def test_insert_snapshot(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="SNAP-MKT"))
        snap = _make_snapshot(market_id="SNAP-MKT")
        tmp_db.log_snapshot(snap)

        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        assert len(rows) == 1
        assert rows[0]["yes_price"] == 0.60
        assert rows[0]["no_price"] == 0.40
        assert rows[0]["volume_1h"] == 5000.0

    def test_multiple_snapshots_different_times(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="SNAP-MKT"))

        now = datetime.now(timezone.utc)
        snap1 = _make_snapshot(
            market_id="SNAP-MKT",
            timestamp=now - timedelta(hours=2),
            yes_price=0.55,
        )
        snap2 = _make_snapshot(
            market_id="SNAP-MKT",
            timestamp=now - timedelta(hours=1),
            yes_price=0.60,
        )
        snap3 = _make_snapshot(
            market_id="SNAP-MKT",
            timestamp=now,
            yes_price=0.65,
        )
        tmp_db.log_snapshot(snap1)
        tmp_db.log_snapshot(snap2)
        tmp_db.log_snapshot(snap3)

        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        assert len(rows) == 3
        # Ordered by timestamp ASC
        assert rows[0]["yes_price"] == 0.55
        assert rows[2]["yes_price"] == 0.65

    def test_snapshot_stores_all_fields(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="SNAP-MKT"))
        snap = _make_snapshot(
            market_id="SNAP-MKT",
            yes_price=0.72,
            no_price=0.28,
            spread=0.04,
            volume_1h=12345.0,
            liquidity=88888.0,
        )
        tmp_db.log_snapshot(snap)

        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        assert len(rows) == 1
        r = rows[0]
        assert r["yes_price"] == 0.72
        assert r["no_price"] == 0.28
        assert r["spread"] == 0.04
        assert r["volume_1h"] == 12345.0
        assert r["liquidity"] == 88888.0


# ──────────────────────────────────────
# get_snapshots_for_market
# ──────────────────────────────────────


class TestGetSnapshotsForMarket:
    """Tests for get_snapshots_for_market — with start/end filters."""

    def _seed_snapshots(self, tmp_db: Database, market_id: str = "SNAP-MKT"):
        """Insert a market and 5 snapshots spanning 5 hours."""
        tmp_db.upsert_market(_make_market(ticker=market_id))
        base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            snap = _make_snapshot(
                market_id=market_id,
                timestamp=base + timedelta(hours=i),
                yes_price=0.50 + i * 0.05,
            )
            tmp_db.log_snapshot(snap)

    def test_no_filters_returns_all(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        assert len(rows) == 5

    def test_start_filter(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        start = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc).isoformat()
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT", start=start)
        # Hours 14, 15, 16 => 3 snapshots
        assert len(rows) == 3

    def test_end_filter(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        end = datetime(2026, 4, 1, 13, 0, 0, tzinfo=timezone.utc).isoformat()
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT", end=end)
        # Hours 12, 13 => 2 snapshots
        assert len(rows) == 2

    def test_start_and_end_filter(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        start = datetime(2026, 4, 1, 13, 0, 0, tzinfo=timezone.utc).isoformat()
        end = datetime(2026, 4, 1, 15, 0, 0, tzinfo=timezone.utc).isoformat()
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT", start=start, end=end)
        # Hours 13, 14, 15 => 3 snapshots
        assert len(rows) == 3

    def test_ordered_by_timestamp_asc(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)

    def test_nonexistent_market_returns_empty(self, tmp_db: Database):
        rows = tmp_db.get_snapshots_for_market("NOPE")
        assert rows == []

    def test_returns_dicts(self, tmp_db: Database):
        self._seed_snapshots(tmp_db)
        rows = tmp_db.get_snapshots_for_market("SNAP-MKT")
        assert isinstance(rows[0], dict)


# ──────────────────────────────────────
# cleanup_old_snapshots
# ──────────────────────────────────────


class TestCleanupOldSnapshots:
    """Tests for cleanup_old_snapshots — deletes old snapshots, returns count."""

    def test_deletes_old_snapshots(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="CLEANUP"))

        old_time = datetime.now(timezone.utc) - timedelta(days=45)
        recent_time = datetime.now(timezone.utc) - timedelta(hours=1)

        tmp_db.log_snapshot(_make_snapshot(market_id="CLEANUP", timestamp=old_time))
        tmp_db.log_snapshot(_make_snapshot(market_id="CLEANUP", timestamp=recent_time))

        deleted = tmp_db.cleanup_old_snapshots(max_age_days=30)
        assert deleted == 1

        remaining = tmp_db.get_snapshots_for_market("CLEANUP")
        assert len(remaining) == 1

    def test_returns_zero_when_nothing_to_delete(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="FRESH"))
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        tmp_db.log_snapshot(_make_snapshot(market_id="FRESH", timestamp=recent))

        deleted = tmp_db.cleanup_old_snapshots(max_age_days=30)
        assert deleted == 0

    def test_custom_max_age(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="AGE"))

        eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

        tmp_db.log_snapshot(_make_snapshot(market_id="AGE", timestamp=eight_days_ago))
        tmp_db.log_snapshot(_make_snapshot(market_id="AGE", timestamp=three_days_ago))

        deleted = tmp_db.cleanup_old_snapshots(max_age_days=7)
        assert deleted == 1

        remaining = tmp_db.get_snapshots_for_market("AGE")
        assert len(remaining) == 1

    def test_deletes_all_old_snapshots(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="OLD"))
        old_base = datetime.now(timezone.utc) - timedelta(days=60)

        for i in range(5):
            tmp_db.log_snapshot(
                _make_snapshot(
                    market_id="OLD",
                    timestamp=old_base + timedelta(hours=i),
                )
            )

        deleted = tmp_db.cleanup_old_snapshots(max_age_days=30)
        assert deleted == 5
        assert tmp_db.get_snapshots_for_market("OLD") == []


# ──────────────────────────────────────
# cleanup_orphaned_records
# ──────────────────────────────────────


class TestCleanupOrphanedRecords:
    """Tests for cleanup_orphaned_records — cleans orphaned child rows."""

    def test_removes_orphaned_snapshots(self, tmp_db: Database):
        # Insert snapshot with no parent market
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO market_snapshots (market_id, timestamp, yes_price, no_price) "
            "VALUES (?, ?, ?, ?)",
            ("ORPHAN-MKT", now, 0.5, 0.5),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert "market_snapshots" in result
        assert result["market_snapshots"] >= 1

    def test_removes_orphaned_signals(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (strategy, market_id, direction, edge, "
            "probability_estimate, market_price, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ai_probability", "ORPHAN-SIG", "BUY_YES", 0.05, 0.6, 0.55, now),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert "signals" in result
        assert result["signals"] >= 1

    def test_removes_orphaned_orders(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO orders (id, market_id, token_id, side, price, size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("orphan-order-1", "ORPHAN-ORD", "tok1", "BUY", 0.5, 10.0, now),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert "orders" in result

    def test_removes_orphaned_trades(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO trades (order_id, market_id, token_id, side, price, size, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ord-1", "ORPHAN-TRD", "tok1", "BUY", 0.5, 10.0, now),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert "trades" in result

    def test_removes_orphaned_calibration_records(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO calibration_records (market_id, predicted_probability, "
            "market_price_at_prediction, predicted_at) "
            "VALUES (?, ?, ?, ?)",
            ("ORPHAN-CAL", 0.7, 0.6, now),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert "calibration_records" in result

    def test_does_not_remove_non_orphaned_records(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="VALID"))
        snap = _make_snapshot(market_id="VALID")
        tmp_db.log_snapshot(snap)

        result = tmp_db.cleanup_orphaned_records()
        # No orphans to clean
        assert "market_snapshots" not in result

        remaining = tmp_db.get_snapshots_for_market("VALID")
        assert len(remaining) == 1

    def test_returns_empty_dict_when_no_orphans(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market(ticker="GOOD"))
        result = tmp_db.cleanup_orphaned_records()
        assert result == {}

    def test_mixed_orphaned_and_valid(self, tmp_db: Database):
        # Insert a valid market with a snapshot
        tmp_db.upsert_market(_make_market(ticker="VALID"))
        tmp_db.log_snapshot(_make_snapshot(market_id="VALID"))

        # Insert orphaned snapshot
        conn = tmp_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO market_snapshots (market_id, timestamp, yes_price, no_price) "
            "VALUES (?, ?, ?, ?)",
            ("GONE-MKT", now, 0.3, 0.7),
        )
        conn.commit()

        result = tmp_db.cleanup_orphaned_records()
        assert result.get("market_snapshots", 0) >= 1

        # Valid snapshot should still exist
        remaining = tmp_db.get_snapshots_for_market("VALID")
        assert len(remaining) == 1
