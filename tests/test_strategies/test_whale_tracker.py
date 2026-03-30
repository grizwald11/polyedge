"""Tests for whale tracker strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketToken,
    StrategyName,
    WhaleSignal,
    WhaleWallet,
)
from src.data.whale_monitor import WhaleMonitor, WhalePosition
from src.strategies.whale_tracker import WhaleTrackerStrategy


def _make_market(ticker="FED-RATE", yes_price=0.34, no_price=0.66):
    return Market(
        ticker=ticker,
        question="Will the Fed cut rates?",
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
    )


class TestWhaleTrackerStrategy:
    def test_generates_signal_on_consensus(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        # Set up 5 whales, 4 agree on BUY_YES
        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        now = datetime.now(timezone.utc)
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "FED-RATE": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="FED-RATE",
                    direction=Direction.BUY_YES,
                    entry_price=0.40,  # Whales bought at $0.40, market is $0.34 — price edge
                    detected_at=now - timedelta(hours=18),
                ),
            })

        strategy = WhaleTrackerStrategy(monitor, settings, tmp_db)
        markets = [_make_market()]
        signals = strategy.scan_for_opportunities(markets)

        assert len(signals) == 1
        assert signals[0].strategy == StrategyName.WHALE_TRACKER
        assert signals[0].direction == Direction.BUY_YES

    def test_no_signal_empty_basket(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)
        strategy = WhaleTrackerStrategy(monitor, settings, tmp_db)

        signals = strategy.scan_for_opportunities([_make_market()])
        assert len(signals) == 0

    def test_stale_positions_skipped(self, tmp_db):
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        # Positions are 3 days old (stale)
        old_time = datetime.now(timezone.utc) - timedelta(hours=72)
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "FED-RATE": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="FED-RATE",
                    direction=Direction.BUY_YES,
                    entry_price=0.34,
                    detected_at=old_time,
                ),
            })

        strategy = WhaleTrackerStrategy(monitor, settings, tmp_db)
        signals = strategy.scan_for_opportunities([_make_market()])

        assert len(signals) == 0

    def test_timing_weight_early_entry(self, tmp_db):
        strategy = WhaleTrackerStrategy.__new__(WhaleTrackerStrategy)
        # Entry 36 hours ago — early, should get high weight
        entry_time = datetime.now(timezone.utc) - timedelta(hours=36)
        weight = strategy._timing_weight(entry_time)
        assert weight == 0.9

    def test_probability_estimate_uses_avg_entry(self, tmp_db):
        """Regression: probability_estimate should equal whale avg_entry, not current_price + edge."""
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        now = datetime.now(timezone.utc)
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "FED-RATE": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="FED-RATE",
                    direction=Direction.BUY_YES,
                    entry_price=0.50,  # Whales bought at $0.50
                    detected_at=now - timedelta(hours=18),
                ),
            })

        strategy = WhaleTrackerStrategy(monitor, settings, tmp_db)
        # Market at $0.34, whales entered at $0.50 → edge = $0.16
        markets = [_make_market(yes_price=0.34, no_price=0.66)]
        signals = strategy.scan_for_opportunities(markets)

        assert len(signals) == 1
        # probability_estimate should be avg_entry (0.50), not current_price + edge (0.34 + 0.16 = 0.50)
        # In this case they happen to be equal, but the logic should use avg_entry directly
        assert signals[0].probability_estimate == pytest.approx(0.50, abs=0.01)

    def test_timing_weight_recent_entry(self, tmp_db):
        strategy = WhaleTrackerStrategy.__new__(WhaleTrackerStrategy)
        # Entry 2 hours ago — very recent, lower weight
        entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        weight = strategy._timing_weight(entry_time)
        assert weight == 0.3

    def test_confidence_uses_average_not_multiplication(self, tmp_db):
        """Regression: confidence = (timing + consensus) / 2, not timing * consensus.
        Multiplication produces tiny values (0.7 * 0.8 = 0.56 vs avg 0.75)."""
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        now = datetime.now(timezone.utc)
        # 4/5 whales agree (80% consensus), entry 18h ago (timing weight = 0.7)
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "FED-RATE": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="FED-RATE",
                    direction=Direction.BUY_YES,
                    entry_price=0.50,
                    detected_at=now - timedelta(hours=18),
                ),
            })

        strategy = WhaleTrackerStrategy(monitor, settings, tmp_db)
        markets = [_make_market(yes_price=0.34)]
        signals = strategy.scan_for_opportunities(markets)

        assert len(signals) == 1
        # timing_weight(18h) = 0.7, consensus = 0.8
        # Average: (0.7 + 0.8) / 2 = 0.75
        # Multiplication would give: 0.7 * 0.8 = 0.56
        assert signals[0].confidence == pytest.approx(0.75, abs=0.01)

    def test_avg_entry_uses_direction_specific_prices(self, tmp_db):
        """Regression: avg_entry_price must only include whales going in the
        consensus direction, not all whales (which would contaminate the price)."""
        settings = Settings()
        monitor = WhaleMonitor(settings, tmp_db)

        for i in range(5):
            monitor._basket.append(WhaleWallet(address=f"whale-{i}"))

        now = datetime.now(timezone.utc)
        # 4 whales BUY_YES at $0.48
        for i in range(4):
            monitor.update_positions(f"whale-{i}", {
                "FED-RATE": WhalePosition(
                    wallet=f"whale-{i}",
                    market_id="FED-RATE",
                    direction=Direction.BUY_YES,
                    entry_price=0.48,
                    detected_at=now - timedelta(hours=18),
                ),
            })
        # 1 whale BUY_NO at $0.52
        monitor.update_positions("whale-4", {
            "FED-RATE": WhalePosition(
                wallet="whale-4",
                market_id="FED-RATE",
                direction=Direction.BUY_NO,
                entry_price=0.52,
                detected_at=now - timedelta(hours=18),
            ),
        })

        signal = monitor.get_consensus("FED-RATE")
        assert signal is not None
        assert signal.direction == Direction.BUY_YES
        # avg_entry should be 0.48 (only YES whales), not 0.496 (all whales)
        assert signal.avg_entry_price == pytest.approx(0.48, abs=0.001)
