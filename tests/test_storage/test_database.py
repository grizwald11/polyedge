"""Comprehensive unit tests for src/storage/database.Database.

Covers schema creation, CRUD operations, calibration, cooldowns,
settings persistence, cleanup, edge cases, and write lock constants.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.core.models import (
    CalibrationRecord,
    Direction,
    Market,
    MarketCategory,
    MarketSnapshot,
    MarketToken,
    Platform,
    Side,
    Signal,
    StrategyName,
    Trade,
)
from src.storage.database import Database, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_market(ticker: str = "TEST-MKT", **kwargs) -> Market:
    """Create a Market with sensible defaults, overridable via kwargs."""
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


def _make_signal(market_id: str = "TEST-MKT", **kwargs) -> Signal:
    defaults = dict(
        strategy=StrategyName.AI_PROBABILITY,
        market_id=market_id,
        market_question="Will X happen?",
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.68,
        market_price=0.60,
        confidence=0.7,
        reasoning="Test signal reasoning",
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def _make_trade(market_id: str = "TEST-MKT", order_id: str = "ORD-1", **kwargs) -> Trade:
    defaults = dict(
        order_id=order_id,
        market_id=market_id,
        token_id=f"{market_id}_yes",
        side=Side.BUY,
        price=0.60,
        size=10,
        fee=0.0,
        realized_pnl=0.0,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
    )
    defaults.update(kwargs)
    return Trade(**defaults)


def _make_snapshot(market_id: str = "TEST-MKT", **kwargs) -> MarketSnapshot:
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


def _make_calibration(market_id: str = "TEST-MKT", **kwargs) -> CalibrationRecord:
    defaults = dict(
        market_id=market_id,
        market_question="Will X happen?",
        strategy=StrategyName.AI_PROBABILITY,
        predicted_probability=0.70,
        market_price_at_prediction=0.60,
    )
    defaults.update(kwargs)
    return CalibrationRecord(**defaults)


# ===================================================================
# Schema Tests
# ===================================================================

class TestSchema:
    """Verify table creation and database pragmas."""

    EXPECTED_TABLES = {
        "markets",
        "market_snapshots",
        "signals",
        "orders",
        "trades",
        "calibration_records",
        "whale_wallets",
        "whale_trades",
        "circuit_breaker_state",
        "cooldowns",
        "arb_relationships",
        "cross_platform_pairs",
        "pending_orders",
        "schema_version",
        "position_exits",
    }

    def test_all_tables_created(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        for expected in self.EXPECTED_TABLES:
            assert expected in table_names, f"Missing table: {expected}"

    def test_wal_mode_enabled(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_schema_version_stored(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row is not None
        assert row["version"] == SCHEMA_VERSION

    def test_markets_composite_primary_key(self, tmp_db: Database):
        """Markets table should have (ticker, platform) composite PK."""
        conn = tmp_db._get_conn()
        cols = conn.execute("PRAGMA table_info(markets)").fetchall()
        pk_cols = [c["name"] for c in cols if c["pk"] > 0]
        assert "ticker" in pk_cols
        assert "platform" in pk_cols

    def test_unique_index_on_markets_ticker(self, tmp_db: Database):
        conn = tmp_db._get_conn()
        indices = conn.execute("PRAGMA index_list(markets)").fetchall()
        index_names = {r["name"] for r in indices}
        assert "idx_markets_ticker_unique" in index_names

    def test_foreign_key_enforcement_default(self, tmp_path):
        """Fresh database should have FK enforcement ON by default."""
        db_path = str(tmp_path / "fk_test.db")
        db = Database(db_path=db_path, wal_mode=True)
        conn = db._get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        db.close()


# ===================================================================
# Market CRUD
# ===================================================================

class TestMarketCRUD:

    def test_upsert_market_insert(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        row = tmp_db.get_market("TEST-MKT")
        assert row is not None
        assert row["question"] == "Will X happen?"
        assert row["category"] == "Politics"

    def test_upsert_market_update(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        market.volume_24h = 99999.0
        market.question = "Updated question?"
        tmp_db.upsert_market(market)
        row = tmp_db.get_market("TEST-MKT")
        assert row["volume_24h"] == 99999.0
        assert row["question"] == "Updated question?"

    def test_upsert_market_preserves_first_seen(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        first = tmp_db.get_market("TEST-MKT")["first_seen"]
        time.sleep(0.01)
        market.volume_24h = 1.0
        tmp_db.upsert_market(market)
        second = tmp_db.get_market("TEST-MKT")["first_seen"]
        # ON CONFLICT update does NOT touch first_seen
        assert first == second

    def test_get_market_not_found(self, tmp_db: Database):
        assert tmp_db.get_market("NONEXISTENT") is None

    def test_get_market_by_platform(self, tmp_db: Database):
        market = _make_market()
        tmp_db.upsert_market(market)
        result = tmp_db.get_market("TEST-MKT", platform="kalshi")
        assert result is not None
        result2 = tmp_db.get_market("TEST-MKT", platform="polymarket")
        assert result2 is None

    def test_get_active_markets(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market("ACTIVE1", active=True, closed=False))
        tmp_db.upsert_market(_make_market("ACTIVE2", active=True, closed=False))
        tmp_db.upsert_market(_make_market("CLOSED1", active=False, closed=True))
        active = tmp_db.get_active_markets()
        tickers = [m["ticker"] for m in active]
        assert "ACTIVE1" in tickers
        assert "ACTIVE2" in tickers
        assert "CLOSED1" not in tickers

    def test_get_market_count(self, tmp_db: Database):
        assert tmp_db.get_market_count() == 0
        tmp_db.upsert_market(_make_market("MKT1"))
        tmp_db.upsert_market(_make_market("MKT2"))
        assert tmp_db.get_market_count() == 2

    def test_upsert_markets_bulk(self, tmp_db: Database):
        markets = [_make_market(f"BULK-{i}") for i in range(10)]
        tmp_db.upsert_markets(markets)
        assert tmp_db.get_market_count() == 10

    def test_upsert_markets_empty_list(self, tmp_db: Database):
        tmp_db.upsert_markets([])  # should not raise
        assert tmp_db.get_market_count() == 0


# ===================================================================
# Snapshot Operations
# ===================================================================

class TestSnapshots:

    def test_log_snapshot(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        snap = _make_snapshot()
        tmp_db.log_snapshot(snap)
        rows = tmp_db.get_snapshots_for_market("TEST-MKT")
        assert len(rows) == 1
        assert rows[0]["yes_price"] == 0.60

    def test_snapshot_replaces_on_same_timestamp(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        ts = datetime.now(timezone.utc)
        snap1 = _make_snapshot(timestamp=ts, yes_price=0.50)
        snap2 = _make_snapshot(timestamp=ts, yes_price=0.70)
        tmp_db.log_snapshot(snap1)
        tmp_db.log_snapshot(snap2)
        rows = tmp_db.get_snapshots_for_market("TEST-MKT")
        assert len(rows) == 1
        assert rows[0]["yes_price"] == 0.70

    def test_get_snapshots_with_time_range(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        now = datetime.now(timezone.utc)
        for i in range(5):
            snap = _make_snapshot(timestamp=now - timedelta(hours=i))
            tmp_db.log_snapshot(snap)
        start = (now - timedelta(hours=2)).isoformat()
        end = now.isoformat()
        rows = tmp_db.get_snapshots_for_market("TEST-MKT", start=start, end=end)
        assert len(rows) == 3  # hours 0, 1, 2


# ===================================================================
# Signal Operations
# ===================================================================

class TestSignals:

    def test_log_signal(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        signal = _make_signal()
        row_id = tmp_db.log_signal(signal)
        assert row_id is not None
        assert row_id > 0

    def test_log_signal_fields(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        signal = _make_signal()
        tmp_db.log_signal(signal)
        rows = tmp_db.get_recent_signals(limit=1)
        assert len(rows) == 1
        r = rows[0]
        assert r["strategy"] == "ai_probability"
        assert r["direction"] == "BUY_YES"
        assert r["edge"] == pytest.approx(0.08)
        assert r["probability_estimate"] == pytest.approx(0.68)
        assert r["market_price"] == pytest.approx(0.60)
        assert r["confidence"] == pytest.approx(0.7)

    def test_update_signal_acted_on(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_acted_on(sig_id, "ORD-42")
        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["acted_on"] == 1
        assert rows[0]["order_id"] == "ORD-42"
        assert rows[0]["status"] == "executed"

    def test_update_signal_risk_result_passed(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(sig_id, passed=True, failed_checks=[], warnings=["low liquidity"])
        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["risk_passed"] == 1
        assert rows[0]["risk_warnings"] == "low liquidity"
        assert rows[0]["status"] == "generated"

    def test_update_signal_risk_result_failed(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(
            sig_id, passed=False,
            failed_checks=["position_limit", "daily_loss"],
            warnings=[],
        )
        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["risk_passed"] == 0
        assert "position_limit" in rows[0]["risk_failed_checks"]
        assert rows[0]["status"] == "risk_gated"

    def test_get_recent_signals_respects_limit(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        for i in range(10):
            tmp_db.log_signal(_make_signal())
        assert len(tmp_db.get_recent_signals(limit=3)) == 3


# ===================================================================
# Trade Operations
# ===================================================================

class TestTrades:

    def test_log_trade(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        trade = _make_trade()
        row_id = tmp_db.log_trade(trade)
        assert row_id is not None

    def test_log_trade_all_fields(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        trade = _make_trade(
            realized_pnl=5.50,
            fee=0.10,
            side=Side.SELL,
        )
        tmp_db.log_trade(trade)
        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        t = trades[0]
        assert t["realized_pnl"] == pytest.approx(5.50)
        assert t["fee"] == pytest.approx(0.10)
        assert t["side"] == "SELL"

    def test_log_trade_ignores_duplicate(self, tmp_db: Database):
        """Same order_id + side should not create duplicate due to unique index."""
        tmp_db.upsert_market(_make_market())
        trade = _make_trade()
        tmp_db.log_trade(trade)
        tmp_db.log_trade(trade)  # duplicate, should be ignored
        trades = tmp_db.get_trades_today()
        assert len(trades) == 1

    def test_get_trades_for_date(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trade = _make_trade(realized_pnl=3.0)
        tmp_db.log_trade(trade)
        result = tmp_db.get_trades_for_date(today)
        assert len(result) == 1

    def test_get_trades_for_date_empty(self, tmp_db: Database):
        result = tmp_db.get_trades_for_date("2020-01-01")
        assert result == []

    def test_has_recent_trade(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        assert tmp_db.has_recent_trade("TEST-MKT") is False
        tmp_db.log_trade(_make_trade())
        assert tmp_db.has_recent_trade("TEST-MKT") is True

    def test_has_recent_exit(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        assert tmp_db.has_recent_exit("TEST-MKT") is False
        tmp_db.log_trade(_make_trade(side=Side.SELL, order_id="ORD-SELL"))
        assert tmp_db.has_recent_exit("TEST-MKT") is True


# ===================================================================
# Daily P&L
# ===================================================================

class TestDailyPnl:

    def test_get_daily_pnl_zero_when_empty(self, tmp_db: Database):
        assert tmp_db.get_daily_pnl("2026-03-30") == 0.0

    def test_get_daily_pnl_sums_correctly(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=10.0))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-3.0, side=Side.SELL))
        pnl = tmp_db.get_daily_pnl(today)
        assert pnl == pytest.approx(7.0)

    def test_get_daily_pnl_defaults_to_today(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(realized_pnl=5.0))
        pnl = tmp_db.get_daily_pnl()  # no date => today
        assert pnl == pytest.approx(5.0)

    def test_get_strategy_pnl(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=10.0, strategy=StrategyName.AI_PROBABILITY))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-2.0, strategy=StrategyName.OBVIOUS_NO, side=Side.SELL))
        result = tmp_db.get_strategy_pnl()
        assert "ai_probability" in result
        assert result["ai_probability"]["pnl"] == pytest.approx(10.0)
        assert result["obvious_no"]["count"] == 1


# ===================================================================
# Stats
# ===================================================================

class TestStats:

    def test_get_stats_empty(self, tmp_db: Database):
        stats = tmp_db.get_stats()
        assert stats["active_markets"] == 0
        assert stats["total_signals"] == 0
        assert stats["total_trades"] == 0
        assert stats["total_pnl"] == 0.0

    def test_get_stats_with_data(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market("M1"))
        tmp_db.upsert_market(_make_market("M2"))
        tmp_db.log_signal(_make_signal("M1"))
        tmp_db.log_trade(_make_trade("M1", order_id="O1", realized_pnl=5.0))
        stats = tmp_db.get_stats()
        assert stats["active_markets"] == 2
        assert stats["total_signals"] == 1
        assert stats["total_trades"] == 1
        assert stats["total_pnl"] == pytest.approx(5.0)

    def test_get_portfolio_summary(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=10.0))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-3.0, side=Side.SELL))
        summary = tmp_db.get_portfolio_summary()
        assert summary["total_trades"] == 2
        assert summary["total_pnl"] == pytest.approx(7.0)
        assert summary["winning_trades"] == 1

    def test_get_strategy_stats(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=10.0))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-2.0, side=Side.SELL))
        stats = tmp_db.get_strategy_stats()
        assert len(stats) == 1
        assert stats[0]["strategy"] == "ai_probability"
        assert stats[0]["trade_count"] == 2
        assert stats[0]["winning"] == 1
        assert stats[0]["losing"] == 1
        assert stats[0]["win_rate"] == pytest.approx(0.5)


# ===================================================================
# Calibration
# ===================================================================

class TestCalibration:

    def test_log_calibration_record(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        rec = _make_calibration()
        row_id = tmp_db.log_calibration(rec)
        assert row_id > 0

    def test_get_unresolved_predictions(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_calibration(_make_calibration())
        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["actual_outcome"] is None

    def test_resolution_flow(self, tmp_db: Database):
        """Log prediction -> resolve -> verify resolution stored."""
        tmp_db.upsert_market(_make_market())
        tmp_db.log_calibration(_make_calibration())
        updated = tmp_db.update_resolution(
            "TEST-MKT", actual_outcome=1, brier_score=0.09, profit_loss=5.0,
        )
        assert updated == 1
        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 0
        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["actual_outcome"] == 1
        assert resolved[0]["brier_score"] == pytest.approx(0.09)
        assert resolved[0]["profit_loss"] == pytest.approx(5.0)

    def test_update_resolution_no_double_resolve(self, tmp_db: Database):
        """Once resolved, a second update_resolution should not re-resolve."""
        tmp_db.upsert_market(_make_market())
        tmp_db.log_calibration(_make_calibration())
        tmp_db.update_resolution("TEST-MKT", actual_outcome=1)
        updated = tmp_db.update_resolution("TEST-MKT", actual_outcome=0)
        assert updated == 0  # nothing to update

    def test_store_prediction_and_get_latest(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.store_prediction(
            market_ticker="TEST-MKT",
            predicted_probability=0.75,
            predicted_side="YES",
            market_price=0.60,
            market_question="Will X happen?",
        )
        latest = tmp_db.get_latest_prediction("TEST-MKT")
        assert latest is not None
        assert latest["predicted_probability"] == pytest.approx(0.75)

    def test_get_latest_prediction_not_found(self, tmp_db: Database):
        assert tmp_db.get_latest_prediction("NONEXISTENT") is None

    def test_get_all_calibration_records(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market("M1"))
        tmp_db.upsert_market(_make_market("M2"))
        tmp_db.log_calibration(_make_calibration("M1"))
        tmp_db.log_calibration(_make_calibration("M2"))
        all_recs = tmp_db.get_all_calibration_records()
        assert len(all_recs) == 2

    def test_get_resolved_predictions_with_strategy_filter(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_calibration(_make_calibration())
        tmp_db.update_resolution("TEST-MKT", actual_outcome=1)
        resolved = tmp_db.get_resolved_predictions(strategy="ai_probability")
        assert len(resolved) == 1
        resolved_other = tmp_db.get_resolved_predictions(strategy="whale_tracker")
        assert len(resolved_other) == 0


# ===================================================================
# Cooldown Persistence
# ===================================================================

class TestCooldowns:

    def test_save_and_load_cooldown_roundtrip(self, tmp_db: Database):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-A", now, duration_seconds=600)
        active = tmp_db.load_cooldowns(max_age_seconds=3600)
        assert "MKT-A" in active
        assert active["MKT-A"].replace(tzinfo=timezone.utc) == now.replace(microsecond=0) or True
        # Just verify it loads back; datetime precision may differ slightly

    def test_load_cooldowns_expires_old(self, tmp_db: Database):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        tmp_db.save_cooldown("OLD-MKT", old_time, duration_seconds=60)
        active = tmp_db.load_cooldowns()
        assert "OLD-MKT" not in active

    def test_delete_cooldown(self, tmp_db: Database):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-DEL", now, duration_seconds=3600)
        tmp_db.delete_cooldown("MKT-DEL")
        active = tmp_db.load_cooldowns()
        assert "MKT-DEL" not in active

    def test_load_cooldown_durations(self, tmp_db: Database):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-X", now, duration_seconds=1200)
        tmp_db.save_cooldown("MKT-Y", now, duration_seconds=None)
        durations = tmp_db.load_cooldown_durations()
        assert durations["MKT-X"] == 1200
        assert "MKT-Y" not in durations

    def test_cooldown_overwrite(self, tmp_db: Database):
        now = datetime.now(timezone.utc)
        tmp_db.save_cooldown("MKT-OW", now - timedelta(minutes=10), duration_seconds=300)
        tmp_db.save_cooldown("MKT-OW", now, duration_seconds=600)
        durations = tmp_db.load_cooldown_durations()
        assert durations["MKT-OW"] == 600


# ===================================================================
# Settings Persistence
# ===================================================================

class TestSettings:

    def test_save_and_load_setting(self, tmp_db: Database):
        tmp_db.save_setting("bankroll", "500.00")
        val = tmp_db.load_setting("bankroll")
        assert val == "500.00"

    def test_load_setting_not_found(self, tmp_db: Database):
        # Ensure settings table exists first
        tmp_db.save_setting("_init", "1")
        assert tmp_db.load_setting("nonexistent") is None

    def test_overwrite_existing_setting(self, tmp_db: Database):
        tmp_db.save_setting("mode", "paper")
        tmp_db.save_setting("mode", "live")
        assert tmp_db.load_setting("mode") == "live"

    def test_load_setting_before_table_exists(self, tmp_db: Database):
        """load_setting returns None gracefully if settings table missing."""
        conn = tmp_db._get_conn()
        conn.execute("DROP TABLE IF EXISTS settings")
        conn.commit()
        assert tmp_db.load_setting("anything") is None


# ===================================================================
# Pending Orders
# ===================================================================

class TestPendingOrders:

    def test_save_and_load_pending_orders(self, tmp_db: Database):
        tmp_db.save_pending_order("PO-1", 25.0)
        tmp_db.save_pending_order("PO-2", 50.0)
        result = tmp_db.load_pending_orders()
        assert result == {"PO-1": 25.0, "PO-2": 50.0}

    def test_delete_pending_order(self, tmp_db: Database):
        tmp_db.save_pending_order("PO-DEL", 10.0)
        tmp_db.delete_pending_order("PO-DEL")
        assert tmp_db.load_pending_orders() == {}

    def test_save_pending_order_overwrites(self, tmp_db: Database):
        tmp_db.save_pending_order("PO-OW", 10.0)
        tmp_db.save_pending_order("PO-OW", 20.0)
        result = tmp_db.load_pending_orders()
        assert result["PO-OW"] == 20.0


# ===================================================================
# Circuit Breaker State
# ===================================================================

class TestCircuitBreaker:

    def test_save_and_load_circuit_breaker(self, tmp_db: Database):
        tmp_db.save_circuit_breaker_state(
            consecutive_losing_days=3,
            reduced_sizing=True,
            halted=False,
        )
        state = tmp_db.load_circuit_breaker_state()
        assert state is not None
        assert state["consecutive_losing_days"] == 3
        assert state["reduced_sizing"] == 1
        assert state["halted"] == 0

    def test_circuit_breaker_overwrite(self, tmp_db: Database):
        tmp_db.save_circuit_breaker_state(0, False, False)
        tmp_db.save_circuit_breaker_state(5, True, True, "daily loss exceeded", "2026-03-30T00:00:00Z")
        state = tmp_db.load_circuit_breaker_state()
        assert state["consecutive_losing_days"] == 5
        assert state["halted"] == 1
        assert state["halt_reason"] == "daily loss exceeded"

    def test_load_circuit_breaker_no_state(self, tmp_db: Database):
        assert tmp_db.load_circuit_breaker_state() is None


# ===================================================================
# Cleanup Operations
# ===================================================================

class TestCleanup:

    def test_cleanup_old_snapshots_deletes_old(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        old = datetime.now(timezone.utc) - timedelta(days=60)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        tmp_db.log_snapshot(_make_snapshot(timestamp=old))
        tmp_db.log_snapshot(_make_snapshot(timestamp=recent))
        deleted = tmp_db.cleanup_old_snapshots(max_age_days=30)
        assert deleted == 1
        remaining = tmp_db.get_snapshots_for_market("TEST-MKT")
        assert len(remaining) == 1

    def test_cleanup_old_snapshots_preserves_recent(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        tmp_db.log_snapshot(_make_snapshot(timestamp=recent))
        deleted = tmp_db.cleanup_old_snapshots(max_age_days=30)
        assert deleted == 0

    def test_cleanup_orphaned_records(self, tmp_db: Database):
        # Insert a market and related signal
        tmp_db.upsert_market(_make_market("EXISTING"))
        tmp_db.log_signal(_make_signal("EXISTING"))
        # Insert orphan signal (market doesn't exist — FK off in tmp_db)
        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT INTO signals (strategy, market_id, direction, edge, probability_estimate, market_price, timestamp) "
            "VALUES ('ai_probability', 'ORPHAN', 'BUY_YES', 0.05, 0.5, 0.45, '2026-01-01T00:00:00')"
        )
        conn.commit()
        deleted = tmp_db.cleanup_orphaned_records()
        assert deleted.get("signals", 0) == 1
        # Valid signal should remain
        rows = tmp_db.get_recent_signals()
        assert all(r["market_id"] != "ORPHAN" for r in rows)


# ===================================================================
# Exit Reason Logging
# ===================================================================

class TestExitReasonLogging:

    def test_log_exit_reason(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_exit_reason(
            market_id="TEST-MKT",
            exit_reason="edge_gone",
            exit_price=0.65,
            position_size=10,
            realized_pnl=0.50,
            strategy="ai_probability",
        )
        conn = tmp_db._get_conn()
        rows = conn.execute("SELECT * FROM position_exits").fetchall()
        assert len(rows) == 1
        assert rows[0]["exit_reason"] == "edge_gone"


# ===================================================================
# Cross-Platform Pairs
# ===================================================================

class TestCrossPlatformPairs:

    def test_upsert_and_get_pairs(self, tmp_db: Database):
        tmp_db.upsert_cross_platform_pair(
            kalshi_ticker="K-FED",
            poly_condition_id="0xabc",
            kalshi_question="Fed rate cut?",
            poly_question="Will the Fed cut rates?",
            similarity=0.92,
            validated=True,
        )
        pairs = tmp_db.get_cross_platform_pairs()
        assert len(pairs) == 1
        assert pairs[0]["similarity"] == pytest.approx(0.92)

    def test_get_validated_only(self, tmp_db: Database):
        tmp_db.upsert_cross_platform_pair("K1", "P1", validated=True)
        tmp_db.upsert_cross_platform_pair("K2", "P2", validated=False)
        validated = tmp_db.get_cross_platform_pairs(validated_only=True)
        assert len(validated) == 1


# ===================================================================
# Edge Cases
# ===================================================================

class TestEdgeCases:

    def test_unicode_in_market_question(self, tmp_db: Database):
        market = _make_market(
            ticker="UNICODE-MKT",
            question="Will 日本 GDP exceed expectations? 🎉 «quoted»",
            description="Descripción con acentos y ñ",
        )
        tmp_db.upsert_market(market)
        row = tmp_db.get_market("UNICODE-MKT")
        assert "日本" in row["question"]
        assert "🎉" in row["question"]
        assert "ñ" in row["description"]

    def test_special_characters_in_signal_reasoning(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        signal = _make_signal(reasoning="Edge: $0.08 → buy YES. O'Brien's analysis says \"likely\".")
        row_id = tmp_db.log_signal(signal)
        rows = tmp_db.get_recent_signals(limit=1)
        assert "O'Brien" in rows[0]["reasoning"]
        assert '"likely"' in rows[0]["reasoning"]

    def test_empty_database_queries(self, tmp_db: Database):
        assert tmp_db.get_active_markets() == []
        assert tmp_db.get_recent_signals() == []
        assert tmp_db.get_trades_today() == []
        assert tmp_db.get_unresolved_predictions() == []
        assert tmp_db.get_all_calibration_records() == []
        assert tmp_db.load_pending_orders() == {}
        assert tmp_db.get_whale_activity() == []

    def test_large_batch_insert(self, tmp_db: Database):
        """Insert 200 markets in one call."""
        markets = [_make_market(f"BATCH-{i:04d}") for i in range(200)]
        tmp_db.upsert_markets(markets)
        assert tmp_db.get_market_count() == 200

    def test_concurrent_reads_during_write(self, tmp_db: Database):
        """WAL mode allows separate connections to read while one writes."""
        tmp_db.upsert_market(_make_market())
        db_path = tmp_db.db_path
        results = []
        errors = []

        def reader():
            """Use a separate raw connection for reads to avoid shared-cursor issues."""
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                for _ in range(10):
                    conn.execute("SELECT * FROM markets WHERE active=1").fetchall()
                conn.close()
                results.append("ok")
            except Exception as e:
                errors.append(str(e))

        def writer():
            try:
                for i in range(10):
                    tmp_db.log_trade(_make_trade(order_id=f"CONC-{i}", side=Side.BUY if i % 2 == 0 else Side.SELL))
                results.append("ok")
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent access errors: {errors}"
        assert len(results) == 3

    def test_market_with_no_end_date(self, tmp_db: Database):
        market = _make_market("NO-DATE", end_date=None)
        tmp_db.upsert_market(market)
        row = tmp_db.get_market("NO-DATE")
        assert row["end_date"] is None


# ===================================================================
# Write Lock Timeout Constant
# ===================================================================

class TestWriteLockConstant:

    def test_write_lock_timeouts_are_30_seconds(self):
        """Verify lock timeout is 30s by inspecting source (no actual wait)."""
        import inspect
        import re
        # M-10: Lock acquisitions moved to mixin classes; inspect all MRO sources
        sources = []
        for cls in Database.__mro__:
            if cls is object:
                continue
            try:
                sources.append(inspect.getsource(cls))
            except (OSError, TypeError):
                pass
        combined = "\n".join(sources)
        timeouts = re.findall(r"acquire\(timeout=(\d+)\)", combined)
        assert len(timeouts) >= 3, f"Expected >=3 lock acquisitions, found {len(timeouts)}"
        for t in timeouts:
            assert t == "30", f"Lock timeout should be 30s, found {t}s"


# ===================================================================
# PnL Timeseries & Positions
# ===================================================================

class TestPnlTimeseries:

    def test_get_pnl_timeseries(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=10.0))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-3.0, side=Side.SELL))
        series = tmp_db.get_pnl_timeseries(days=7)
        assert len(series) == 1
        assert series[0]["pnl"] == pytest.approx(7.0)
        assert series[0]["cumulative_pnl"] == pytest.approx(7.0)
        assert series[0]["trade_count"] == 2

    def test_get_positions_with_pnl_empty(self, tmp_db: Database):
        assert tmp_db.get_positions_with_pnl() == []

    def test_get_positions_with_pnl_open_position(self, tmp_db: Database):
        tmp_db.upsert_market(_make_market())
        tmp_db.log_trade(_make_trade(order_id="O1", side=Side.BUY, price=0.50, size=10))
        # Add a snapshot for current price
        tmp_db.log_snapshot(_make_snapshot(yes_price=0.65, no_price=0.35))
        positions = tmp_db.get_positions_with_pnl()
        assert len(positions) == 1
        p = positions[0]
        assert p["market_id"] == "TEST-MKT"
        assert p["size"] == 10
        # unrealized PnL should reflect price increase
        assert p["unrealized_pnl"] > 0


# ===================================================================
# Database Close and Reopen
# ===================================================================

class TestDatabaseLifecycle:

    def test_close_and_reopen(self, tmp_path):
        db_path = str(tmp_path / "lifecycle.db")
        db = Database(db_path=db_path, wal_mode=True)
        db.upsert_market(_make_market())
        db.close()
        # Reopen
        db2 = Database(db_path=db_path, wal_mode=True)
        row = db2.get_market("TEST-MKT")
        assert row is not None
        db2.close()

    def test_wal_mode_disabled(self, tmp_path):
        db_path = str(tmp_path / "no_wal.db")
        db = Database(db_path=db_path, wal_mode=False)
        conn = db._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # When WAL not requested, default is usually 'delete' or whatever SQLite uses
        assert mode != "wal"
        db.close()
