"""Tests for whale monitor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.models import Direction, WhaleWallet
from src.data.whale_monitor import WhaleMonitor, WhalePosition


class TestWhaleMonitor:
    def test_empty_basket(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        assert monitor.basket_size == 0
        assert monitor.get_consensus("MKT-A") is None

    def test_consensus_yes(self, tmp_db):
        settings = Settings()
        settings.whales.consensus_threshold = 0.8
        monitor = WhaleMonitor(settings, tmp_db)

        # Manually add whales to basket
        for i in range(5):
            monitor._basket.append(WhaleWallet(
                address=f"whale-{i}",
                alias=f"Whale {i}",
                win_rate=0.60,
                total_trades=100,
            ))

        # 4/5 whales buy YES on market
        now = datetime.now(timezone.utc)
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "MKT-A": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="MKT-A",
                    direction=Direction.BUY_YES,
                    entry_price=0.34,
                    detected_at=now - timedelta(hours=12),
                ),
            })

        consensus = monitor.get_consensus("MKT-A")
        assert consensus is not None
        assert consensus.direction == Direction.BUY_YES
        assert consensus.whale_count == 4
        assert consensus.consensus_pct == 0.8

    def test_no_consensus(self, tmp_db):
        settings = Settings()
        settings.whales.consensus_threshold = 0.8
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        # Split: 3 YES, 2 NO — below 80% threshold
        for i in range(3):
            monitor.update_positions(f"whale-{i}", {
                "MKT-A": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="MKT-A",
                    direction=Direction.BUY_YES,
                    entry_price=0.34,
                ),
            })
        for i in range(3, 5):
            monitor.update_positions(f"whale-{i}", {
                "MKT-A": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="MKT-A",
                    direction=Direction.BUY_NO,
                    entry_price=0.66,
                ),
            })

        consensus = monitor.get_consensus("MKT-A")
        assert consensus is None

    def test_get_all_markets(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)
        monitor._basket.append(WhaleWallet(address="whale-1"))
        monitor._basket.append(WhaleWallet(address="whale-2"))

        monitor.update_positions("whale-1", {
            "MKT-A": WhalePosition(wallet="whale-1", market_id="MKT-A", direction=Direction.BUY_YES),
        })
        monitor.update_positions("whale-2", {
            "MKT-B": WhalePosition(wallet="whale-2", market_id="MKT-B", direction=Direction.BUY_NO),
        })

        markets = monitor.get_all_markets_with_positions()
        assert "MKT-A" in markets
        assert "MKT-B" in markets

    def test_new_entry_logged(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)
        monitor._basket.append(WhaleWallet(address="whale-1"))

        monitor.update_positions("whale-1", {
            "MKT-A": WhalePosition(
                wallet="whale-1",
                market_id="MKT-A",
                direction=Direction.BUY_YES,
                entry_price=0.34,
            ),
        })

        # Verify logged to DB
        conn = tmp_db._get_conn()
        rows = conn.execute("SELECT * FROM whale_trades").fetchall()
        assert len(rows) == 1
        assert rows[0]["market_id"] == "MKT-A"
