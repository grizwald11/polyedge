"""Tests for dashboard server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Skip entire module if FastAPI not installed
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from src.dashboard.server import create_app


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_stats.return_value = {
        "total_trades": 42,
        "total_signals": 100,
        "active_markets": 30,
    }
    db.get_portfolio_summary.return_value = {
        "total_trades": 42,
        "total_pnl": 125.50,
        "winning_trades": 28,
    }
    db.get_daily_pnl.return_value = 12.50
    db.get_recent_signals.return_value = [
        {"market_id": "M1", "strategy": "ai_probability", "direction": "BUY_YES",
         "edge": 0.08, "confidence": 0.7, "acted_on": 1}
    ]
    db.get_resolved_predictions.return_value = [
        {"market_id": "M1", "brier_score": 0.15},
        {"market_id": "M2", "brier_score": 0.10},
    ]
    db.load_circuit_breaker_state.return_value = {"halted": False, "consecutive_losing_days": 0, "reduced_sizing": False}
    db.get_strategy_stats.return_value = [
        {"strategy": "ai_probability", "trade_count": 30, "total_pnl": 100.0,
         "winning": 18, "losing": 12, "win_rate": 0.6},
    ]
    db.get_pnl_timeseries.return_value = [
        {"date": "2026-03-15", "pnl": 5.0, "cumulative_pnl": 5.0, "trade_count": 2},
    ]
    db.get_whale_activity.return_value = []
    db.load_cooldowns.return_value = {}

    # For /api/positions fallback
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []
    mock_conn.close = MagicMock()
    db._get_conn.return_value = mock_conn

    db.get_trades_for_date.return_value = []
    return db


@pytest.fixture
def mock_position_manager():
    pm = MagicMock()
    pm.get_all_positions.return_value = []
    pm.get_total_exposure.return_value = 50.0
    pm.get_total_exposure_pct.return_value = 0.10
    pm.get_total_unrealized_pnl.return_value = 3.50
    pm.get_position_count.return_value = 2
    return pm


@pytest.fixture
def mock_circuit_breaker():
    cb = MagicMock()
    cb.is_halted.return_value = False
    cb.halt_reason = None
    cb.is_reduced_sizing = False
    return cb


@pytest.fixture
def mock_calibration_tracker():
    ct = MagicMock()
    ct.get_calibration_bins.return_value = [
        {"bin_center": 0.5, "predicted_avg": 0.5, "actual_avg": 0.48, "count": 10}
    ]
    ct.get_accuracy_by_category.return_value = {"Politics": {"brier": 0.12, "count": 5}}
    ct.calculate_brier_score.return_value = 0.15
    return ct


@pytest.fixture
def mock_calibration_analyzer():
    ca = MagicMock()
    report = MagicMock()
    report.overall_brier = 0.15
    report.overall_win_rate = 0.60
    report.total_resolved = 20
    report.total_unresolved = 5
    report.calibration_curve = []
    report.category_stats = []
    report.best_category = "Politics"
    report.worst_category = "Culture"
    ca.generate_report.return_value = report
    return ca


@pytest.fixture
def client(mock_db):
    """Client with only DB (backward compat)."""
    app = create_app(mock_db)
    assert app is not None
    return TestClient(app)


@pytest.fixture
def full_client(mock_db, mock_position_manager, mock_calibration_tracker,
                mock_calibration_analyzer, mock_circuit_breaker):
    """Client with all optional components."""
    app = create_app(
        mock_db,
        position_manager=mock_position_manager,
        calibration_tracker=mock_calibration_tracker,
        calibration_analyzer=mock_calibration_analyzer,
        circuit_breaker=mock_circuit_breaker,
    )
    assert app is not None
    return TestClient(app)


class TestBackwardCompat:
    """create_app with only db still works."""

    def test_returns_app_with_db_only(self, mock_db):
        app = create_app(mock_db)
        assert app is not None

    def test_api_stats(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["total_trades"] == 42

    def test_api_positions_fallback(self, client):
        """Without position_manager, falls back to raw trade query."""
        resp = client.get("/api/positions")
        assert resp.status_code == 200

    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "PolyEdge" in resp.text


class TestDashboardAPI:
    def test_api_stats(self, full_client):
        resp = full_client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["total_trades"] == 42

    def test_api_positions_from_manager(self, full_client):
        resp = full_client.get("/api/positions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_api_trades(self, full_client):
        resp = full_client.get("/api/trades")
        assert resp.status_code == 200

    def test_api_signals(self, full_client):
        resp = full_client.get("/api/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    def test_api_strategies(self, full_client):
        resp = full_client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["strategy"] == "ai_probability"

    def test_api_calibration(self, full_client):
        resp = full_client.get("/api/calibration")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_resolved"] == 2
        assert data["brier_score"] == pytest.approx(0.125)

    def test_api_calibration_chart(self, full_client):
        resp = full_client.get("/api/calibration/chart")
        assert resp.status_code == 200
        data = resp.json()
        assert "bins" in data
        assert len(data["bins"]) == 1

    def test_api_calibration_categories(self, full_client):
        resp = full_client.get("/api/calibration/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert "Politics" in data

    def test_api_risk(self, full_client):
        resp = full_client.get("/api/risk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["halted"] is False
        assert data["exposure"] == 50.0
        assert data["position_count"] == 2

    def test_api_pnl_timeseries(self, full_client):
        resp = full_client.get("/api/pnl/timeseries")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["cumulative_pnl"] == 5.0

    def test_api_whale_activity(self, full_client):
        resp = full_client.get("/api/whale/activity")
        assert resp.status_code == 200

    def test_api_portfolio(self, full_client):
        resp = full_client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert "circuit_breaker" in data

    def test_api_health_no_metrics(self, full_client):
        resp = full_client.get("/api/health")
        assert resp.status_code == 200


class TestHTMLRoutes:
    def test_index(self, full_client):
        resp = full_client.get("/")
        assert resp.status_code == 200
        assert "Portfolio Overview" in resp.text

    def test_strategies_page(self, full_client):
        resp = full_client.get("/strategies")
        assert resp.status_code == 200
        assert "Strategy" in resp.text

    def test_calibration_page(self, full_client):
        resp = full_client.get("/calibration")
        assert resp.status_code == 200
        assert "Calibration" in resp.text

    def test_signals_page(self, full_client):
        resp = full_client.get("/signals")
        assert resp.status_code == 200
        assert "Signals" in resp.text

    def test_risk_page(self, full_client):
        resp = full_client.get("/risk")
        assert resp.status_code == 200
        assert "Risk" in resp.text


class TestHTMLPartials:
    def test_partial_signals(self, full_client):
        resp = full_client.get("/partials/signals")
        assert resp.status_code == 200
        assert "M1" in resp.text

    def test_partial_positions_empty(self, full_client):
        resp = full_client.get("/partials/positions")
        assert resp.status_code == 200
        assert "No open positions" in resp.text


class TestCreateApp:
    def test_returns_none_without_fastapi(self, mock_db):
        with patch("src.dashboard.server.FASTAPI_AVAILABLE", False):
            app = create_app(mock_db)
            assert app is None
