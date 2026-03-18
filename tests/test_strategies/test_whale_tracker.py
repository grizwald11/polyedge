"""Tests for whale tracker strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Direction, Market, MarketToken, StrategyName, WhaleSignal, WhaleWallet,
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

    def test_timing_weight_recent_entry(self, tmp_db):
        strategy = WhaleTrackerStrategy.__new__(WhaleTrackerStrategy)
        # Entry 2 hours ago — very recent, lower weight
        entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        weight = strategy._timing_weight(entry_time)
        assert weight == 0.3
