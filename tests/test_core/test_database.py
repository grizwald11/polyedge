"""Tests for the SQLite database module."""

from datetime import datetime, timezone

import pytest

from src.core.models import (
    Signal, StrategyName, Direction, Trade, Side, CalibrationRecord, MarketSnapshot,
)
from src.storage.database import Database


class TestDatabaseInit:
    def test_creates_tables(self, tmp_db):
        stats = tmp_db.get_stats()
        assert stats["active_markets"] == 0
        assert stats["total_signals"] == 0
        assert stats["total_trades"] == 0

    def test_wal_mode(self, tmp_path):
        db = Database(str(tmp_path / "wal_test.db"), wal_mode=True)
        assert db.wal_mode is True


class TestMarketOperations:
    def test_upsert_and_get(self, tmp_db, sample_market):
        tmp_db.upsert_market(sample_market)
        stored = tmp_db.get_market(sample_market.ticker)
        assert stored is not None
        assert stored["question"] == sample_market.question
        assert stored["category"] == "Fed/Macro"

    def test_upsert_updates_existing(self, tmp_db, sample_market):
        tmp_db.upsert_market(sample_market)
        updated = sample_market.model_copy()
        updated.volume_24h = 250000.0
        tmp_db.upsert_market(updated)
        stored = tmp_db.get_market(sample_market.ticker)
        assert stored["volume_24h"] == 250000.0

    def test_get_active_markets(self, tmp_db, sample_market, sample_market_politics):
        tmp_db.upsert_market(sample_market)
        tmp_db.upsert_market(sample_market_politics)
        markets = tmp_db.get_active_markets()
        assert len(markets) == 2

    def test_get_nonexistent(self, tmp_db):
        result = tmp_db.get_market("nonexistent_id")
        assert result is None

    def test_market_count(self, tmp_db, sample_market):
        assert tmp_db.get_market_count() == 0
        tmp_db.upsert_market(sample_market)
        assert tmp_db.get_market_count() == 1


class TestSnapshotOperations:
    def test_log_snapshot(self, tmp_db, sample_market):
        tmp_db.upsert_market(sample_market)
        snapshot = MarketSnapshot(
            market_id=sample_market.ticker,
            yes_price=0.34,
            no_price=0.66,
            spread=0.02,
        )
        tmp_db.log_snapshot(snapshot)
        # No error = success (we don't have a get_snapshots method yet)


class TestSignalOperations:
    def test_log_and_retrieve(self, tmp_db, sample_signal, sample_market):
        tmp_db.upsert_market(sample_market)  # FK requires parent market
        row_id = tmp_db.log_signal(sample_signal)
        assert row_id > 0
        signals = tmp_db.get_recent_signals(limit=10)
        assert len(signals) == 1
        assert signals[0]["strategy"] == "ai_probability"
        assert signals[0]["edge"] == 0.08

    def test_multiple_signals(self, tmp_db, sample_signal, sample_market):
        tmp_db.upsert_market(sample_market)
        tmp_db.log_signal(sample_signal)
        s2 = sample_signal.model_copy()
        s2.edge = 0.12
        s2.market_id = sample_market.ticker  # Same market (FK)
        tmp_db.log_signal(s2)
        signals = tmp_db.get_recent_signals()
        assert len(signals) == 2


class TestTradeOperations:
    def _insert_prereqs(self, tmp_db):
        """Insert prerequisite market for FK constraints."""
        from src.core.models import Market, MarketToken, MarketCategory
        m = Market(ticker="MKT-001", question="Test?", category=MarketCategory.OTHER,
                   tokens=[MarketToken(token_id="MKT-001_yes", outcome="Yes", price=0.5)],
                   volume_24h=50000, active=True)
        tmp_db.upsert_market(m)

    def _insert_prereqs_multi(self, tmp_db):
        from src.core.models import Market, MarketToken, MarketCategory
        for mid in ["M1", "M2"]:
            m = Market(ticker=mid, question=f"Test {mid}?", category=MarketCategory.OTHER,
                       tokens=[MarketToken(token_id=f"{mid}_yes", outcome="Yes", price=0.5)],
                       volume_24h=50000, active=True)
            tmp_db.upsert_market(m)

    def test_log_trade(self, tmp_db):
        self._insert_prereqs(tmp_db)
        trade = Trade(
            order_id="ord_001",
            market_id="MKT-001",
            token_id="MKT-001_yes",
            side=Side.BUY,
            price=0.34,
            size=50.0,
            fee=0.0,
            realized_pnl=0.0,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
        )
        row_id = tmp_db.log_trade(trade)
        assert row_id > 0

    def test_daily_pnl(self, tmp_db):
        self._insert_prereqs_multi(tmp_db)
        t1 = Trade(
            order_id="o1", market_id="M1", token_id="M1_yes",
            side=Side.BUY, price=0.34, size=50,
            realized_pnl=5.0, strategy=StrategyName.AI_PROBABILITY,
        )
        t2 = Trade(
            order_id="o2", market_id="M2", token_id="M2_yes",
            side=Side.BUY, price=0.50, size=30,
            realized_pnl=-3.0, strategy=StrategyName.AI_PROBABILITY,
        )
        tmp_db.log_trade(t1)
        tmp_db.log_trade(t2)
        pnl = tmp_db.get_daily_pnl()
        assert abs(pnl - 2.0) < 0.01

    def test_trades_today(self, tmp_db):
        self._insert_prereqs_multi(tmp_db)
        trade = Trade(
            order_id="o1", market_id="M1", token_id="M1_yes",
            side=Side.BUY, price=0.40, size=25,
            strategy=StrategyName.OBVIOUS_NO,
        )
        tmp_db.log_trade(trade)
        today = tmp_db.get_trades_today()
        assert len(today) == 1


