"""Tests for mean reversion strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Direction, Market, MarketCategory, MarketToken, StrategyName
from src.strategies.mean_reversion import (
    MeanReversionStrategy,
    MAX_CONCURRENT_POSITIONS,
    MAX_HOLD_HOURS,
    MIN_PRICE_MOVE_PCT,
    MIN_SNAPSHOTS,
    MIN_VOLUME_24H,
)


def _make_market(
    ticker="TEST-MKT",
    yes_price=0.50,
    volume_24h=50000.0,
    days_ahead=30,
) -> Market:
    return Market(
        ticker=ticker,
        question=f"Test market {ticker}?",
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=1.0 - yes_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=days_ahead),
        volume_24h=volume_24h,
        active=True,
    )


def _seed_snapshots(db, ticker, prices, hours_ago_start=2):
    """Seed snapshots at regular intervals over the lookback window."""
    from src.core.models import MarketSnapshot
    now = datetime.now(timezone.utc)
    interval = timedelta(hours=hours_ago_start) / max(len(prices), 1)
    for i, price in enumerate(prices):
        ts = now - timedelta(hours=hours_ago_start) + interval * i
        db.log_snapshot(
            MarketSnapshot(
                market_id=ticker,
                timestamp=ts,
                yes_price=price,
                no_price=1.0 - price,
                spread=0.02,
                volume_1h=5000.0,
                liquidity=10000.0,
            )
        )


class TestMeanReversionSignals:
    def test_signal_on_sharp_rise(self, tmp_db):
        """A 15% price rise should generate a BUY_NO signal."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.575)  # Current price after rise
        # Price rose from 0.50 to 0.575 (15%)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.575])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_NO
        assert signals[0].edge > 0

    def test_signal_on_sharp_drop(self, tmp_db):
        """A 15% price drop should generate a BUY_YES signal."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.425)
        # Price dropped from 0.50 to 0.425 (15%)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.48, 0.45, 0.425])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_YES

    def test_no_signal_on_small_move(self, tmp_db):
        """A 5% move should not trigger."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.525)
        # Only 5% move
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.51, 0.52, 0.525])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_no_signal_low_volume(self, tmp_db):
        """Low volume markets should be skipped."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60, volume_24h=5000)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_no_signal_extreme_prices(self, tmp_db):
        """Penny and near-certain markets should be skipped."""
        strategy = MeanReversionStrategy(None, tmp_db)

        market_low = _make_market(yes_price=0.05)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.03, 0.04, 0.045, 0.05])

        market_high = _make_market(ticker="HIGH", yes_price=0.95)
        _seed_snapshots(tmp_db, "HIGH", [0.85, 0.90, 0.93, 0.95])

        signals = strategy.generate_signals([market_low, market_high])
        assert len(signals) == 0

    def test_insufficient_snapshots(self, tmp_db):
        """Too few snapshots should not produce signals."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.60])  # Only 2

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_concurrent_position_limit(self, tmp_db):
        """Should not exceed MAX_CONCURRENT_POSITIONS."""
        strategy = MeanReversionStrategy(None, tmp_db)
        # Fill up active entries
        for i in range(MAX_CONCURRENT_POSITIONS):
            strategy.record_entry(f"FILLED-{i}")

        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_skips_already_entered_market(self, tmp_db):
        """Should skip markets we're already in."""
        strategy = MeanReversionStrategy(None, tmp_db)
        strategy.record_entry("TEST-MKT")

        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0


class TestEntryExitTracking:
    def test_record_entry_and_exit(self, tmp_db):
        strategy = MeanReversionStrategy(None, tmp_db)
        assert strategy.active_count == 0

        strategy.record_entry("MKT-1")
        assert strategy.active_count == 1

        strategy.record_exit("MKT-1")
        assert strategy.active_count == 0

    def test_exit_nonexistent_market(self, tmp_db):
        """Exiting a market not tracked should not error."""
        strategy = MeanReversionStrategy(None, tmp_db)
        strategy.record_exit("DOESNT-EXIST")
        assert strategy.active_count == 0

    def test_auto_close_candidates(self, tmp_db):
        """Positions older than MAX_HOLD_HOURS should be returned."""
        strategy = MeanReversionStrategy(None, tmp_db)
        old_time = datetime.now(timezone.utc) - timedelta(hours=MAX_HOLD_HOURS + 1)
        strategy._active_entries["OLD-MKT"] = old_time
        strategy._active_entries["NEW-MKT"] = datetime.now(timezone.utc)

        candidates = strategy.get_exit_candidates()
        assert "OLD-MKT" in candidates
        assert "NEW-MKT" not in candidates

    def test_no_exit_candidates_when_fresh(self, tmp_db):
        strategy = MeanReversionStrategy(None, tmp_db)
        strategy._active_entries["FRESH-MKT"] = datetime.now(timezone.utc)

        assert strategy.get_exit_candidates() == []


class TestSignalDetails:
    def test_signal_has_correct_strategy(self, tmp_db):
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        assert signals[0].strategy == StrategyName.MEAN_REVERSION

    def test_signal_edge_is_half_move(self, tmp_db):
        """Edge should be approximately half the price move percentage."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        # ~15-20% move depending on snapshot timing
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        # Edge is half of measured move, move ~15-20% → edge ~7.5-10%
        assert 0.05 <= signals[0].edge <= 0.12

    def test_signal_reasoning_contains_move_info(self, tmp_db):
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.52, 0.55, 0.60])

        signals = strategy.generate_signals([market])
        assert "Mean reversion" in signals[0].reasoning
        assert "0.60" in signals[0].reasoning
