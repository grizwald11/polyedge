"""Dashboard server — FastAPI web UI for portfolio visibility.

Provides both HTML pages and JSON API endpoints.
Accessible at localhost:8080.

Route handlers are split into submodules:
  - routes_html.py    — HTML page routes
  - routes_api.py     — JSON API routes
  - routes_partials.py — HTMX partial routes
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Optional

from src.metrics import Metrics
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Only import FastAPI if available
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response
    from fastapi.staticfiles import StaticFiles
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.info("FastAPI not installed — dashboard disabled")

try:
    from jinja2 import Environment, FileSystemLoader
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False


def create_app(
    db: Database,
    metrics: Metrics | None = None,
    position_manager=None,
    calibration_tracker=None,
    calibration_analyzer=None,
    circuit_breaker=None,
    bankroll: float = 500.0,
) -> Optional[object]:
    """Create the FastAPI dashboard application.

    Args:
        db: Database instance (required).
        metrics: Metrics instance for health endpoint.
        position_manager: Optional PositionManager for live position data.
        calibration_tracker: Optional CalibrationTracker for calibration bins.
        calibration_analyzer: Optional CalibrationAnalyzer for reports.
        circuit_breaker: Optional CircuitBreaker for risk status.

    Returns None if FastAPI is not installed.
    """
    if not FASTAPI_AVAILABLE:
        return None

    app = FastAPI(title="PolyEdge Dashboard", version="2.0")

    # L-2: Warn if running on HTTP (not HTTPS) — only safe when restricted to localhost
    logger.warning("Dashboard running on HTTP — use HTTPS in production or restrict to localhost")

    # CORS — configurable origins, defaulting to localhost only
    cors_origins_env = os.environ.get("POLYEDGE_CORS_ORIGINS", "")
    if cors_origins_env:
        cors_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    else:
        cors_origins = ["http://localhost:8080", "http://127.0.0.1:8080"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # ─── Authentication Middleware ─────────────────
    _dashboard_key = os.environ.get("POLYEDGE_DASHBOARD_KEY")
    if not _dashboard_key:
        logger.warning(
            "POLYEDGE_DASHBOARD_KEY is not set — dashboard is unauthenticated. "
            "Set this environment variable to enable API key protection."
        )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """M-12: Require API key OR localhost origin for all requests.

        When POLYEDGE_DASHBOARD_KEY is set, require it for all non-static paths.
        When not set, only allow requests from localhost (127.0.0.1/::1) —
        remote requests without a key are rejected.
        """
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        if _dashboard_key:
            provided_key: Optional[str] = None
            provided_key = request.query_params.get("key")
            if not provided_key:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    provided_key = auth_header[len("Bearer "):]
            if not provided_key or not hmac.compare_digest(provided_key.encode(), _dashboard_key.encode()):
                return Response(
                    content='{"detail": "Unauthorized"}',
                    status_code=401,
                    media_type="application/json",
                )
        else:
            # M-12: No key configured — restrict to localhost only
            client_host = getattr(request.client, "host", "") if request.client else ""
            _localhost_hosts = {"127.0.0.1", "::1", "localhost", "", "testclient"}
            if client_host not in _localhost_hosts:
                return Response(
                    content='{"detail": "Unauthorized — set POLYEDGE_DASHBOARD_KEY for remote access"}',
                    status_code=401,
                    media_type="application/json",
                )
        return await call_next(request)

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Templates
    templates_dir = Path(__file__).parent / "templates"
    jinja_env = None
    if JINJA2_AVAILABLE and templates_dir.exists():
        jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)

    def _render(template_name: str, **ctx) -> str:
        if jinja_env:
            template = jinja_env.get_template(template_name)
            return template.render(**ctx)
        return "<h1>PolyEdge</h1><p>Install jinja2 for full UI.</p>"

    # ─── Register route modules ───────────────────
    from src.dashboard.routes_api import register_api_routes
    from src.dashboard.routes_html import register_html_routes
    from src.dashboard.routes_partials import register_partial_routes

    register_html_routes(
        app, db=db, render=_render,
        position_manager=position_manager,
        calibration_analyzer=calibration_analyzer,
        circuit_breaker=circuit_breaker,
        bankroll=bankroll,
    )
    register_api_routes(
        app, db=db, metrics=metrics,
        position_manager=position_manager,
        calibration_tracker=calibration_tracker,
        circuit_breaker=circuit_breaker,
        bankroll=bankroll,
    )
    register_partial_routes(
        app, db=db,
        position_manager=position_manager,
    )

    return app


async def start_dashboard(
    db: Database,
    metrics: Metrics | None = None,
    position_manager=None,
    calibration_tracker=None,
    calibration_analyzer=None,
    circuit_breaker=None,
    bankroll: float = 500.0,
    host: str = "0.0.0.0",
    port: int = 8080,
):
    """Start the dashboard server as a background task."""
    app = create_app(
        db,
        metrics=metrics,
        position_manager=position_manager,
        calibration_tracker=calibration_tracker,
        calibration_analyzer=calibration_analyzer,
        circuit_breaker=circuit_breaker,
        bankroll=bankroll,
    )
    if app is None:
        logger.info("Dashboard not available (install fastapi + uvicorn)")
        return

    try:
        import uvicorn
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        logger.info(f"Dashboard starting at http://{host}:{port}")
        await server.serve()
    except ImportError:
        logger.info("uvicorn not installed — dashboard disabled")
    except SystemExit:
        logger.warning(f"Dashboard failed to bind port {port} (address in use)")
    except Exception as e:
        logger.error(f"Dashboard failed: {e}", exc_info=True)