class TestRecentTradeDedup:
    """Tests for has_recent_trade() dedup method."""

    def _insert_prereqs(self, tmp_db):
        from src.core.models import Market, MarketToken, MarketCategory
        m = Market(ticker="MKT-001", question="Test?", category=MarketCategory.OTHER,
                   tokens=[MarketToken(token_id="MKT-001_yes", outcome="Yes", price=0.5)],
                   volume_24h=50000, active=True)
        tmp_db.upsert_market(m)

    def test_no_trades_returns_false(self, tmp_db):
        assert tmp_db.has_recent_trade("MKT-001") is False

    def test_recent_buy_returns_true(self, tmp_db):
        self._insert_prereqs(tmp_db)
        trade = Trade(
            order_id="ord_dedup", market_id="MKT-001", token_id="MKT-001_yes",
            side=Side.BUY, price=0.34, size=50.0, fee=0.0, realized_pnl=0.0,
            strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        tmp_db.log_trade(trade)
        assert tmp_db.has_recent_trade("MKT-001") is True

    def test_sell_trade_not_counted(self, tmp_db):
        """Sell trades should not trigger dedup — only BUY entries."""
        self._insert_prereqs(tmp_db)
        trade = Trade(
            order_id="ord_sell", market_id="MKT-001", token_id="MKT-001_yes",
            side=Side.SELL, price=0.50, size=50.0, fee=0.0, realized_pnl=5.0,
            strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        tmp_db.log_trade(trade)
        assert tmp_db.has_recent_trade("MKT-001") is False

    def test_old_trade_not_counted(self, tmp_db):
        """Trade older than the lookback window should not trigger dedup."""
        self._insert_prereqs(tmp_db)
        from datetime import timedelta
        old_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        trade = Trade(
            order_id="ord_old", market_id="MKT-001", token_id="MKT-001_yes",
            side=Side.BUY, price=0.34, size=50.0, fee=0.0, realized_pnl=0.0,
            strategy=StrategyName.AI_PROBABILITY, paper=True,
            timestamp=old_time,
        )
        tmp_db.log_trade(trade)
        assert tmp_db.has_recent_trade("MKT-001", seconds=300) is False

    def test_different_market_not_counted(self, tmp_db):
        """Trade on a different market should not trigger dedup."""
        self._insert_prereqs(tmp_db)
        trade = Trade(
            order_id="ord_diff", market_id="MKT-001", token_id="MKT-001_yes",
            side=Side.BUY, price=0.34, size=50.0, fee=0.0, realized_pnl=0.0,
            strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        tmp_db.log_trade(trade)
        assert tmp_db.has_recent_trade("MKT-OTHER") is False


class TestCalibrationOperations:
    def _insert_market(self, tmp_db, mid):
        from src.core.models import Market, MarketToken, MarketCategory
        m = Market(ticker=mid, question=f"Test {mid}?", category=MarketCategory.OTHER,
                   tokens=[MarketToken(token_id=f"{mid}_yes", outcome="Yes", price=0.5)],
                   volume_24h=50000, active=True)
        tmp_db.upsert_market(m)

    def test_log_calibration(self, tmp_db):
        self._insert_market(tmp_db, "M1")
        record = CalibrationRecord(
            market_id="M1",
            market_question="Will X happen?",
            predicted_probability=0.65,
            market_price_at_prediction=0.50,
        )
        row_id = tmp_db.log_calibration(record)
        assert row_id > 0

    def test_unresolved_predictions(self, tmp_db):
        self._insert_market(tmp_db, "M1")
        self._insert_market(tmp_db, "M2")
        r1 = CalibrationRecord(
            market_id="M1",
            predicted_probability=0.65,
            market_price_at_prediction=0.50,
        )
        r2 = CalibrationRecord(
            market_id="M2",
            predicted_probability=0.30,
            market_price_at_prediction=0.40,
            actual_outcome=False,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(r1)
        tmp_db.log_calibration(r2)
        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["market_id"] == "M1"


class TestStats:
    def test_empty_stats(self, tmp_db):
        stats = tmp_db.get_stats()
        assert stats["active_markets"] == 0
        assert stats["total_trades"] == 0
        assert stats["total_pnl"] == 0.0

    def test_stats_after_data(self, tmp_db, sample_market, sample_signal):
        tmp_db.upsert_market(sample_market)
        tmp_db.log_signal(sample_signal)
        # Insert market for trade FK
        from src.core.models import Market, MarketToken, MarketCategory
        m = Market(ticker="M1", question="Test?", category=MarketCategory.OTHER,
                   tokens=[MarketToken(token_id="M1_yes", outcome="Yes", price=0.5)],
                   volume_24h=50000, active=True)
        tmp_db.upsert_market(m)
        trade = Trade(
            order_id="o1", market_id="M1", token_id="M1_yes",
            side=Side.BUY, price=0.40, size=25,
            realized_pnl=8.50,
            strategy=StrategyName.AI_PROBABILITY,
        )
        tmp_db.log_trade(trade)
        stats = tmp_db.get_stats()
        assert stats["active_markets"] == 2  # sample_market + M1
        assert stats["total_signals"] == 1
        assert stats["total_trades"] == 1
        assert abs(stats["total_pnl"] - 8.50) < 0.01
