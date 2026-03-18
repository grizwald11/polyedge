"""Dashboard server — FastAPI web UI for portfolio visibility.

Provides both HTML pages and JSON API endpoints.
Accessible at localhost:8080.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.storage.database import Database

logger = logging.getLogger(__name__)

# Only import FastAPI if available
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
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


def create_app(db: Database) -> Optional[object]:
    """Create the FastAPI dashboard application.

    Returns None if FastAPI is not installed.
    """
    if not FASTAPI_AVAILABLE:
        return None

    app = FastAPI(title="PolyEdge Dashboard", version="1.0")

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Templates
    templates_dir = Path(__file__).parent / "templates"
    jinja_env = None
    if JINJA2_AVAILABLE and templates_dir.exists():
        jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))

    # ─── HTML Routes ──────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Portfolio overview page."""
        if jinja_env:
            template = jinja_env.get_template("index.html")
            stats = db.get_stats()
            summary = db.get_portfolio_summary()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily_pnl = db.get_daily_pnl(today)

            return template.render(
                stats=stats,
                summary=summary,
                daily_pnl=daily_pnl,
                today=today,
            )
        return HTMLResponse("<h1>PolyEdge Dashboard</h1><p>Install jinja2 for full UI.</p>")

    # ─── JSON API Routes ─────────────────────────

    @app.get("/api/stats")
    async def api_stats():
        """Get database stats."""
        return db.get_stats()

    @app.get("/api/positions")
    async def api_positions():
        """Get all open positions from trade history."""
        conn = db._get_conn()
        try:
            # Get latest trades per market to reconstruct positions
            rows = conn.execute(
                "SELECT market_id, side, price, size, strategy, timestamp "
                "FROM trades ORDER BY timestamp DESC LIMIT 100"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @app.get("/api/trades")
    async def api_trades():
        """Get recent trades."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return db.get_trades_for_date(today)

    @app.get("/api/signals")
    async def api_signals():
        """Get recent signals."""
        return db.get_recent_signals(limit=50)

    @app.get("/api/calibration")
    async def api_calibration():
        """Get calibration records."""
        resolved = db.get_resolved_predictions(days=30)
        if not resolved:
            return {"records": [], "brier_score": None}

        brier_scores = [
            r["brier_score"] for r in resolved
            if r.get("brier_score") is not None
        ]
        avg_brier = sum(brier_scores) / len(brier_scores) if brier_scores else None

        return {
            "records": resolved[:50],
            "brier_score": avg_brier,
            "total_resolved": len(resolved),
        }

    @app.get("/api/portfolio")
    async def api_portfolio():
        """Get portfolio summary."""
        stats = db.get_stats()
        summary = db.get_portfolio_summary()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)

        cb_state = db.load_circuit_breaker_state()

        return {
            **stats,
            **summary,
            "daily_pnl": daily_pnl,
            "circuit_breaker": cb_state,
        }

    return app


async def start_dashboard(db: Database, host: str = "0.0.0.0", port: int = 8080):
    """Start the dashboard server as a background task."""
    app = create_app(db)
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
    except Exception as e:
        logger.error(f"Dashboard failed: {e}")
