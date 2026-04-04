"""Tests for the dry-run replay script."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from scripts.dry_run_replay import (
    ReplayDiff,
    ReplayReport,
    build_market,
    build_signal,
    format_report,
    load_signals_for_date,
    replay_signals,
)


# ── Helpers ──────────────────────────────────────────


def _make_signal_row(
    *,
    signal_id=1,
    strategy="ai_probability",
    market_id="MKT-TEST-1",
    direction="BUY_YES",
    edge=0.08,
    probability_estimate=0.58,
    market_price=0.50,
    confidence=0.7,
    risk_passed=1,
    status="executed",
    timestamp="2026-04-02T14:00:00Z",
):
    return {
        "id": signal_id,
        "strategy": strategy,
        "market_id": market_id,
        "platform": "kalshi",
        "market_question": "Will something happen?",
        "direction": direction,
        "edge": edge,
        "probability_estimate": probability_estimate,
        "market_price": market_price,
        "confidence": confidence,
        "reasoning": "test reasoning",
        "timestamp": timestamp,
        "acted_on": 1,
        "order_id": "ORD-1",
        "risk_passed": risk_passed,
        "risk_failed_checks": "",
        "risk_warnings": "",
        "status": status,
    }


def _make_market_row(
    ticker="MKT-TEST-1",
    yes_price=0.50,
    no_price=0.50,
    volume_24h=20000,
    liquidity=10000,
):
    return {
        "ticker": ticker,
        "question": "Will something happen?",
        "category": "Politics",
        "end_date": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
        "yes_price": yes_price,
        "no_price": no_price,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "spread": 0.02,
        "platform": "kalshi",
    }


def _mock_db(signals=None, market_data=None):
    """Build a mock database that returns specified signals and market data."""
    db = MagicMock()
    conn = MagicMock()
    db._get_conn.return_value = conn

    def mock_execute(sql, params=None):
        result = MagicMock()
        sql_lower = sql.strip().lower()

        if "from signals" in sql_lower:
            rows = []
            for s in (signals or []):
                m = MagicMock()
                m.__iter__ = lambda self, d=s: iter(d)
                m.__len__ = lambda self, d=s: len(d)
                m.keys = lambda d=s: d.keys()
                m.__getitem__ = lambda self, key, d=s: d[key]
                rows.append(m)
            result.fetchall.return_value = rows
        elif "from markets" in sql_lower or "left join market_snapshots" in sql_lower:
            if market_data:
                m = MagicMock()
                m.__iter__ = lambda self, d=market_data: iter(d)
                m.__len__ = lambda self, d=market_data: len(d)
                m.keys = lambda d=market_data: d.keys()
                m.__getitem__ = lambda self, key, d=market_data: d[key]
                result.fetchone.return_value = m
            else:
                result.fetchone.return_value = None
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None

        return result

    conn.execute = mock_execute

    # Methods used by CircuitBreaker, PositionManager, RiskEngine
    db.load_circuit_breaker_state.return_value = {
        "halted": False,
        "consecutive_losing_days": 0,
        "reduced_sizing": False,
        "halt_reason": None,
        "halt_time": None,
        "last_recorded_day": None,
        "high_water_mark": None,
    }
    db.load_cooldowns.return_value = {}
    db.load_cooldown_durations.return_value = {}
    db.load_setting.return_value = None

    return db


# ── Unit tests ──────────────────────────────────────


class TestReplayDiff:

    def test_str_format(self):
        diff = ReplayDiff(
            signal_id=42,
            market_id="MKT-1",
            strategy="ai_probability",
            field="risk_passed",
            historical="PASS",
            replayed="FAIL(cooldown)",
        )
        s = str(diff)
        assert "42" in s
        assert "MKT-1" in s
        assert "PASS" in s
        assert "FAIL" in s


class TestReplayReport:

    def test_no_regressions(self):
        r = ReplayReport(date="2026-04-02", total_signals=5, replayed=5)
        assert not r.has_regressions

    def test_with_regressions(self):
        r = ReplayReport(
            date="2026-04-02",
            total_signals=5,
            replayed=5,
            diffs=[ReplayDiff(1, "M1", "ai", "risk_passed", "PASS", "FAIL")],
        )
        assert r.has_regressions


class TestBuildSignal:

    def test_valid_signal(self):
        row = _make_signal_row()
        sig = build_signal(row)
        assert sig is not None
        assert sig.market_id == "MKT-TEST-1"
        assert sig.edge == 0.08

    def test_invalid_strategy_returns_none(self):
        row = _make_signal_row(strategy="nonexistent_strategy")
        sig = build_signal(row)
        assert sig is None

    def test_invalid_direction_returns_none(self):
        row = _make_signal_row(direction="INVALID")
        sig = build_signal(row)
        assert sig is None


class TestBuildMarket:

    def test_valid_market(self):
        data = _make_market_row()
        signal = _make_signal_row()
        market = build_market(data, signal)
        assert market is not None
        assert market.ticker == "MKT-TEST-1"
        assert market.yes_price == 0.50

    def test_falls_back_to_signal_price(self):
        data = _make_market_row(yes_price=None, no_price=None)
        signal = _make_signal_row(market_price=0.60)
        market = build_market(data, signal)
        assert market is not None
        assert market.yes_price == 0.60

    def test_unknown_category_falls_back(self):
        data = _make_market_row()
        data["category"] = "UnknownCategory"
        signal = _make_signal_row()
        market = build_market(data, signal)
        assert market is not None


class TestLoadSignals:

    def test_loads_for_date(self):
        signals = [_make_signal_row()]
        db = _mock_db(signals=signals)
        result = load_signals_for_date(db, "2026-04-02")
        assert len(result) == 1

    def test_empty_date(self):
        db = _mock_db(signals=[])
        result = load_signals_for_date(db, "2026-04-02")
        assert result == []


class TestFormatReport:

    def test_clean_report(self):
        report = ReplayReport(
            date="2026-04-02", total_signals=10, replayed=8, skipped=2,
        )
        text = format_report(report)
        assert "DRY-RUN REPLAY" in text
        assert "No regressions" in text

    def test_regression_report(self):
        report = ReplayReport(
            date="2026-04-02",
            total_signals=5,
            replayed=5,
            diffs=[
                ReplayDiff(1, "M1", "ai_probability", "risk_passed", "PASS", "FAIL(cooldown)"),
            ],
        )
        text = format_report(report)
        assert "DIFFERENCES DETECTED" in text
        assert "Review the above" in text


class TestReplaySignals:

    def test_skips_when_no_market_data(self):
        signals = [_make_signal_row()]
        db = _mock_db(signals=signals, market_data=None)
        report = replay_signals(db, signals)
        assert report.skipped >= 1

    def test_replays_with_market_data(self):
        signals = [_make_signal_row()]
        market = _make_market_row()
        db = _mock_db(signals=signals, market_data=market)
        report = replay_signals(db, signals)
        assert report.replayed >= 1

    def test_signals_without_risk_status_skipped(self):
        """Signals with status='generated' and no risk_passed are skipped."""
        signals = [_make_signal_row(risk_passed=None, status="generated")]
        market = _make_market_row()
        db = _mock_db(signals=signals, market_data=market)
        report = replay_signals(db, signals)
        # Should have been replayed but then skipped in comparison (continue)
        assert len(report.diffs) == 0
