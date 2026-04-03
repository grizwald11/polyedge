"""Tests for tax report export script (M-14)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.export_tax_report import (
    TaxLot,
    compute_summary,
    export_tax_report,
    generate_settlement_lots,
    match_trades_fifo,
    query_trades,
    write_csv,
)
from src.storage.database import Database


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _insert_trade(
    db: Database,
    market_id: str,
    side: str,
    price: float,
    size: float,
    strategy: str = "ai_probability",
    fee: float = 0.0,
    paper: int = 1,
    days_ago: int = 5,
    timestamp: str | None = None,
    token_id: str | None = None,
    order_id: str | None = None,
):
    """Insert a trade directly into the database."""
    conn = db._get_conn()
    if timestamp is None:
        timestamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    if token_id is None:
        token_id = f"{market_id}_yes"
    if order_id is None:
        order_id = f"order-{market_id}-{side}-{days_ago}"
    conn.execute("""
        INSERT INTO trades (order_id, market_id, token_id, side, price, size, fee,
                           realized_pnl, strategy, paper, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id,
        market_id,
        token_id,
        side,
        price,
        size,
        fee,
        0.0,
        strategy,
        paper,
        timestamp,
    ))
    conn.commit()


def _insert_market(
    db: Database,
    ticker: str,
    question: str = "Test market?",
    result: str = "",
    end_date: str = "",
):
    """Insert a market into the database for join queries."""
    conn = db._get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO markets (ticker, platform, question, description, category,
                                        end_date, result, first_seen, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, "kalshi", question, "", "Other", end_date, result, now, now))
    conn.commit()


# ──────────────────────────────────────
# TaxLot Tests
# ──────────────────────────────────────

class TestTaxLot:
    def test_gain_loss_computed(self):
        lot = TaxLot(
            market_id="MKT-1",
            description="Test (YES)",
            side="YES",
            strategy="ai_probability",
            contracts=10.0,
            cost_basis=5.0,
            proceeds=8.0,
            date_acquired="2026-01-01",
            date_sold="2026-02-01",
        )
        assert lot.gain_loss == 3.0
        assert lot.term == "Short-term"

    def test_long_term_classification(self):
        lot = TaxLot(
            market_id="MKT-1",
            description="Test (YES)",
            side="YES",
            strategy="ai_probability",
            contracts=10.0,
            cost_basis=5.0,
            proceeds=8.0,
            date_acquired="2025-01-01",
            date_sold="2026-06-01",
        )
        assert lot.term == "Long-term"

    def test_exactly_365_days_is_short_term(self):
        lot = TaxLot(
            market_id="MKT-1",
            description="Test (YES)",
            side="YES",
            strategy="ai_probability",
            contracts=10.0,
            cost_basis=5.0,
            proceeds=8.0,
            date_acquired="2025-01-01",
            date_sold="2026-01-01",
        )
        assert lot.term == "Short-term"

    def test_negative_gain_loss(self):
        lot = TaxLot(
            market_id="MKT-1",
            description="Test (YES)",
            side="YES",
            strategy="ai_probability",
            contracts=10.0,
            cost_basis=8.0,
            proceeds=3.0,
            date_acquired="2026-01-01",
            date_sold="2026-02-01",
        )
        assert lot.gain_loss == -5.0


# ──────────────────────────────────────
# Query Tests
# ──────────────────────────────────────

class TestQueryTrades:
    def test_query_all_trades(self, tmp_db):
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, days_ago=5)
        _insert_trade(tmp_db, "MKT-A", "SELL", 0.60, 10, days_ago=2)

        trades = query_trades(tmp_db)
        assert len(trades) == 2

    def test_query_with_date_filter(self, tmp_db):
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, timestamp="2026-01-15T12:00:00+00:00", order_id="buy-jan")
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.50, 5, timestamp="2026-03-15T12:00:00+00:00", order_id="buy-mar")

        trades = query_trades(tmp_db, start_date="2026-03-01", end_date="2026-04-01")
        assert len(trades) == 1
        assert trades[0]["price"] == 0.50

    def test_query_live_only(self, tmp_db):
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, paper=1, days_ago=5)
        _insert_trade(tmp_db, "MKT-B", "BUY", 0.50, 5, paper=0, days_ago=3)

        trades = query_trades(tmp_db, live_only=True)
        assert len(trades) == 1
        assert trades[0]["market_id"] == "MKT-B"

    def test_query_joins_market_question(self, tmp_db):
        _insert_market(tmp_db, "MKT-A", question="Will X happen?")
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, days_ago=5)

        trades = query_trades(tmp_db)
        assert trades[0]["market_question"] == "Will X happen?"

    def test_query_without_market_uses_market_id(self, tmp_db):
        _insert_trade(tmp_db, "MKT-ORPHAN", "BUY", 0.40, 10, days_ago=5)

        trades = query_trades(tmp_db)
        assert trades[0]["market_question"] == "MKT-ORPHAN"

    def test_query_ordered_ascending(self, tmp_db):
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, timestamp="2026-01-10T00:00:00+00:00")
        _insert_trade(tmp_db, "MKT-A", "SELL", 0.60, 10, timestamp="2026-01-05T00:00:00+00:00")

        trades = query_trades(tmp_db)
        # Earlier trade first (ascending)
        assert trades[0]["side"] == "SELL"
        assert trades[1]["side"] == "BUY"


