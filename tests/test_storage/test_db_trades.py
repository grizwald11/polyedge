"""Comprehensive unit tests for src/storage/db_trades.TradesMixin.

Covers signal CRUD, trade logging (including duplicate handling),
recent-trade/exit time-window checks, daily P&L, portfolio summary,
pending order round-trips, position P&L computation, and exit reason logging.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import (
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
from src.storage.database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_market(ticker: str = "TEST-MKT", **kwargs) -> Market:
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


# ===========================================================================
# Signal Operations
# ===========================================================================


class TestLogSignal:
    """Tests for log_signal and get_recent_signals."""

    def test_log_and_retrieve(self, tmp_db: Database):
        sig = _make_signal()
        row_id = tmp_db.log_signal(sig)
        assert row_id >= 1

        rows = tmp_db.get_recent_signals(limit=10)
        assert len(rows) == 1
        assert rows[0]["market_id"] == "TEST-MKT"
        assert rows[0]["strategy"] == "ai_probability"
        assert rows[0]["direction"] == "BUY_YES"
        assert float(rows[0]["edge"]) == pytest.approx(0.08)
        assert rows[0]["acted_on"] == 0

    def test_multiple_signals_ordered_by_timestamp(self, tmp_db: Database):
        """Recent signals come first (DESC order)."""
        old = _make_signal(
            market_id="MKT-OLD",
            timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        new = _make_signal(
            market_id="MKT-NEW",
            timestamp=datetime.now(timezone.utc),
        )
        tmp_db.log_signal(old)
        tmp_db.log_signal(new)

        rows = tmp_db.get_recent_signals(limit=10)
        assert len(rows) == 2
        assert rows[0]["market_id"] == "MKT-NEW"
        assert rows[1]["market_id"] == "MKT-OLD"

    def test_limit_respected(self, tmp_db: Database):
        for i in range(5):
            tmp_db.log_signal(_make_signal(market_id=f"MKT-{i}"))
        rows = tmp_db.get_recent_signals(limit=3)
        assert len(rows) == 3

    def test_platform_stored(self, tmp_db: Database):
        sig = _make_signal(platform=Platform.KALSHI)
        tmp_db.log_signal(sig)
        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["platform"] == "kalshi"


class TestUpdateSignalActedOn:

    def test_marks_acted_on(self, tmp_db: Database):
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_acted_on(sig_id, "ORD-42")

        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["acted_on"] == 1
        assert rows[0]["order_id"] == "ORD-42"
        assert rows[0]["status"] == "executed"


class TestUpdateSignalRiskResult:

    def test_risk_passed(self, tmp_db: Database):
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(sig_id, passed=True, failed_checks=[], warnings=["low_liq"])

        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["risk_passed"] == 1
        assert rows[0]["risk_failed_checks"] == ""
        assert rows[0]["risk_warnings"] == "low_liq"
        assert rows[0]["status"] == "generated"

    def test_risk_gated(self, tmp_db: Database):
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(
            sig_id, passed=False,
            failed_checks=["max_exposure", "daily_loss"],
            warnings=[],
        )

        rows = tmp_db.get_recent_signals(limit=1)
        assert rows[0]["risk_passed"] == 0
        assert rows[0]["risk_failed_checks"] == "max_exposure, daily_loss"
        assert rows[0]["status"] == "risk_gated"


class TestGetSkippedSignals:

    def test_returns_risk_gated_for_date(self, tmp_db: Database):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(
            sig_id, passed=False,
            failed_checks=["max_exposure"],
            warnings=[],
        )
        # Also log a passed signal -- should NOT appear
        passed_id = tmp_db.log_signal(_make_signal(market_id="PASSED-MKT"))
        tmp_db.update_signal_risk_result(passed_id, passed=True, failed_checks=[], warnings=[])

        skipped = tmp_db.get_skipped_signals_for_date(today)
        assert len(skipped) == 1
        assert skipped[0]["market_id"] == "TEST-MKT"
        assert skipped[0]["risk_failed_checks"] == "max_exposure"

    def test_defaults_to_today(self, tmp_db: Database):
        sig_id = tmp_db.log_signal(_make_signal())
        tmp_db.update_signal_risk_result(
            sig_id, passed=False, failed_checks=["check"], warnings=[],
        )
        skipped = tmp_db.get_skipped_signals_for_date()  # no date arg
        assert len(skipped) == 1

    def test_excludes_other_dates(self, tmp_db: Database):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        sig = _make_signal(
            timestamp=datetime.now(timezone.utc) - timedelta(days=1),
        )
        sig_id = tmp_db.log_signal(sig)
        tmp_db.update_signal_risk_result(
            sig_id, passed=False, failed_checks=["check"], warnings=[],
        )
        # Query for today should return nothing
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert tmp_db.get_skipped_signals_for_date(today) == []
        # Query for yesterday should find it
        assert len(tmp_db.get_skipped_signals_for_date(yesterday)) == 1


# ===========================================================================
# Trade Operations
# ===========================================================================


class TestLogTrade:

    def test_basic_log(self, tmp_db: Database):
        trade = _make_trade()
        row_id = tmp_db.log_trade(trade)
        assert row_id >= 1

    def test_duplicate_order_id_and_side_ignored(self, tmp_db: Database):
        """INSERT OR IGNORE on (order_id, side) unique index."""
        t1 = _make_trade(order_id="ORD-DUP", side=Side.BUY, price=0.60)
        t2 = _make_trade(order_id="ORD-DUP", side=Side.BUY, price=0.65)

        tmp_db.log_trade(t1)
        tmp_db.log_trade(t2)

        conn = tmp_db._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE order_id='ORD-DUP'"
        ).fetchone()["cnt"]
        assert count == 1

        # Verify it kept the first trade's price
        row = conn.execute(
            "SELECT price FROM trades WHERE order_id='ORD-DUP'"
        ).fetchone()
        assert float(row["price"]) == pytest.approx(0.60)

    def test_same_order_id_different_side_allowed(self, tmp_db: Database):
        """BUY and SELL for same order_id are separate rows."""
        tmp_db.log_trade(_make_trade(order_id="ORD-X", side=Side.BUY))
        tmp_db.log_trade(_make_trade(order_id="ORD-X", side=Side.SELL))

        conn = tmp_db._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE order_id='ORD-X'"
        ).fetchone()["cnt"]
        assert count == 2

    def test_platform_stored(self, tmp_db: Database):
        trade = _make_trade(platform=Platform.KALSHI)
        tmp_db.log_trade(trade)
        conn = tmp_db._get_conn()
        row = conn.execute("SELECT platform FROM trades LIMIT 1").fetchone()
        assert row["platform"] == "kalshi"


class TestHasRecentTrade:

    def test_recent_buy_detected(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(side=Side.BUY))
        assert tmp_db.has_recent_trade("TEST-MKT", seconds=300) is True

    def test_old_buy_not_detected(self, tmp_db: Database):
        old_trade = _make_trade(
            side=Side.BUY,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=600),
        )
        tmp_db.log_trade(old_trade)
        assert tmp_db.has_recent_trade("TEST-MKT", seconds=300) is False

    def test_sell_not_counted(self, tmp_db: Database):
        """has_recent_trade only looks at BUY trades."""
        tmp_db.log_trade(_make_trade(side=Side.SELL))
        assert tmp_db.has_recent_trade("TEST-MKT", seconds=300) is False

    def test_different_market_not_counted(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(market_id="OTHER-MKT", side=Side.BUY))
        assert tmp_db.has_recent_trade("TEST-MKT", seconds=300) is False

    def test_no_trades_returns_false(self, tmp_db: Database):
        assert tmp_db.has_recent_trade("TEST-MKT") is False


class TestHasRecentExit:

    def test_recent_sell_detected(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(side=Side.SELL))
        assert tmp_db.has_recent_exit("TEST-MKT", seconds=300) is True

    def test_old_sell_not_detected(self, tmp_db: Database):
        old_trade = _make_trade(
            side=Side.SELL,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=600),
        )
        tmp_db.log_trade(old_trade)
        assert tmp_db.has_recent_exit("TEST-MKT", seconds=300) is False

    def test_buy_not_counted(self, tmp_db: Database):
        """has_recent_exit only looks at SELL trades."""
        tmp_db.log_trade(_make_trade(side=Side.BUY))
        assert tmp_db.has_recent_exit("TEST-MKT", seconds=300) is False

    def test_different_market_not_counted(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(market_id="OTHER-MKT", side=Side.SELL))
        assert tmp_db.has_recent_exit("TEST-MKT", seconds=300) is False


class TestGetTradesToday:

    def test_returns_todays_trades(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(order_id="ORD-TODAY"))
        trades = tmp_db.get_trades_today()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "ORD-TODAY"

    def test_excludes_old_trades(self, tmp_db: Database):
        old = _make_trade(
            order_id="ORD-OLD",
            timestamp=datetime.now(timezone.utc) - timedelta(days=2),
        )
        tmp_db.log_trade(old)
        assert tmp_db.get_trades_today() == []


class TestGetTradesForDate:

    def test_specific_date(self, tmp_db: Database):
        target_date = "2026-03-15"
        trade = _make_trade(
            order_id="ORD-315",
            timestamp=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        tmp_db.log_trade(trade)
        trades = tmp_db.get_trades_for_date(target_date)
        assert len(trades) == 1
        assert trades[0]["order_id"] == "ORD-315"

    def test_excludes_adjacent_dates(self, tmp_db: Database):
        trade = _make_trade(
            order_id="ORD-314",
            timestamp=datetime(2026, 3, 14, 23, 59, 0, tzinfo=timezone.utc),
        )
        tmp_db.log_trade(trade)
        assert tmp_db.get_trades_for_date("2026-03-15") == []

    def test_defaults_to_today(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade())
        trades = tmp_db.get_trades_for_date()  # no arg
        assert len(trades) == 1


class TestGetDailyPnl:

    def test_sums_realized_pnl(self, tmp_db: Database):
        target = "2026-04-01"
        ts = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        tmp_db.log_trade(_make_trade(order_id="O1", realized_pnl=5.50, timestamp=ts))
        tmp_db.log_trade(_make_trade(order_id="O2", realized_pnl=-2.25, timestamp=ts, side=Side.SELL))
        pnl = tmp_db.get_daily_pnl(target)
        assert pnl == pytest.approx(3.25)

    def test_no_trades_returns_zero(self, tmp_db: Database):
        assert tmp_db.get_daily_pnl("2026-01-01") == pytest.approx(0.0)

    def test_defaults_to_today(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(realized_pnl=1.0))
        pnl = tmp_db.get_daily_pnl()
        assert pnl == pytest.approx(1.0)


# ===========================================================================
# Position with P&L
# ===========================================================================


class TestGetPositionsWithPnl:

    def test_open_position_from_buy(self, tmp_db: Database):
        """A single BUY with no SELL shows as an open position."""
        # Insert market + snapshot so the join works
        mkt = _make_market(ticker="POS-MKT")
        tmp_db.upsert_market(mkt)
        snap = MarketSnapshot(
            market_id="POS-MKT",
            yes_price=0.70,
            no_price=0.30,
            spread=0.02,
        )
        tmp_db.log_snapshot(snap)

        tmp_db.log_trade(_make_trade(
            market_id="POS-MKT",
            order_id="ORD-BUY",
            token_id="POS-MKT_yes",
            side=Side.BUY,
            price=0.60,
            size=10,
        ))

        positions = tmp_db.get_positions_with_pnl()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["market_id"] == "POS-MKT"
        assert pos["direction"] == "BUY_YES"
        assert pos["size"] == 10
        assert pos["avg_entry"] == pytest.approx(0.60)
        assert pos["current_price"] == pytest.approx(0.70)
        # unrealized = (0.70 - 0.60) * 10 = 1.0
        assert pos["unrealized_pnl"] == pytest.approx(1.0)

    def test_no_position_detects_suffix(self, tmp_db: Database):
        """A BUY on a _no token is recognized as a NO position."""
        mkt = _make_market(ticker="NO-MKT")
        tmp_db.upsert_market(mkt)
        snap = MarketSnapshot(market_id="NO-MKT", yes_price=0.60, no_price=0.40, spread=0.02)
        tmp_db.log_snapshot(snap)

        tmp_db.log_trade(_make_trade(
            market_id="NO-MKT",
            order_id="ORD-NO",
            token_id="NO-MKT_no",
            side=Side.BUY,
            price=0.35,
            size=5,
        ))

        positions = tmp_db.get_positions_with_pnl()
        assert len(positions) == 1
        assert positions[0]["direction"] == "BUY_NO"
        assert positions[0]["current_price"] == pytest.approx(0.40)
        # unrealized = (0.40 - 0.35) * 5 = 0.25
        assert positions[0]["unrealized_pnl"] == pytest.approx(0.25)

    def test_fully_closed_position_excluded(self, tmp_db: Database):
        """BUY 10 + SELL 10 = net 0 -- should not appear."""
        tmp_db.log_trade(_make_trade(order_id="O-BUY", side=Side.BUY, size=10))
        tmp_db.log_trade(_make_trade(order_id="O-SELL", side=Side.SELL, size=10))

        positions = tmp_db.get_positions_with_pnl()
        assert positions == []

    def test_partial_close(self, tmp_db: Database):
        """BUY 10 + SELL 3 = net 7 open."""
        mkt = _make_market(ticker="PART-MKT")
        tmp_db.upsert_market(mkt)
        snap = MarketSnapshot(market_id="PART-MKT", yes_price=0.65, no_price=0.35, spread=0.02)
        tmp_db.log_snapshot(snap)

        tmp_db.log_trade(_make_trade(
            market_id="PART-MKT", order_id="BUY1",
            token_id="PART-MKT_yes", side=Side.BUY, price=0.60, size=10,
        ))
        tmp_db.log_trade(_make_trade(
            market_id="PART-MKT", order_id="SELL1",
            token_id="PART-MKT_yes", side=Side.SELL, price=0.65, size=3,
        ))

        positions = tmp_db.get_positions_with_pnl()
        assert len(positions) == 1
        assert positions[0]["size"] == 7

    def test_no_snapshot_returns_zero_price(self, tmp_db: Database):
        """Without a snapshot, current_price defaults to 0."""
        tmp_db.log_trade(_make_trade(order_id="NO-SNAP", side=Side.BUY, size=5))
        positions = tmp_db.get_positions_with_pnl()
        assert len(positions) == 1
        assert positions[0]["current_price"] == pytest.approx(0.0)
        assert positions[0]["unrealized_pnl"] == pytest.approx(0.0)


# ===========================================================================
# Portfolio Summary
# ===========================================================================


class TestGetPortfolioSummary:

    def test_empty_portfolio(self, tmp_db: Database):
        summary = tmp_db.get_portfolio_summary()
        assert summary["total_trades"] == 0
        assert summary["total_pnl"] == pytest.approx(0.0)
        assert summary["winning_trades"] == 0

    def test_counts_and_pnl(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(order_id="W1", realized_pnl=3.0))
        tmp_db.log_trade(_make_trade(order_id="W2", realized_pnl=1.5, side=Side.SELL))
        tmp_db.log_trade(_make_trade(order_id="L1", realized_pnl=-2.0))

        summary = tmp_db.get_portfolio_summary()
        assert summary["total_trades"] == 3
        assert summary["total_pnl"] == pytest.approx(2.5)
        assert summary["winning_trades"] == 2  # W1 and W2

    def test_zero_pnl_not_counted_as_win(self, tmp_db: Database):
        tmp_db.log_trade(_make_trade(order_id="Z1", realized_pnl=0.0))
        summary = tmp_db.get_portfolio_summary()
        assert summary["winning_trades"] == 0


# ===========================================================================
# Pending Orders
# ===========================================================================


class TestPendingOrders:

    def test_save_and_load_roundtrip(self, tmp_db: Database):
        tmp_db.save_pending_order("ORD-A", 15.50)
        tmp_db.save_pending_order("ORD-B", 22.00)

        pending = tmp_db.load_pending_orders()
        assert len(pending) == 2
        assert pending["ORD-A"] == pytest.approx(15.50)
        assert pending["ORD-B"] == pytest.approx(22.00)

    def test_delete_removes_order(self, tmp_db: Database):
        tmp_db.save_pending_order("ORD-DEL", 10.0)
        tmp_db.delete_pending_order("ORD-DEL")

        pending = tmp_db.load_pending_orders()
        assert "ORD-DEL" not in pending

    def test_delete_nonexistent_is_noop(self, tmp_db: Database):
        """Deleting an order that doesn't exist should not raise."""
        tmp_db.delete_pending_order("GHOST-ORDER")
        assert tmp_db.load_pending_orders() == {}

    def test_save_replace_updates_cost(self, tmp_db: Database):
        """INSERT OR REPLACE overwrites existing row."""
        tmp_db.save_pending_order("ORD-UPD", 10.0)
        tmp_db.save_pending_order("ORD-UPD", 20.0)

        pending = tmp_db.load_pending_orders()
        assert len(pending) == 1
        assert pending["ORD-UPD"] == pytest.approx(20.0)

    def test_empty_load(self, tmp_db: Database):
        assert tmp_db.load_pending_orders() == {}


