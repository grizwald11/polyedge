"""Dashboard server — FastAPI web UI for portfolio visibility.

Provides both HTML pages and JSON API endpoints.
Accessible at localhost:8080.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.storage.database import Database
from src.metrics import Metrics

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
        return f"<h1>PolyEdge</h1><p>Install jinja2 for full UI.</p>"

    # ─── HTML Routes ──────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Portfolio overview page."""
        stats = db.get_stats()
        summary = db.get_portfolio_summary()
        summary["bankroll"] = bankroll
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)

        positions = []
        exposure = 0.0
        unrealized_pnl = 0.0
        if position_manager is not None:
            positions = [
                {
                    "market_id": p.market_id,
                    "direction": p.direction.value,
                    "size": p.size,
                    "avg_entry": p.avg_entry_price,
                    "current": p.current_price,
                    "pnl": p.unrealized_pnl,
                    "roi_pct": ((p.current_price - p.avg_entry_price) / p.avg_entry_price * 100) if p.avg_entry_price > 0 else 0.0,
                    "cost_basis": p.cost_basis,
                    "strategy": p.strategy.value,
                }
                for p in position_manager.get_all_positions()
            ]
            exposure = position_manager.get_total_exposure()
            unrealized_pnl = position_manager.get_total_unrealized_pnl()
        else:
            # Fallback: compute from DB when position_manager is unavailable
            positions = db.get_positions_with_pnl()
            exposure = sum(p["cost_basis"] for p in positions)
            unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)
            # Normalize keys for template compatibility
            for p in positions:
                p["current"] = p["current_price"]
                p["pnl"] = p["unrealized_pnl"]

        cb_halted = False
        cb_reason = None
        if circuit_breaker is not None:
            cb_halted = circuit_breaker.is_halted()
            cb_reason = circuit_breaker.halt_reason

        return _render(
            "index.html",
            stats=stats,
            summary=summary,
            daily_pnl=daily_pnl,
            today=today,
            positions=positions,
            exposure=exposure,
            unrealized_pnl=unrealized_pnl,
            cb_halted=cb_halted,
            cb_reason=cb_reason,
            active_page="portfolio",
        )

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page():
        """Strategy breakdown page."""
        strategy_stats = db.get_strategy_stats()
        return _render(
            "strategies.html",
            strategy_stats=strategy_stats,
            active_page="strategies",
        )

    @app.get("/calibration", response_class=HTMLResponse)
    async def calibration_page():
        """Calibration visualization page."""
        report = None
        if calibration_analyzer is not None:
            try:
                report = calibration_analyzer.generate_report()
            except Exception as e:
                logger.warning(f"Failed to generate calibration report: {e}")

        return _render(
            "calibration.html",
            report=report,
            active_page="calibration",
        )

    @app.get("/signals", response_class=HTMLResponse)
    async def signals_page():
        """Signals browser page."""
        return _render(
            "signals.html",
            active_page="signals",
        )

    @app.get("/risk", response_class=HTMLResponse)
    async def risk_page():
        """Risk management page."""
        cb_state = db.load_circuit_breaker_state()
        exposure = 0.0
        exposure_pct = 0.0
        position_count = 0
        if position_manager is not None:
            exposure = position_manager.get_total_exposure()
            exposure_pct = position_manager.get_total_exposure_pct()
            position_count = position_manager.get_position_count()

        cooldowns = db.load_cooldowns()

        # Calculate daily loss as percentage of bankroll
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)
        summary = db.get_portfolio_summary()
        summary["bankroll"] = bankroll
        daily_loss_pct = abs(min(0, daily_pnl)) / bankroll if bankroll > 0 else 0

        return _render(
            "risk.html",
            cb_state=cb_state,
            exposure=exposure,
            exposure_pct=exposure_pct,
            position_count=position_count,
            cooldowns=cooldowns,
            daily_loss_pct=daily_loss_pct,
            active_page="risk",
        )

    # ─── JSON API Routes ─────────────────────────

    @app.get("/api/stats")
    async def api_stats():
        return db.get_stats()

    @app.get("/api/positions")
    async def api_positions():
        """Get live positions from PositionManager (not raw trades)."""
        if position_manager is not None:
            return [
                {
                    "market_id": p.market_id,
                    "market_question": p.market_question,
                    "direction": p.direction.value,
                    "size": p.size,
                    "avg_entry_price": p.avg_entry_price,
                    "current_price": p.current_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "cost_basis": p.cost_basis,
                    "total_fees": p.total_fees,
                    "strategy": p.strategy.value,
                    "paper": p.paper,
                }
                for p in position_manager.get_all_positions()
            ]
        # Fallback: compute positions with P&L from DB
        return db.get_positions_with_pnl()

    @app.get("/api/trades")
    async def api_trades():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return db.get_trades_for_date(today)

    @app.get("/api/signals")
    async def api_signals():
        return db.get_recent_signals(limit=50)

    @app.get("/api/strategies")
    async def api_strategies():
        """Per-strategy P&L, win rate, and optional Brier score."""
        stats = db.get_strategy_stats()
        # Enrich with Brier scores if calibration_tracker available
        if calibration_tracker is not None:
            for s in stats:
                try:
                    from src.core.models import StrategyName
                    brier = calibration_tracker.calculate_brier_score(
                        strategy=StrategyName(s["strategy"])
                    )
                    s["brier_score"] = brier
                except Exception:
                    s["brier_score"] = None
        return stats

    @app.get("/api/calibration")
    async def api_calibration():
        resolved = db.get_resolved_predictions(days=30)
        if not resolved:
            return {"records": [], "brier_score": None, "total_resolved": 0}

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

    @app.get("/api/calibration/chart")
    async def api_calibration_chart():
        """Binned predicted-vs-actual for Chart.js."""
        if calibration_tracker is not None:
            bins = calibration_tracker.get_calibration_bins()
            return {"bins": bins}
        return {"bins": []}

    @app.get("/api/calibration/categories")
    async def api_calibration_categories():
        """Per-category Brier scores."""
        if calibration_tracker is not None:
            return calibration_tracker.get_accuracy_by_category()
        return {}

    @app.get("/api/risk")
    async def api_risk():
        """Exposure levels, halt status."""
        result = {
            "halted": False,
            "halt_reason": None,
            "exposure": 0.0,
            "exposure_pct": 0.0,
            "position_count": 0,
        }
        if circuit_breaker is not None:
            result["halted"] = circuit_breaker.is_halted()
            result["halt_reason"] = circuit_breaker.halt_reason
            result["reduced_sizing"] = circuit_breaker.is_reduced_sizing
        if position_manager is not None:
            result["exposure"] = position_manager.get_total_exposure()
            result["exposure_pct"] = position_manager.get_total_exposure_pct()
            result["position_count"] = position_manager.get_position_count()
        return result

    @app.get("/api/pnl/timeseries")
    async def api_pnl_timeseries():
        """Daily P&L for line chart."""
        return db.get_pnl_timeseries(days=30)

    @app.get("/api/whale/activity")
    async def api_whale_activity():
        """Recent whale trades."""
        return db.get_whale_activity(limit=50)

    @app.get("/api/portfolio")
    async def api_portfolio():
        stats = db.get_stats()
        summary = db.get_portfolio_summary()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)
        cb_state = db.load_circuit_breaker_state()

        # Unrealized P&L from position_manager or DB fallback
        if position_manager is not None:
            unrealized = position_manager.get_total_unrealized_pnl()
            open_positions = position_manager.get_position_count()
            exposure = position_manager.get_total_exposure()
        else:
            db_positions = db.get_positions_with_pnl()
            unrealized = sum(p["unrealized_pnl"] for p in db_positions)
            open_positions = len(db_positions)
            exposure = sum(p["cost_basis"] for p in db_positions)

        return {
            **stats,
            **summary,
            "daily_pnl": daily_pnl,
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl_incl_unrealized": round(summary.get("total_pnl", 0) + unrealized, 2),
            "open_positions": open_positions,
            "exposure": round(exposure, 2),
            "circuit_breaker": cb_state,
        }

    @app.get("/api/health")
    async def api_health():
        if metrics is not None:
            return metrics.get_health_status()
        return {"status": "unknown", "message": "Metrics not initialized"}

    # ─── HTML Partial Routes (for HTMX) ──────────

    @app.get("/partials/signals", response_class=HTMLResponse)
    async def partial_signals():
        """HTML fragment of recent signals for HTMX swap."""
        signals = db.get_recent_signals(limit=20)
        rows = []
        for s in signals:
            acted = "Yes" if s.get("acted_on") else "No"
            rows.append(
                f'<tr><td>{html.escape(str(s.get("market_id","")))}</td>'
                f'<td>{html.escape(str(s.get("strategy","")))}</td>'
                f'<td>{html.escape(str(s.get("direction","")))}</td>'
                f'<td>{s.get("edge",0):.1%}</td>'
                f'<td>{s.get("confidence",0):.0%}</td>'
                f'<td>{acted}</td></tr>'
            )
        return "\n".join(rows) if rows else '<tr><td colspan="6" class="muted">No signals yet</td></tr>'

    @app.get("/partials/positions", response_class=HTMLResponse)
    async def partial_positions():
        """HTML fragment of positions for HTMX swap."""
        pos_list = []
        if position_manager is not None:
            for p in position_manager.get_all_positions():
                entry = p.avg_entry_price
                roi = ((p.current_price - entry) / entry * 100) if entry > 0 else 0.0
                pos_list.append({
                    "market_id": p.market_id, "direction": p.direction.value,
                    "size": int(p.size), "avg_entry": entry,
                    "current": p.current_price, "pnl": p.unrealized_pnl,
                    "cost_basis": p.cost_basis, "roi_pct": roi,
                    "strategy": p.strategy.value,
                })
        else:
            pos_list = db.get_positions_with_pnl()
            for p in pos_list:
                p["current"] = p["current_price"]
                p["pnl"] = p["unrealized_pnl"]

        if not pos_list:
            return '<tr><td colspan="9" class="muted">No open positions</td></tr>'
        rows = []
        for p in pos_list:
            pnl_class = "positive" if p["pnl"] >= 0 else "negative"
            roi_class = "positive" if p.get("roi_pct", 0) >= 0 else "negative"
            cost = p.get("cost_basis", p["avg_entry"] * p["size"])
            rows.append(
                f'<tr><td>{html.escape(str(p["market_id"])[:30])}</td>'
                f'<td>{html.escape(str(p["direction"]))}</td>'
                f'<td>{int(p["size"])}</td>'
                f'<td>${p["avg_entry"]:.2f}</td>'
                f'<td>${p["current"]:.2f}</td>'
                f'<td>${cost:.2f}</td>'
                f'<td class="{pnl_class}">${p["pnl"]:.2f}</td>'
                f'<td class="{roi_class}">{p.get("roi_pct", 0):.1f}%</td>'
                f'<td>{html.escape(str(p.get("strategy", "")))}</td></tr>'
            )
        return "\n".join(rows)

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
        logger.error(f"Dashboard failed: {e}")
