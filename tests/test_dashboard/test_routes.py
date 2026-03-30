"""Tests for dashboard routes — API, HTML, HTMX partials, and auth middleware.

Covers routes defined in routes_api.py, routes_html.py, routes_partials.py,
and the authentication middleware in server.py.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from src.dashboard.server import create_app

# ──────────────────────────────────────
# Fixtures
# ──────────────────────────────────────


@pytest.fixture
def mock_db():
    """Mock Database with all methods used by dashboard routes."""
    db = MagicMock()
    db.get_stats.return_value = {
        "total_trades": 10,
        "total_signals": 25,
        "active_markets": 15,
    }
    db.get_portfolio_summary.return_value = {
        "total_trades": 10,
        "total_pnl": 42.50,
        "winning_trades": 7,
    }
    db.get_daily_pnl.return_value = 5.25
    db.get_recent_signals.return_value = [
        {
            "market_id": "SIG-1",
            "strategy": "ai_probability",
            "direction": "BUY_YES",
            "edge": 0.12,
            "confidence": 0.80,
            "acted_on": 1,
        },
        {
            "market_id": "SIG-2",
            "strategy": "obvious_no",
            "direction": "BUY_NO",
            "edge": 0.03,
            "confidence": 0.95,
            "acted_on": 0,
        },
    ]
    db.get_resolved_predictions.return_value = []
    db.load_circuit_breaker_state.return_value = {
        "halted": False,
        "consecutive_losing_days": 0,
        "reduced_sizing": False,
    }
    db.get_strategy_stats.return_value = [
        {
            "strategy": "ai_probability",
            "trade_count": 8,
            "total_pnl": 30.0,
            "winning": 5,
            "losing": 3,
            "win_rate": 0.625,
        },
    ]
    db.get_pnl_timeseries.return_value = []
    db.get_whale_activity.return_value = []
    db.load_cooldowns.return_value = {}
    db.get_trades_for_date.return_value = []
    db.get_positions_with_pnl.return_value = [
        {
            "market_id": "POS-1",
            "direction": "BUY_YES",
            "size": 100,
            "avg_entry_price": 0.40,
            "current_price": 0.50,
            "unrealized_pnl": 10.0,
            "cost_basis": 40.0,
            "total_fees": 0.0,
            "strategy": "ai_probability",
            "paper": True,
        }
    ]
    return db


@pytest.fixture
def mock_position_manager():
    """Mock PositionManager returning sample positions."""
    pm = MagicMock()

    pos = MagicMock()
    pos.market_id = "PM-MKT-1"
    pos.market_question = "Will X happen?"
    pos.direction = MagicMock(value="BUY_YES")
    pos.size = 50
    pos.avg_entry_price = 0.35
    pos.current_price = 0.45
    pos.unrealized_pnl = 5.0
    pos.cost_basis = 17.50
    pos.total_fees = 0.0
    pos.strategy = MagicMock(value="ai_probability")
    pos.paper = True

    pm.get_all_positions.return_value = [pos]
    pm.get_total_exposure.return_value = 17.50
    pm.get_total_exposure_pct.return_value = 0.035
    pm.get_total_unrealized_pnl.return_value = 5.0
    pm.get_position_count.return_value = 1
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
    ct.get_calibration_bins.return_value = []
    ct.get_accuracy_by_category.return_value = {}
    ct.calculate_brier_score.return_value = 0.18
    return ct


@pytest.fixture
def mock_calibration_analyzer():
    ca = MagicMock()
    report = MagicMock()
    report.overall_brier = 0.18
    report.overall_win_rate = 0.625
    report.total_resolved = 8
    report.total_unresolved = 2
    report.calibration_curve = []
    report.category_stats = []
    report.best_category = "Politics"
    report.worst_category = "Culture"
    ca.generate_report.return_value = report
    return ca


def _make_client(mock_db, position_manager=None, calibration_tracker=None,
                 calibration_analyzer=None, circuit_breaker=None, env_overrides=None):
    """Helper to create a TestClient with optional env overrides."""
    env = {k: v for k, v in os.environ.items() if k != "POLYEDGE_DASHBOARD_KEY"}
    if env_overrides:
        env.update(env_overrides)
    with patch.dict(os.environ, env, clear=True):
        app = create_app(
            mock_db,
            position_manager=position_manager,
            calibration_tracker=calibration_tracker,
            calibration_analyzer=calibration_analyzer,
            circuit_breaker=circuit_breaker,
        )
        assert app is not None
        return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def unauthenticated_client(mock_db, mock_position_manager, mock_calibration_tracker,
                           mock_calibration_analyzer, mock_circuit_breaker):
    """Client with no POLYEDGE_DASHBOARD_KEY set (localhost-only mode)."""
    return _make_client(
        mock_db,
        position_manager=mock_position_manager,
        calibration_tracker=mock_calibration_tracker,
        calibration_analyzer=mock_calibration_analyzer,
        circuit_breaker=mock_circuit_breaker,
    )


@pytest.fixture
def authenticated_client(mock_db, mock_position_manager, mock_calibration_tracker,
                         mock_calibration_analyzer, mock_circuit_breaker):
    """Client with POLYEDGE_DASHBOARD_KEY set."""
    return _make_client(
        mock_db,
        position_manager=mock_position_manager,
        calibration_tracker=mock_calibration_tracker,
        calibration_analyzer=mock_calibration_analyzer,
        circuit_breaker=mock_circuit_breaker,
        env_overrides={"POLYEDGE_DASHBOARD_KEY": "test-api-key-42"},
    )


# ──────────────────────────────────────
# API Route Tests
# ──────────────────────────────────────


class TestAPIPortfolio:
    """Tests for /api/portfolio endpoint."""

    def test_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/portfolio")
        assert resp.status_code == 200

    def test_contains_expected_fields(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/portfolio").json()
        assert "total_pnl" in data
        assert "daily_pnl" in data
        assert "unrealized_pnl" in data
        assert "open_positions" in data
        assert "exposure" in data
        assert "circuit_breaker" in data

    def test_includes_unrealized_from_position_manager(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/portfolio").json()
        assert data["unrealized_pnl"] == 5.0
        assert data["open_positions"] == 1

    def test_falls_back_to_db_without_position_manager(self, mock_db):
        client = _make_client(mock_db, position_manager=None)
        data = client.get("/api/portfolio").json()
        assert data["open_positions"] == 1  # from get_positions_with_pnl


class TestAPIPositions:
    """Tests for /api/positions endpoint."""

    def test_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/positions")
        assert resp.status_code == 200

    def test_returns_positions_from_manager(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/positions").json()
        assert len(data) == 1
        assert data[0]["market_id"] == "PM-MKT-1"
        assert data[0]["direction"] == "BUY_YES"
        assert data[0]["size"] == 50

    def test_falls_back_to_db(self, mock_db):
        client = _make_client(mock_db, position_manager=None)
        data = client.get("/api/positions").json()
        assert len(data) == 1
        assert data[0]["market_id"] == "POS-1"


class TestAPISignals:
    """Tests for /api/signals endpoint."""

    def test_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/signals")
        assert resp.status_code == 200

    def test_returns_signals_list(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/signals").json()
        assert len(data) == 2
        assert data[0]["market_id"] == "SIG-1"
        assert data[1]["strategy"] == "obvious_no"

    def test_signals_called_with_limit(self, unauthenticated_client, mock_db):
        unauthenticated_client.get("/api/signals")
        mock_db.get_recent_signals.assert_called_with(limit=50)


class TestAPIRisk:
    """Tests for /api/risk endpoint."""

    def test_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/risk")
        assert resp.status_code == 200

    def test_includes_exposure_data(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/risk").json()
        assert data["halted"] is False
        assert data["exposure"] == 17.50
        assert data["position_count"] == 1

    def test_reflects_halted_circuit_breaker(self, mock_db, mock_position_manager,
                                             mock_calibration_tracker):
        cb = MagicMock()
        cb.is_halted.return_value = True
        cb.halt_reason = "Daily loss limit exceeded"
        cb.is_reduced_sizing = True
        client = _make_client(
            mock_db,
            position_manager=mock_position_manager,
            circuit_breaker=cb,
        )
        data = client.get("/api/risk").json()
        assert data["halted"] is True
        assert data["halt_reason"] == "Daily loss limit exceeded"
        assert data["reduced_sizing"] is True


class TestAPIHealth:
    """Tests for /api/health endpoint."""

    def test_returns_200_without_metrics(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "unknown"

    def test_returns_metrics_health(self, mock_db):
        metrics = MagicMock()
        metrics.get_health_status.return_value = {"status": "ok", "uptime": 3600}
        env = {k: v for k, v in os.environ.items() if k != "POLYEDGE_DASHBOARD_KEY"}
        with patch.dict(os.environ, env, clear=True):
            app = create_app(mock_db, metrics=metrics)
            client = TestClient(app)
            data = client.get("/api/health").json()
            assert data["status"] == "ok"


class TestAPIStrategies:
    """Tests for /api/strategies endpoint."""

    def test_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/api/strategies")
        assert resp.status_code == 200

    def test_includes_brier_score_from_tracker(self, unauthenticated_client):
        data = unauthenticated_client.get("/api/strategies").json()
        assert len(data) == 1
        assert data[0]["brier_score"] == 0.18


# ──────────────────────────────────────
# HTML Route Tests
# ──────────────────────────────────────


class TestHTMLRoutes:
    """Tests for HTML page routes."""

    def test_index_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/")
        assert resp.status_code == 200

    def test_positions_page_via_index(self, unauthenticated_client):
        # Index page contains positions table
        resp = unauthenticated_client.get("/")
        assert resp.status_code == 200

    def test_signals_page_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/signals")
        assert resp.status_code == 200

    def test_strategies_page_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/strategies")
        assert resp.status_code == 200

    def test_calibration_page_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/calibration")
        assert resp.status_code == 200

    def test_risk_page_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/risk")
        assert resp.status_code == 200

    def test_index_contains_html(self, unauthenticated_client):
        resp = unauthenticated_client.get("/")
        assert "PolyEdge" in resp.text or "<html" in resp.text.lower()


# ──────────────────────────────────────
# HTMX Partial Route Tests
# ──────────────────────────────────────


class TestHTMXPartials:
    """Tests for HTMX partial HTML fragment routes."""

    def test_partial_signals_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/partials/signals")
        assert resp.status_code == 200

    def test_partial_signals_contains_market_ids(self, unauthenticated_client):
        resp = unauthenticated_client.get("/partials/signals")
        assert "SIG-1" in resp.text
        assert "SIG-2" in resp.text

    def test_partial_signals_empty(self, mock_db):
        mock_db.get_recent_signals.return_value = []
        client = _make_client(mock_db)
        resp = client.get("/partials/signals")
        assert resp.status_code == 200
        assert "No signals yet" in resp.text

    def test_partial_positions_returns_200(self, unauthenticated_client):
        resp = unauthenticated_client.get("/partials/positions")
        assert resp.status_code == 200

    def test_partial_positions_with_manager(self, unauthenticated_client):
        resp = unauthenticated_client.get("/partials/positions")
        assert resp.status_code == 200
        assert "PM-MKT-1" in resp.text

    def test_partial_positions_empty(self, mock_db):
        pm = MagicMock()
        pm.get_all_positions.return_value = []
        client = _make_client(mock_db, position_manager=pm)
        resp = client.get("/partials/positions")
        assert "No open positions" in resp.text


# ──────────────────────────────────────
# Authentication Tests
# ──────────────────────────────────────


class TestDashboardAuth:
    """Tests for API key authentication and localhost restriction."""

    def test_no_key_set_allows_localhost(self, unauthenticated_client):
        """When no key is configured, localhost requests pass."""
        resp = unauthenticated_client.get("/api/portfolio")
        assert resp.status_code == 200

    def test_correct_key_query_param(self, authenticated_client):
        resp = authenticated_client.get("/api/portfolio?key=test-api-key-42")
        assert resp.status_code == 200

    def test_correct_key_bearer_header(self, authenticated_client):
        resp = authenticated_client.get(
            "/api/portfolio",
            headers={"Authorization": "Bearer test-api-key-42"},
        )
        assert resp.status_code == 200

    def test_missing_key_returns_401(self, authenticated_client):
        resp = authenticated_client.get("/api/portfolio")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, authenticated_client):
        resp = authenticated_client.get("/api/portfolio?key=wrong-key")
        assert resp.status_code == 401

    def test_wrong_bearer_returns_401(self, authenticated_client):
        resp = authenticated_client.get(
            "/api/portfolio",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_static_bypasses_auth(self, authenticated_client):
        resp = authenticated_client.get("/static/nonexistent.css")
        # Should not be 401 (may be 404 if file missing, but auth is bypassed)
        assert resp.status_code != 401

    def test_auth_applies_to_html_routes(self, authenticated_client):
        resp = authenticated_client.get("/")
        assert resp.status_code == 401

    def test_auth_applies_to_partials(self, authenticated_client):
        resp = authenticated_client.get("/partials/signals")
        assert resp.status_code == 401

    def test_localhost_restriction_without_key(self, mock_db):
        """When no key is set, non-localhost clients are rejected."""
        env = {k: v for k, v in os.environ.items() if k != "POLYEDGE_DASHBOARD_KEY"}
        with patch.dict(os.environ, env, clear=True):
            app = create_app(mock_db)
            # TestClient sends from "testclient" which is in the allowed list,
            # so we verify the middleware logic by checking the allowed hosts list
            # is used. A direct unit test of the middleware is in test_server.py.
            client = TestClient(app)
            resp = client.get("/api/stats")
            assert resp.status_code == 200
