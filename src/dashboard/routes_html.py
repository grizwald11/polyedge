"""HTML page routes for the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def register_html_routes(app, *, db, render, position_manager, calibration_analyzer, circuit_breaker, bankroll) -> None:
    """Register all HTML page routes on the FastAPI app."""
    _register_portfolio_page(app, db=db, render=render, position_manager=position_manager, circuit_breaker=circuit_breaker, bankroll=bankroll)
    _register_analysis_pages(app, db=db, render=render, calibration_analyzer=calibration_analyzer)
    _register_risk_page(app, db=db, render=render, position_manager=position_manager, bankroll=bankroll)


def _register_portfolio_page(app, *, db, render, position_manager, circuit_breaker, bankroll) -> None:
    """Register the portfolio overview page route."""
    from fastapi.responses import HTMLResponse

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
                    "roi_pct": round((p.current_price - p.avg_entry_price) / p.avg_entry_price * 100, 2) if p.avg_entry_price > 0 else 0.0,
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
            exposure = round(sum(p["cost_basis"] for p in positions), 4)
            unrealized_pnl = round(sum(p["unrealized_pnl"] for p in positions), 4)
            for p in positions:
                p["current"] = p["current_price"]
                p["pnl"] = p["unrealized_pnl"]

        cb_halted = False
        cb_reason = None
        if circuit_breaker is not None:
            cb_halted = circuit_breaker.is_halted()
            cb_reason = circuit_breaker.halt_reason

        return render(
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


def _register_analysis_pages(app, *, db, render, calibration_analyzer) -> None:
    """Register strategy, calibration, and signals page routes."""
    from fastapi.responses import HTMLResponse

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page():
        """Strategy breakdown page."""
        strategy_stats = db.get_strategy_stats()
        return render(
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

        return render(
            "calibration.html",
            report=report,
            active_page="calibration",
        )

    @app.get("/signals", response_class=HTMLResponse)
    async def signals_page():
        """Signals browser page."""
        return render(
            "signals.html",
            active_page="signals",
        )


def _register_risk_page(app, *, db, render, position_manager, bankroll) -> None:
    """Register the risk management page route."""
    from fastapi.responses import HTMLResponse

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

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)
        daily_loss_pct = abs(min(0, daily_pnl)) / bankroll if bankroll > 0 else 0

        return render(
            "risk.html",
            cb_state=cb_state,
            exposure=exposure,
            exposure_pct=exposure_pct,
            position_count=position_count,
            cooldowns=cooldowns,
            daily_loss_pct=daily_loss_pct,
            active_page="risk",
        )
