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
        # 4 whales positioned YES, 0 positioned NO → 4/4 = 1.0
        assert consensus.consensus_pct == pytest.approx(1.0)

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

    def test_consensus_uses_positioned_whales_not_basket_size(self, tmp_db):
        """Consensus should be yes_count/(yes+no), not yes_count/basket_size."""
        settings = Settings()
        settings.whales.consensus_threshold = 0.8
        monitor = WhaleMonitor(settings, tmp_db)

        # 10 whales in basket, but only 4 have positions
        for i in range(10):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        # 3 YES + 1 NO = 4 positioned. 3/4 = 75% (below 80%)
        # Old bug: 3/10 = 30%, also no signal. This test validates the denominator.
        for i in range(3):
            monitor.update_positions(f"whale-{i}", {
                "MKT-A": WhalePosition(
                    wallet=f"whale-{i}", market_id="MKT-A",
                    direction=Direction.BUY_YES, entry_price=0.40,
                ),
            })
        monitor.update_positions("whale-3", {
            "MKT-A": WhalePosition(
                wallet="whale-3", market_id="MKT-A",
                direction=Direction.BUY_NO, entry_price=0.60,
            ),
        })

        # 3/4 = 75% < 80% threshold → no signal
        assert monitor.get_consensus("MKT-A") is None

        # Add 1 more YES → 4 YES + 1 NO = 4/5 = 80% → signal
        monitor.update_positions("whale-4", {
            "MKT-A": WhalePosition(
                wallet="whale-4", market_id="MKT-A",
                direction=Direction.BUY_YES, entry_price=0.42,
            ),
        })
        consensus = monitor.get_consensus("MKT-A")
        assert consensus is not None
        assert consensus.consensus_pct == pytest.approx(0.8)
        assert consensus.whale_count == 4

    def test_consensus_requires_minimum_positioned_whales(self, tmp_db):
        """Fewer than 3 positioned whales should not trigger consensus."""
        settings = Settings()
        settings.whales.consensus_threshold = 0.8
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(10):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        # Only 2 whales positioned, both YES → 2/2 = 100% but too few
        for i in range(2):
            monitor.update_positions(f"whale-{i}", {
                "MKT-A": WhalePosition(
                    wallet=f"whale-{i}", market_id="MKT-A",
                    direction=Direction.BUY_YES, entry_price=0.34,
                ),
            })

        assert monitor.get_consensus("MKT-A") is None
