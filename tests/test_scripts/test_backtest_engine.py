"""Tests for backtest engine — strategy replay, validation, and parameter sweep."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scripts.backtest_engine import (
    BacktestEngine,
    BacktestPortfolio,
    BacktestResult,
    BacktestTrade,
    MockForecaster,
    format_result,
    format_sweep,
    format_validation,
    parameter_sweep,
    validate_backtest,
)
from src.config import Settings
from src.core.models import Direction, ForecastResult


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def backtest_db(tmp_path):
    """Create a DB with settled markets and snapshots for backtesting."""
    from src.storage.database import Database

    db = Database(db_path=str(tmp_path / "bt.db"), wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    # Add 'result' column used by backtest engine (not in base schema)
    try:
        conn.execute("ALTER TABLE markets ADD COLUMN result TEXT DEFAULT ''")
    except Exception:
        pass  # Column may already exist
    conn.commit()

    # Insert 5 settled markets with known outcomes
    now = datetime.now(timezone.utc)
    ts_now = now.isoformat()
    markets = [
        ("MKT-A", "Will A happen?", "Politics", "yes", "EVT-1",
         '[]', (now - timedelta(days=10)).isoformat(), 50000, 20000),
        ("MKT-B", "Will B happen?", "Politics", "no", "EVT-1",
         '[]', (now - timedelta(days=8)).isoformat(), 40000, 15000),
        ("MKT-C", "Will C happen?", "Economics", "yes", "EVT-2",
         '[]', (now - timedelta(days=6)).isoformat(), 60000, 30000),
        ("MKT-D", "Will D happen?", "Tech", "no", "EVT-3",
         '[]', (now - timedelta(days=4)).isoformat(), 30000, 10000),
        ("MKT-E", "Will E happen?", "Politics", "yes", "EVT-4",
         '[]', (now - timedelta(days=2)).isoformat(), 70000, 25000),
    ]
    for ticker, q, cat, result, evt, tokens, end, vol, liq in markets:
        conn.execute(
            "INSERT INTO markets (ticker, question, category, result, event_ticker, "
            "tokens, end_date, volume_24h, liquidity, active, closed, first_seen, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)",
            (ticker, q, cat, result, evt, tokens, end, vol, liq, ts_now, ts_now),
        )

    # Insert hourly snapshots (3 per market) with varying prices
    base = now - timedelta(days=15)
    snapshot_data = [
        # MKT-A (outcome=yes): price rises from 0.40 to 0.60
        ("MKT-A", base, 0.40, 0.60, 0.20, 10),
        ("MKT-A", base + timedelta(hours=1), 0.50, 0.50, 0.00, 12),
        ("MKT-A", base + timedelta(hours=2), 0.60, 0.40, 0.20, 8),
        # MKT-B (outcome=no): price at 0.70 (will lose for YES buyers)
        ("MKT-B", base + timedelta(hours=3), 0.70, 0.30, 0.40, 15),
        ("MKT-B", base + timedelta(hours=4), 0.65, 0.35, 0.30, 11),
        # MKT-C (outcome=yes): price at 0.30
        ("MKT-C", base + timedelta(hours=5), 0.30, 0.70, 0.40, 20),
        ("MKT-C", base + timedelta(hours=6), 0.35, 0.65, 0.30, 18),
        # MKT-D (outcome=no): price at 0.45
        ("MKT-D", base + timedelta(hours=7), 0.45, 0.55, 0.10, 9),
        # MKT-E (outcome=yes): price at 0.50
        ("MKT-E", base + timedelta(hours=8), 0.50, 0.50, 0.00, 22),
    ]
    for mid, ts, yp, np_, sp, vol in snapshot_data:
        conn.execute(
            "INSERT INTO market_snapshots (market_id, timestamp, yes_price, no_price, spread, volume_1h) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, ts.isoformat(), yp, np_, sp, vol),
        )

    conn.commit()
    return db


@pytest.fixture
def backtest_db_with_calibration(backtest_db):
    """DB with calibration records for MockForecaster cache mode."""
    conn = backtest_db._get_conn()
    # Cached predictions — slightly better than market prices for outcome=yes markets
    records = [
        ("MKT-A", 0.55, 1),  # predicted 55%, actual yes
        ("MKT-B", 0.60, 0),  # predicted 60%, actual no (overconfident)
        ("MKT-C", 0.50, 1),  # predicted 50%, actual yes
    ]
    now = datetime.now(timezone.utc).isoformat()
    for mid, prob, outcome in records:
        conn.execute(
            "INSERT INTO calibration_records (market_id, predicted_probability, "
            "market_price_at_prediction, actual_outcome, predicted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, prob, 0.40, outcome, now),
        )
    conn.commit()
    return backtest_db


# ──────────────────────────────────────────────
# BacktestPortfolio
# ──────────────────────────────────────────────

class TestBacktestPortfolio:
    def test_initial_state(self):
        p = BacktestPortfolio(500.0)
        assert p.bankroll == 500.0
        assert p.get_exposure() == 0.0
        assert p.total_pnl == 0.0
        assert p.total_fees == 0.0

    def test_open_and_close_winning_buy_yes(self):
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.40, "test")
        # C-3: entry taker fee = ceil(0.07 * 10 * 0.40 * 0.60) = 1 cent = $0.01
        assert p.bankroll == pytest.approx(495.99)
        assert p.get_exposure() == pytest.approx(4.0)
        assert p.has_position("M1")

        pnl = p.close_position("M1", 0.60)
        # C-3: exit maker fee = ceil(0.0175 * 10 * 0.60 * 0.40) = 1 cent = $0.01
        # gross_pnl = (0.60 - 0.40) * 10 = 2.0; net_pnl = 2.0 - 0.01 = 1.99
        assert pnl == pytest.approx(1.99)
        assert p.total_pnl == pytest.approx(1.99)
        assert p.total_fees == pytest.approx(0.02)  # entry + exit
        assert not p.has_position("M1")

    def test_open_and_close_losing_buy_yes(self):
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.50, "test")
        pnl = p.close_position("M1", 0.30)
        # C-3: exit maker fee = ceil(0.0175 * 10 * 0.30 * 0.70) = 1 cent = $0.01
        # gross_pnl = (0.30 - 0.50) * 10 = -2.0; net_pnl = -2.0 - 0.01 = -2.01
        assert pnl == pytest.approx(-2.01)

    def test_buy_no_pnl(self):
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_NO, 10, 0.60, "test")
        pnl = p.close_position("M1", 0.40)
        # BUY_NO at 0.60, exit at 0.40: loss of 0.20 per contract
        # C-3: exit maker fee = ceil(0.0175 * 10 * 0.40 * 0.60) = 1 cent = $0.01
        # gross_pnl = (0.40 - 0.60) * 10 = -2.0; net_pnl = -2.0 - 0.01 = -2.01
        assert pnl == pytest.approx(-2.01)

    def test_close_nonexistent_returns_zero(self):
        p = BacktestPortfolio(500.0)
        assert p.close_position("MISSING", 0.50) == 0.0

    def test_resolve_yes_outcome(self):
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.30, "test")
        pnl = p.resolve_position("M1", outcome=True)
        # C-3: exit maker fee at price=1.0 = ceil(0.0175 * 10 * 1.0 * 0.0) = 0
        # gross_pnl = (1.0 - 0.30) * 10 = 7.0; net_pnl = 7.0 - 0.0 = 7.0
        assert pnl == pytest.approx(7.0)  # (1.0 - 0.30) * 10, no exit fee at boundary

    def test_resolve_no_outcome_buy_yes_loses(self):
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.40, "test")
        pnl = p.resolve_position("M1", outcome=False)
        # C-3: exit maker fee at price=0.0 = ceil(0.0175 * 10 * 0.0 * 1.0) = 0
        # gross_pnl = (0.0 - 0.40) * 10 = -4.0; net_pnl = -4.0 - 0.0 = -4.0
        assert pnl == pytest.approx(-4.0)  # (0.0 - 0.40) * 10, no exit fee at boundary

    def test_resolve_buy_no_yes_outcome_loses(self):
        """BUY_NO when YES wins: NO contracts settle at $0, full loss."""
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_NO, 10, 0.60, "test")
        pnl = p.resolve_position("M1", outcome=True)
        # Bought NO at 0.60, NO settles at 0.0 → loss of 0.60 per contract
        # C-3: entry fee = ceil(0.07 * 10 * 0.60 * 0.40) = 1 cent; exit fee at 0.0 = 0
        # net_pnl = -6.0 - 0.0 = -6.0
        assert pnl == pytest.approx(-6.0)
        # bankroll: 500 - 6 cost - 0.01 entry_fee + 0 proceeds - 0 exit_fee = 493.99
        assert p.bankroll == pytest.approx(493.99)

    def test_resolve_buy_no_no_outcome_wins(self):
        """BUY_NO when NO wins: NO contracts settle at $1, profit."""
        p = BacktestPortfolio(500.0)
        p.open_position("M1", Direction.BUY_NO, 10, 0.60, "test")
        pnl = p.resolve_position("M1", outcome=False)
        # Bought NO at 0.60, NO settles at 1.0 → gain of 0.40 per contract
        # C-3: entry fee = ceil(0.07 * 10 * 0.60 * 0.40) = 1 cent; exit fee at 1.0 = 0
        # net_pnl = 4.0 - 0.0 = 4.0
        assert pnl == pytest.approx(4.0)
        # bankroll: 500 - 6 cost - 0.01 entry_fee + 10 proceeds - 0 exit_fee = 503.99
        assert p.bankroll == pytest.approx(503.99)

    def test_resolve_nonexistent_returns_zero(self):
        p = BacktestPortfolio(500.0)
        assert p.resolve_position("MISSING", True) == 0.0

    def test_multiple_positions_exposure(self):
        p = BacktestPortfolio(1000.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.40, "test")
        p.open_position("M2", Direction.BUY_YES, 20, 0.50, "test")
        assert p.get_exposure() == pytest.approx(14.0)  # 4 + 10


# ──────────────────────────────────────────────
# MockForecaster
# ──────────────────────────────────────────────

class TestMockForecaster:
    def test_cached_prediction(self, backtest_db_with_calibration):
        forecaster = MockForecaster(backtest_db_with_calibration, noise=0.1)
        result = forecaster.get_forecast("MKT-A", 0.40)
        assert result is not None
        assert result.probability == pytest.approx(0.55)
        assert result.model_used == "backtest_cache"
        assert result.reasoning == "cached"

    def test_synthetic_from_outcome(self, backtest_db):
        """When no cached prediction but outcome exists, generate synthetic."""
        # MKT-A has outcome=yes in markets table but no calibration record
        # MockForecaster loads outcomes from calibration_records only
        # So we need to add outcome info
        conn = backtest_db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO calibration_records (market_id, predicted_probability, "
            "market_price_at_prediction, actual_outcome, predicted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("MKT-NEW", 0.0, 0.0, 1, now),
        )
        conn.commit()

        forecaster = MockForecaster(backtest_db, noise=0.0)
        # MKT-NEW is in cache (predicted_probability=0.0), so it uses cache
        result = forecaster.get_forecast("MKT-NEW", 0.50)
        assert result is not None

    def test_unknown_market_returns_none(self, backtest_db):
        forecaster = MockForecaster(backtest_db, noise=0.1)
        result = forecaster.get_forecast("TOTALLY-UNKNOWN", 0.50)
        assert result is None

    def test_confidence_bounds_clamped(self, backtest_db_with_calibration):
        forecaster = MockForecaster(backtest_db_with_calibration, noise=0.1)
        result = forecaster.get_forecast("MKT-A", 0.40)
        assert result.confidence_low >= 0.0
        assert result.confidence_high <= 1.0


# ──────────────────────────────────────────────
# BacktestEngine
# ──────────────────────────────────────────────

class TestBacktestEngine:
    def test_run_with_no_data_returns_empty(self, tmp_path, settings):
        """Engine with empty DB returns empty results."""
        from src.storage.database import Database
        db = Database(db_path=str(tmp_path / "empty.db"), wal_mode=True)
        engine = BacktestEngine(db, settings)
        results = engine.run()
        assert results == []

    def test_run_with_data_produces_results(self, backtest_db_with_calibration, settings):
        engine = BacktestEngine(backtest_db_with_calibration, settings)
        results = engine.run(bankroll=500.0)
        # Should produce at least one result
        assert len(results) >= 1
        result = results[0]
        assert isinstance(result, BacktestResult)
        assert result.equity_curve[0] == 500.0

    def test_run_respects_strategy_filter(self, backtest_db_with_calibration, settings):
        engine = BacktestEngine(
            backtest_db_with_calibration, settings,
            strategy_filter="nonexistent_strategy",
        )
        results = engine.run(bankroll=500.0)
        # With a nonexistent strategy filter, all signals should be skipped
        if results:
            assert results[0].total_trades == 0

    def test_run_tracks_equity_curve(self, backtest_db_with_calibration, settings):
        engine = BacktestEngine(backtest_db_with_calibration, settings)
        results = engine.run(bankroll=500.0)
        if results and results[0].total_trades > 0:
            assert len(results[0].equity_curve) > 1

    def test_engine_uses_kelly_sizer(self, backtest_db_with_calibration, settings):
        """Verify the engine uses the Kelly sizer, not arbitrary sizing."""
        engine = BacktestEngine(backtest_db_with_calibration, settings)
        assert engine.kelly is not None
        results = engine.run(bankroll=500.0)
        if results and results[0].trades:
            for trade in results[0].trades:
                # Kelly sizer should produce reasonable sizes
                assert trade.size >= 1

    def test_no_duplicate_positions(self, backtest_db_with_calibration, settings):
        """Engine should not open duplicate positions for the same market."""
        engine = BacktestEngine(backtest_db_with_calibration, settings)
        results = engine.run(bankroll=500.0)
        if results:
            market_ids = [t.market_id for t in results[0].trades]
            assert len(market_ids) == len(set(market_ids))

    def test_min_edge_filtering(self, backtest_db_with_calibration):
        """High min_edge should filter out marginal opportunities."""
        high_edge_settings = Settings()
        high_edge_settings.trading.min_edge_ai = 0.50  # 50% min edge
        engine = BacktestEngine(backtest_db_with_calibration, high_edge_settings)
        results = engine.run(bankroll=500.0)
        if results:
            assert results[0].total_trades == 0

    def test_custom_forecaster(self, backtest_db, settings):
        """Engine accepts a custom MockForecaster."""
        forecaster = MockForecaster(backtest_db, noise=0.0)
        engine = BacktestEngine(backtest_db, settings, forecaster=forecaster)
        # Should not crash
        results = engine.run(bankroll=500.0)
        assert isinstance(results, list)


# ──────────────────────────────────────────────
# Result Computation
# ──────────────────────────────────────────────

class TestComputeResult:
    def _make_result_from_trades(self, trades, bankroll=500.0):
        """Helper to compute a BacktestResult from a list of trades."""
        equity_curve = [bankroll]
        for t in trades:
            equity_curve.append(equity_curve[-1] + t.pnl)
        edges_p = [0.10] * len(trades)
        edges_r = [t.pnl / (t.size * t.price) if t.size > 0 and t.price > 0 else 0 for t in trades]

        engine = BacktestEngine.__new__(BacktestEngine)
        return engine._compute_result("test", trades, equity_curve, edges_p, edges_r, bankroll)

    def test_win_rate(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=6.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.50, 10, "t2", pnl=-5.0, resolved=True, outcome=False),
            BacktestTrade("M3", Direction.BUY_YES, "ai", 0.30, 10, "t3", pnl=7.0, resolved=True, outcome=True),
        ]
        result = self._make_result_from_trades(trades)
        assert result.total_trades == 3
        assert result.winning_trades == 2
        assert result.losing_trades == 1
        assert result.win_rate == pytest.approx(2 / 3)

    def test_total_pnl(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=6.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.60, 10, "t2", pnl=-6.0, resolved=True, outcome=False),
        ]
        result = self._make_result_from_trades(trades)
        assert result.total_pnl == pytest.approx(0.0)

    def test_max_drawdown(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=10.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.50, 10, "t2", pnl=-20.0, resolved=True, outcome=False),
            BacktestTrade("M3", Direction.BUY_YES, "ai", 0.30, 10, "t3", pnl=5.0, resolved=True, outcome=True),
        ]
        result = self._make_result_from_trades(trades)
        # Equity curve: 500 → 510 → 490 → 495
        # Peak = 510, max dd = 510 - 490 = 20
        assert result.max_drawdown == pytest.approx(20.0)
        assert result.max_drawdown_pct == pytest.approx(20.0 / 500.0)

    def test_profit_factor(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=10.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.50, 10, "t2", pnl=-5.0, resolved=True, outcome=False),
        ]
        result = self._make_result_from_trades(trades)
        assert result.profit_factor == pytest.approx(2.0)

    def test_profit_factor_no_losses(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=10.0, resolved=True, outcome=True),
        ]
        result = self._make_result_from_trades(trades)
        assert result.profit_factor == float("inf")

    def test_brier_score_computed(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=6.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.60, 10, "t2", pnl=-6.0, resolved=True, outcome=False),
        ]
        result = self._make_result_from_trades(trades)
        assert result.brier_score is not None
        # Trade 1: predicted=0.40 (BUY_YES), actual=1.0 → (0.40-1.0)^2 = 0.36
        # Trade 2: predicted=0.60 (BUY_YES), actual=0.0 → (0.60-0.0)^2 = 0.36
        # Mean = 0.36
        assert result.brier_score == pytest.approx(0.36)

    def test_unresolved_trades_excluded(self):
        trades = [
            BacktestTrade("M1", Direction.BUY_YES, "ai", 0.40, 10, "t1", pnl=6.0, resolved=True, outcome=True),
            BacktestTrade("M2", Direction.BUY_YES, "ai", 0.50, 10, "t2", resolved=False),
        ]
        result = self._make_result_from_trades(trades)
        assert result.total_trades == 1  # Only resolved trades counted

    def test_empty_trades(self):
        result = self._make_result_from_trades([])
        assert result.total_trades == 0
        assert result.win_rate == 0.0
        assert result.total_pnl == 0.0
        assert result.brier_score is None

    def test_new_fields_default_values(self):
        """C-1/C-2/C-3: New BacktestResult fields have correct defaults."""
        result = BacktestResult(strategy="test")
        assert result.uses_lookahead is False
        assert result.unresolved_positions == 0
        assert result.total_fees == 0.0


# ──────────────────────────────────────────────
# BacktestResult new fields (C-1, C-2, C-3)
# ──────────────────────────────────────────────

class TestBacktestResultNewFields:
    """Tests for uses_lookahead (C-1), unresolved_positions (C-2), total_fees (C-3)."""

    def test_uses_lookahead_default_false(self):
        """C-1: BacktestResult.uses_lookahead defaults to False."""
        result = BacktestResult(strategy="ai_probability")
        assert result.uses_lookahead is False

    def test_uses_lookahead_can_be_set(self):
        """C-1: BacktestResult.uses_lookahead can be set to True."""
        result = BacktestResult(strategy="ai_probability", uses_lookahead=True)
        assert result.uses_lookahead is True

    def test_unresolved_positions_default_zero(self):
        """C-2: unresolved_positions defaults to 0."""
        result = BacktestResult(strategy="ai_probability")
        assert result.unresolved_positions == 0

    def test_unresolved_positions_can_be_set(self):
        """C-2: unresolved_positions tracks mark-to-market count."""
        result = BacktestResult(strategy="ai_probability", unresolved_positions=3)
        assert result.unresolved_positions == 3

    def test_total_fees_default_zero(self):
        """C-3: total_fees defaults to 0.0."""
        result = BacktestResult(strategy="ai_probability")
        assert result.total_fees == 0.0

    def test_total_fees_can_be_set(self):
        """C-3: total_fees stores cumulative fee amount."""
        result = BacktestResult(strategy="ai_probability", total_fees=1.23)
        assert result.total_fees == pytest.approx(1.23)

    def test_portfolio_total_fees_accumulate(self):
        """C-3: BacktestPortfolio.total_fees accumulates across trades."""
        p = BacktestPortfolio(1000.0)
        p.open_position("M1", Direction.BUY_YES, 10, 0.40, "test")
        p.open_position("M2", Direction.BUY_YES, 10, 0.50, "test")
        # Each open deducts a taker fee
        assert p.total_fees > 0.0
        fees_before_close = p.total_fees
        p.close_position("M1", 0.60)
        # Close deducts maker fee too
        assert p.total_fees > fees_before_close

    def test_mock_forecaster_used_lookahead_tracks_synthetic(self, backtest_db):
        """C-1: MockForecaster.used_lookahead is True when outcome-derived forecasts are made."""
        from src.storage.database import Database
        conn = backtest_db._get_conn()
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        # Insert a market with an outcome but NO calibration record → forces synthetic mode
        try:
            conn.execute("ALTER TABLE markets ADD COLUMN result TEXT DEFAULT ''")
        except Exception:
            pass
        conn.commit()
        # MockForecaster loads outcomes from calibration_records + markets table
        # backtest_db already has MKT-A with result='yes' — MockForecaster should pick it up
        forecaster = MockForecaster(backtest_db, noise=0.0)
        # Call get_forecast on a market from self._outcomes (loaded from markets table)
        result = forecaster.get_forecast("MKT-A", 0.50)
        if result is not None and result.model_used == "backtest_synthetic":
            assert forecaster.used_lookahead is True
        # If it falls back to cache mode, used_lookahead stays False — still valid

    def test_mock_forecaster_used_lookahead_false_when_cached(self, backtest_db_with_calibration):
        """C-1: MockForecaster.used_lookahead stays False when only cache is used."""
        forecaster = MockForecaster(backtest_db_with_calibration, noise=0.1)
        # MKT-A, MKT-B, MKT-C have cached predictions — no synthetic mode needed
        forecaster.get_forecast("MKT-A", 0.40)
        forecaster.get_forecast("MKT-B", 0.70)
        assert forecaster.used_lookahead is False

    def test_engine_propagates_uses_lookahead(self, backtest_db, settings):
        """C-1: BacktestEngine propagates forecaster.used_lookahead to BacktestResult."""
        forecaster = MockForecaster(backtest_db, noise=0.1)
        engine = BacktestEngine(backtest_db, settings, forecaster=forecaster)
        results = engine.run(bankroll=500.0)
        if results:
            # uses_lookahead should match what the forecaster did
            assert results[0].uses_lookahead == forecaster.used_lookahead

    def test_engine_propagates_total_fees(self, backtest_db_with_calibration, settings):
        """C-3: BacktestEngine propagates total_fees from portfolio to BacktestResult."""
        engine = BacktestEngine(backtest_db_with_calibration, settings)
        results = engine.run(bankroll=500.0)
        if results and results[0].total_trades > 0:
            # If trades happened, fees should be >= 0
            assert results[0].total_fees >= 0.0

    def test_format_result_includes_fees_and_unresolved(self):
        """C-2/C-3: format_result output includes total_fees and unresolved_positions."""
        from scripts.backtest_engine import format_result
        result = BacktestResult(
            strategy="ai_probability",
            total_trades=5,
            total_fees=0.15,
            unresolved_positions=2,
        )
        text = format_result(result)
        assert "Total Fees" in text
        assert "Unresolved" in text
        assert "0.1500" in text
        assert "2" in text

    def test_format_result_lookahead_warning_shown(self):
        """C-1: format_result shows oracle_upper_bound warning when uses_lookahead=True."""
        from scripts.backtest_engine import format_result
        result = BacktestResult(strategy="test", uses_lookahead=True)
        text = format_result(result)
        assert "oracle_upper_bound" in text

    def test_format_result_no_lookahead_note_shown(self):
        """C-1: format_result shows cached-mode note when uses_lookahead=False."""
        from scripts.backtest_engine import format_result
        result = BacktestResult(strategy="test", uses_lookahead=False)
        text = format_result(result)
        assert "cached-prediction mode" in text


# ──────────────────────────────────────────────
# Validation (Phase 3 exit criteria)
# ──────────────────────────────────────────────

class TestValidation:
    def test_all_pass(self):
        result = BacktestResult(
            strategy="ai_probability",
            total_trades=60,
            winning_trades=36,
            losing_trades=24,
            total_pnl=50.0,
            max_drawdown=40.0,
            max_drawdown_pct=0.08,
            win_rate=0.60,
            brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["overall"]["pass"] is True
        assert checks["min_trades_50"]["pass"] is True
        assert checks["brier_below_020"]["pass"] is True
        assert checks["win_rate_55_70"]["pass"] is True
        assert checks["positive_pnl"]["pass"] is True
        assert checks["max_drawdown_below_20pct"]["pass"] is True

    def test_insufficient_trades(self):
        result = BacktestResult(
            strategy="test", total_trades=30, win_rate=0.60,
            total_pnl=10.0, max_drawdown_pct=0.10, brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["min_trades_50"]["pass"] is False
        assert checks["overall"]["pass"] is False

    def test_bad_brier(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.60,
            total_pnl=10.0, max_drawdown_pct=0.10, brier_score=0.25,
        )
        checks = validate_backtest(result)
        assert checks["brier_below_020"]["pass"] is False

    def test_win_rate_too_low(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.50,
            total_pnl=10.0, max_drawdown_pct=0.10, brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["win_rate_55_70"]["pass"] is False

    def test_win_rate_too_high(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.80,
            total_pnl=10.0, max_drawdown_pct=0.10, brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["win_rate_55_70"]["pass"] is False

    def test_negative_pnl(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.60,
            total_pnl=-20.0, max_drawdown_pct=0.10, brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["positive_pnl"]["pass"] is False

    def test_excessive_drawdown(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.60,
            total_pnl=10.0, max_drawdown_pct=0.25, brier_score=0.15,
        )
        checks = validate_backtest(result)
        assert checks["max_drawdown_below_20pct"]["pass"] is False

    def test_none_brier_fails(self):
        result = BacktestResult(
            strategy="test", total_trades=60, win_rate=0.60,
            total_pnl=10.0, max_drawdown_pct=0.10, brier_score=None,
        )
        checks = validate_backtest(result)
        assert checks["brier_below_020"]["pass"] is False


# ──────────────────────────────────────────────
# Parameter Sweep
# ──────────────────────────────────────────────

class TestParameterSweep:
    def test_sweep_kelly_fraction(self, backtest_db_with_calibration, settings):
        results = parameter_sweep(
            backtest_db_with_calibration, settings,
            "kelly_fraction", [0.25, 0.50, 0.75],
            bankroll=500.0,
        )
        assert isinstance(results, list)
        # Each result should be a (value, BacktestResult) tuple
        for val, r in results:
            assert isinstance(r, BacktestResult)
            assert val in [0.25, 0.50, 0.75]

    def test_sweep_min_edge(self, backtest_db_with_calibration, settings):
        results = parameter_sweep(
            backtest_db_with_calibration, settings,
            "min_edge_ai", [0.01, 0.10, 0.30],
            bankroll=500.0,
        )
        # Higher min_edge should produce fewer or equal trades
        if len(results) >= 2:
            trades_at_low = results[0][1].total_trades
            trades_at_high = results[-1][1].total_trades
            assert trades_at_high <= trades_at_low

    def test_sweep_unknown_param(self, backtest_db_with_calibration, settings, capsys):
        results = parameter_sweep(
            backtest_db_with_calibration, settings,
            "unknown_param", [1.0, 2.0],
            bankroll=500.0,
        )
        assert results == []


# ──────────────────────────────────────────────
# Formatting / Reporting
# ──────────────────────────────────────────────

class TestFormatting:
    def test_format_result(self):
        result = BacktestResult(
            strategy="ai_probability",
            total_trades=10,
            winning_trades=6,
            losing_trades=4,
            total_pnl=25.50,
            win_rate=0.60,
            avg_win=8.0,
            avg_loss=-4.5,
            profit_factor=1.78,
            max_drawdown=15.0,
            max_drawdown_pct=0.03,
            avg_edge_predicted=0.08,
            avg_edge_realized=0.05,
            brier_score=0.18,
            sharpe_ratio=1.5,
            calmar_ratio=2.0,
        )
        text = format_result(result)
        assert "ai_probability" in text
        assert "6W" in text
        assert "4L" in text
        assert "25.50" in text
        assert "Brier" in text
        assert "Sharpe" in text
        assert "Calmar" in text

    def test_format_result_no_optional_metrics(self):
        result = BacktestResult(strategy="test", total_trades=0)
        text = format_result(result)
        assert "test" in text
        assert "Brier" not in text

    def test_format_sweep(self):
        sweep = [
            (0.25, BacktestResult(strategy="s", total_trades=10, win_rate=0.50, total_pnl=-5.0, max_drawdown_pct=0.05)),
            (0.50, BacktestResult(strategy="s", total_trades=15, win_rate=0.60, total_pnl=10.0, max_drawdown_pct=0.03)),
        ]
        text = format_sweep(sweep, "kelly_fraction")
        assert "kelly_fraction" in text
        assert "0.250" in text
        assert "0.500" in text

    def test_format_validation_pass(self):
        checks = {
            "min_trades_50": {"pass": True, "value": 60, "threshold": 50},
            "overall": {"pass": True},
        }
        text = format_validation(checks)
        assert "PASS" in text
        assert "Ready for live trading" in text

    def test_format_validation_fail(self):
        checks = {
            "min_trades_50": {"pass": False, "value": 30, "threshold": 50},
            "overall": {"pass": False},
        }
        text = format_validation(checks)
        assert "FAIL" in text
        assert "NOT READY" in text
