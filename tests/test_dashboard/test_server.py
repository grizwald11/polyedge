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
        "total_markets": 30,
    }
    db.get_portfolio_summary.return_value = {
        "bankroll": 500.0,
        "open_positions": 3,
        "total_exposure": 75.0,
    }
    db.get_daily_pnl.return_value = 12.50
    db.get_recent_signals.return_value = [
        {"market_id": "M1", "strategy": "ai_probability", "direction": "BUY_YES", "edge": 0.08, "confidence": 0.7}
    ]
    db.get_resolved_predictions.return_value = [
        {"market_id": "M1", "brier_score": 0.15},
        {"market_id": "M2", "brier_score": 0.10},
    ]
    db.load_circuit_breaker_state.return_value = {"halted": False}

    # For /api/positions
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []
    mock_conn.close = MagicMock()
    db._get_conn.return_value = mock_conn

    db.get_trades_for_date.return_value = []
    return db


@pytest.fixture
def client(mock_db):
    app = create_app(mock_db)
    assert app is not None
    return TestClient(app)


class TestDashboardAPI:
    def test_api_stats(self, client, mock_db):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 42

    def test_api_positions(self, client):
        resp = client.get("/api/positions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_api_trades(self, client):
        resp = client.get("/api/trades")
        assert resp.status_code == 200

    def test_api_signals(self, client):
        resp = client.get("/api/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["market_id"] == "M1"

    def test_api_calibration(self, client):
        resp = client.get("/api/calibration")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_resolved"] == 2
        assert data["brier_score"] == pytest.approx(0.125)

    def test_api_portfolio(self, client):
        resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 42
        assert data["bankroll"] == 500.0
        assert data["circuit_breaker"]["halted"] is False

    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "PolyEdge" in resp.text


class TestCreateApp:
    def test_returns_none_without_fastapi(self, mock_db):
        with patch("src.dashboard.server.FASTAPI_AVAILABLE", False):
            app = create_app(mock_db)
            assert app is None