# ──────────────────────────────────────
# FIFO Matching Tests
# ──────────────────────────────────────

class TestMatchTradesFifo:
    def test_simple_buy_sell(self):
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.40, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "SELL",
                "price": 0.60, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-20T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 1
        assert lots[0].cost_basis == 4.0  # 0.40 * 10
        assert lots[0].proceeds == 6.0    # 0.60 * 10
        assert lots[0].gain_loss == 2.0

    def test_partial_sell(self):
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.40, "size": 20.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "SELL",
                "price": 0.60, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-20T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 1
        assert lots[0].contracts == 10.0
        assert lots[0].cost_basis == 4.0  # 0.40 * 10

    def test_multiple_buys_one_sell(self):
        """FIFO: sell matches first buy, then second buy."""
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.30, "size": 5.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-05T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.50, "size": 5.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "SELL",
                "price": 0.70, "size": 8.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-20T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 2
        # First lot: 5 contracts from first buy at 0.30
        assert lots[0].contracts == 5.0
        assert lots[0].cost_basis == 1.5  # 0.30 * 5
        assert lots[0].proceeds == 3.5    # 0.70 * 5
        # Second lot: 3 contracts from second buy at 0.50
        assert lots[1].contracts == 3.0
        assert lots[1].cost_basis == 1.5  # 0.50 * 3
        assert lots[1].proceeds == pytest.approx(2.1, abs=0.01)  # 0.70 * 3

    def test_with_fees(self):
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.40, "size": 10.0, "fee": 0.50, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "SELL",
                "price": 0.60, "size": 10.0, "fee": 0.30, "strategy": "ai_probability",
                "timestamp": "2026-01-20T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 1
        assert lots[0].cost_basis == 4.50  # 0.40 * 10 + 0.50
        assert lots[0].proceeds == 5.70    # 0.60 * 10 - 0.30
        assert lots[0].gain_loss == pytest.approx(1.20, abs=0.01)

    def test_no_sells_produces_no_lots(self):
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.40, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 0

    def test_different_markets_separate(self):
        trades = [
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "BUY",
                "price": 0.40, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will X?"
            },
            {
                "market_id": "MKT-B", "token_id": "MKT-B_yes", "side": "BUY",
                "price": 0.30, "size": 5.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-10T00:00:00", "market_question": "Will Y?"
            },
            {
                "market_id": "MKT-A", "token_id": "MKT-A_yes", "side": "SELL",
                "price": 0.60, "size": 10.0, "fee": 0.0, "strategy": "ai_probability",
                "timestamp": "2026-01-20T00:00:00", "market_question": "Will X?"
            },
        ]
        lots = match_trades_fifo(trades)
        assert len(lots) == 1
        assert lots[0].market_id == "MKT-A"

    def test_empty_trades(self):
        lots = match_trades_fifo([])
        assert lots == []


# ──────────────────────────────────────
# Settlement Lots Tests
# ──────────────────────────────────────

class TestSettlementLots:
    def test_settled_yes_winning(self, tmp_db):
        _insert_market(tmp_db, "MKT-SETTLED", question="Did X happen?", result="yes", end_date="2026-02-01")
        _insert_trade(tmp_db, "MKT-SETTLED", "BUY", 0.40, 10, token_id="MKT-SETTLED_yes",
                      timestamp="2026-01-10T00:00:00+00:00")

        trades = query_trades(tmp_db)
        lots = generate_settlement_lots(trades, tmp_db)
        assert len(lots) == 1
        assert lots[0].proceeds == 10.0  # 1.0 * 10
        assert lots[0].cost_basis == 4.0  # 0.40 * 10
        assert lots[0].gain_loss == 6.0

    def test_settled_yes_losing(self, tmp_db):
        _insert_market(tmp_db, "MKT-SETTLED", question="Did X happen?", result="no", end_date="2026-02-01")
        _insert_trade(tmp_db, "MKT-SETTLED", "BUY", 0.40, 10, token_id="MKT-SETTLED_yes",
                      timestamp="2026-01-10T00:00:00+00:00")

        trades = query_trades(tmp_db)
        lots = generate_settlement_lots(trades, tmp_db)
        assert len(lots) == 1
        assert lots[0].proceeds == 0.0
        assert lots[0].cost_basis == 4.0
        assert lots[0].gain_loss == -4.0

    def test_unresolved_market_skipped(self, tmp_db):
        _insert_market(tmp_db, "MKT-OPEN", question="Open market?", result="")
        _insert_trade(tmp_db, "MKT-OPEN", "BUY", 0.40, 10, token_id="MKT-OPEN_yes",
                      timestamp="2026-01-10T00:00:00+00:00")

        trades = query_trades(tmp_db)
        lots = generate_settlement_lots(trades, tmp_db)
        assert len(lots) == 0

    def test_partially_sold_settlement(self, tmp_db):
        """If 10 bought and 6 sold, settlement should cover remaining 4."""
        _insert_market(tmp_db, "MKT-PART", question="Partial?", result="yes", end_date="2026-02-01")
        _insert_trade(tmp_db, "MKT-PART", "BUY", 0.40, 10, token_id="MKT-PART_yes",
                      timestamp="2026-01-10T00:00:00+00:00", order_id="buy-1")
        _insert_trade(tmp_db, "MKT-PART", "SELL", 0.50, 6, token_id="MKT-PART_yes",
                      timestamp="2026-01-15T00:00:00+00:00", order_id="sell-1")

        trades = query_trades(tmp_db)
        lots = generate_settlement_lots(trades, tmp_db)
        assert len(lots) == 1
        assert lots[0].contracts == 4.0
        assert lots[0].proceeds == 4.0  # 1.0 * 4
        assert lots[0].cost_basis == pytest.approx(1.6, abs=0.01)  # 0.40 * 4