# ===========================================================================
# Exit Reason Logging
# ===========================================================================


class TestLogExitReason:

    def test_basic_log(self, tmp_db: Database):
        tmp_db.log_exit_reason(
            market_id="EXIT-MKT",
            exit_reason="edge_gone",
            exit_price=0.55,
            position_size=10.0,
            realized_pnl=-0.50,
            strategy="ai_probability",
            platform="kalshi",
        )

        conn = tmp_db._get_conn()
        row = conn.execute(
            "SELECT * FROM position_exits WHERE market_id='EXIT-MKT'"
        ).fetchone()
        assert row is not None
        assert row["exit_reason"] == "edge_gone"
        assert float(row["exit_price"]) == pytest.approx(0.55)
        assert float(row["position_size"]) == pytest.approx(10.0)
        assert float(row["realized_pnl"]) == pytest.approx(-0.50)
        assert row["strategy"] == "ai_probability"
        assert row["platform"] == "kalshi"
        assert row["timestamp"] is not None

    def test_multiple_exits_same_market(self, tmp_db: Database):
        """Can log multiple exits for the same market (different times)."""
        tmp_db.log_exit_reason(market_id="RE-ENTRY", exit_reason="stop_loss")
        tmp_db.log_exit_reason(market_id="RE-ENTRY", exit_reason="time_exit")

        conn = tmp_db._get_conn()
        rows = conn.execute(
            "SELECT * FROM position_exits WHERE market_id='RE-ENTRY'"
        ).fetchall()
        assert len(rows) == 2

    def test_default_values(self, tmp_db: Database):
        """Default numeric fields are 0, default strings are empty."""
        tmp_db.log_exit_reason(market_id="DEF-MKT", exit_reason="manual")

        conn = tmp_db._get_conn()
        row = conn.execute(
            "SELECT * FROM position_exits WHERE market_id='DEF-MKT'"
        ).fetchone()
        assert float(row["exit_price"]) == pytest.approx(0.0)
        assert float(row["position_size"]) == pytest.approx(0.0)
        assert float(row["realized_pnl"]) == pytest.approx(0.0)
        assert row["strategy"] == ""
        assert row["platform"] == "kalshi"
