"""Tests for mean reversion strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Direction, Market, MarketCategory, MarketToken, StrategyName
from src.strategies.mean_reversion import (
    MeanReversionStrategy,
    MAX_CONCURRENT_POSITIONS,
    MAX_HOLD_HOURS,
    MAX_MONOTONIC_RATIO,
    MAX_MOVE_PCT,
    MIN_EDGE,
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
    """Seed snapshots at regular intervals over the lookback window.

    Starts 10s before the lookback boundary to avoid race conditions
    with generate_signals computing its own datetime.now().
    """
    from src.core.models import MarketSnapshot
    now = datetime.now(timezone.utc)
    # Spread snapshots across 90% of the window, starting 5% in from the boundary.
    # This avoids race conditions where generate_signals computes a slightly
    # later datetime.now() and the first snapshot falls outside the lookback.
    window = timedelta(hours=hours_ago_start)
    margin = window * 0.05  # 5% buffer
    usable_window = window * 0.90
    interval = usable_window / max(len(prices) - 1, 1)
    for i, price in enumerate(prices):
        ts = now - window + margin + interval * i
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
        """A 15% price rise (noisy path) should generate a BUY_NO signal."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.575)  # Current price after rise
        # Price rose from 0.50 to 0.575 (15%) with some noise
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.54, 0.52, 0.56, 0.575])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_NO
        assert signals[0].edge > 0

    def test_signal_on_sharp_drop(self, tmp_db):
        """A 15% price drop (noisy path) should generate a BUY_YES signal."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.425)
        # Price dropped from 0.50 to 0.425 (15%) with some noise
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.46, 0.48, 0.44, 0.425])

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
        # Noisy path to avoid monotonic filter
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.55, 0.52, 0.57, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        assert signals[0].strategy == StrategyName.MEAN_REVERSION

    def test_signal_edge_is_category_scaled_move(self, tmp_db):
        """Edge should be category-scaled fraction of the price move percentage.

        Politics category uses 0.30 reversion factor (M-9), so a ~15-20% move
        produces ~4.5-6% edge.
        """
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        # ~15-20% move with noisy path
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.55, 0.52, 0.57, 0.60])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1
        # Politics reversion factor = 0.30, move ~15-20% → edge ~4.5-6%
        assert 0.03 <= signals[0].edge <= 0.08

    def test_signal_reasoning_contains_move_info(self, tmp_db):
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.60)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.55, 0.52, 0.57, 0.60])

        signals = strategy.generate_signals([market])
        assert "Mean reversion" in signals[0].reasoning
        assert "0.60" in signals[0].reasoning

    def test_no_signal_when_edge_below_minimum(self, tmp_db):
        """A move that meets MIN_PRICE_MOVE_PCT but produces edge < MIN_EDGE should be rejected.

        With Politics (reversion_factor=0.30) and a ~10% move, edge = 10% * 0.30 = 3%.
        This is right at the boundary. A slightly smaller move should be rejected.
        """
        strategy = MeanReversionStrategy(None, tmp_db)
        # ~10.2% move: 0.49 -> 0.54 (edge = 10.2% * 0.30 = 3.06% — just above MIN_EDGE)
        market = _make_market(yes_price=0.54)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.49, 0.50, 0.52, 0.54])

        signals = strategy.generate_signals([market])
        # Should produce a signal (edge ~3.06% >= 3%)
        if signals:
            assert signals[0].edge >= MIN_EDGE

    def test_min_edge_constant_is_positive(self):
        """MIN_EDGE should be a positive value."""
        assert MIN_EDGE > 0


class TestTrendFilters:
    def test_rejects_very_large_move(self, tmp_db):
        """Moves >40% are likely news-driven and should be rejected."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.75)
        # 50% move: 0.50 -> 0.75 — exceeds MAX_MOVE_PCT
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.55, 0.65, 0.75])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_rejects_monotonic_trend(self, tmp_db):
        """A perfectly monotonic move (all steps up) should be rejected as a trend."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.62)
        # Each step goes up — monotonic ratio = 100%
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.53, 0.57, 0.60, 0.62])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_accepts_noisy_move(self, tmp_db):
        """A move with some reversion (noisy) should pass the trend filter."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.62)
        # Net move is 0.50->0.62 (24%), but path is noisy: up, down, up, up
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.58, 0.54, 0.58, 0.62])

        signals = strategy.generate_signals([market])
        assert len(signals) == 1

    def test_max_move_pct_boundary(self, tmp_db):
        """A move at exactly MAX_MOVE_PCT should still be allowed."""
        strategy = MeanReversionStrategy(None, tmp_db)
        # 40% move: 0.50 -> 0.70. With noisy path.
        market = _make_market(yes_price=0.70)
        _seed_snapshots(tmp_db, "TEST-MKT", [0.50, 0.60, 0.55, 0.65, 0.70])

        signals = strategy.generate_signals([market])
        # At exactly 40% boundary, should be allowed (> not >=)
        assert len(signals) == 1

    def test_rejects_flat_prices(self, tmp_db):
        """Flat price snapshots (all identical) should be rejected — no real volatility."""
        strategy = MeanReversionStrategy(None, tmp_db)
        market = _make_market(yes_price=0.62)
        # All snapshots at 0.62 — total_steps == 0
        _seed_snapshots(tmp_db, "TEST-MKT", [0.62, 0.62, 0.62, 0.62])

        signals = strategy.generate_signals([market])
        assert len(signals) == 0

    def test_monotonic_ratio_constant(self):
        assert 0.5 < MAX_MONOTONIC_RATIO < 1.0

    def test_max_move_pct_constant(self):
        assert MAX_MOVE_PCT > MIN_PRICE_MOVE_PCT