# ──────────────────────────────────────
# Summary Tests
# ──────────────────────────────────────

class TestComputeSummary:
    def test_empty_lots(self):
        summary = compute_summary([])
        assert summary["num_lots"] == 0
        assert summary["total_gain_loss"] == 0.0

    def test_mixed_gains_losses(self):
        lots = [
            TaxLot("MKT-A", "Test A", "YES", "ai_probability", 10, 4.0, 6.0, "2026-01-01", "2026-01-20"),
            TaxLot("MKT-B", "Test B", "NO", "obvious_no", 5, 4.75, 5.0, "2026-01-01", "2026-01-10"),
            TaxLot("MKT-C", "Test C", "YES", "ai_probability", 10, 7.0, 3.0, "2026-01-01", "2026-01-15"),
        ]
        summary = compute_summary(lots)
        assert summary["num_lots"] == 3
        assert summary["num_winning"] == 2
        assert summary["num_losing"] == 1
        assert summary["total_proceeds"] == pytest.approx(14.0, abs=0.01)
        assert summary["total_cost_basis"] == pytest.approx(15.75, abs=0.01)
        assert summary["total_gain_loss"] == pytest.approx(-1.75, abs=0.01)
        assert summary["short_term_gain_loss"] == pytest.approx(-1.75, abs=0.01)
        assert summary["long_term_gain_loss"] == pytest.approx(0.0, abs=0.01)


# ──────────────────────────────────────
# CSV Output Tests
# ──────────────────────────────────────

class TestWriteCsv:
    def test_write_csv(self, tmp_path):
        lots = [
            TaxLot("MKT-A", "Will X? (YES)", "YES", "ai_probability", 10, 4.0, 6.0, "2026-01-01", "2026-01-20"),
        ]
        output_path = str(tmp_path / "tax_report.csv")
        write_csv(lots, output_path)

        with open(output_path) as f:
            lines = f.readlines()
        assert len(lines) == 2  # Header + 1 data row
        assert "Description" in lines[0]
        assert "Date Acquired" in lines[0]
        assert "Gain/Loss" in lines[0]
        assert "Will X? (YES)" in lines[1]
        assert "2026-01-01" in lines[1]
        assert "2026-01-20" in lines[1]

    def test_csv_sorted_by_date_sold(self, tmp_path):
        lots = [
            TaxLot("MKT-B", "B", "YES", "ai_probability", 5, 2.0, 3.0, "2026-01-01", "2026-03-01"),
            TaxLot("MKT-A", "A", "YES", "ai_probability", 10, 4.0, 6.0, "2026-01-01", "2026-01-20"),
        ]
        output_path = str(tmp_path / "tax_report.csv")
        write_csv(lots, output_path)

        with open(output_path) as f:
            lines = f.readlines()
        # First data row should be the earlier sale date
        assert lines[1].startswith("A,")


# ──────────────────────────────────────
# Integration Test
# ──────────────────────────────────────

class TestExportTaxReport:
    def test_full_export(self, tmp_db, tmp_path):
        _insert_market(tmp_db, "MKT-A", question="Will X happen?")
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, token_id="MKT-A_yes",
                      timestamp="2026-01-10T00:00:00+00:00", order_id="buy-1")
        _insert_trade(tmp_db, "MKT-A", "SELL", 0.60, 10, token_id="MKT-A_yes",
                      timestamp="2026-01-20T00:00:00+00:00", order_id="sell-1")

        output_path = str(tmp_path / "report.csv")
        lots, summary = export_tax_report(
            db_path=tmp_db.db_path,
            year="2026",
            output=output_path,
            include_settlements=False,
        )

        assert len(lots) == 1
        assert summary["total_gain_loss"] == pytest.approx(2.0, abs=0.01)
        assert (tmp_path / "report.csv").exists()

    def test_no_trades_in_range(self, tmp_db):
        _insert_trade(tmp_db, "MKT-A", "BUY", 0.40, 10, timestamp="2025-06-01T00:00:00+00:00")

        lots, summary = export_tax_report(
            db_path=tmp_db.db_path,
            year="2026",
            include_settlements=False,
        )
        assert len(lots) == 0
        assert summary["num_lots"] == 0
