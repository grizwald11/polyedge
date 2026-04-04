"""JSON API routes for the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def register_api_routes(app, *, db, metrics, position_manager, calibration_tracker, circuit_breaker, bankroll) -> None:
    """Register all JSON API routes on the FastAPI app."""
    _register_portfolio_routes(app, db=db, position_manager=position_manager)
    _register_signal_routes(app, db=db, calibration_tracker=calibration_tracker)
    _register_calibration_routes(app, db=db, calibration_tracker=calibration_tracker)
    _register_risk_routes(app, db=db, metrics=metrics, position_manager=position_manager, circuit_breaker=circuit_breaker)


def _register_portfolio_routes(app, *, db, position_manager) -> None:
    """Register portfolio-related API routes."""

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
        return db.get_positions_with_pnl()

    @app.get("/api/trades")
    async def api_trades():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return db.get_trades_for_date(today)

    @app.get("/api/portfolio")
    async def api_portfolio():
        stats = db.get_stats()
        summary = db.get_portfolio_summary()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = db.get_daily_pnl(today)
        cb_state = db.load_circuit_breaker_state()

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


def _register_signal_routes(app, *, db, calibration_tracker) -> None:
    """Register signal and strategy API routes."""

    @app.get("/api/signals")
    async def api_signals():
        return db.get_recent_signals(limit=50)

    @app.get("/api/strategies")
    async def api_strategies():
        """Per-strategy P&L, win rate, and optional Brier score."""
        stats = db.get_strategy_stats()
        if calibration_tracker is not None:
            for s in stats:
                try:
                    from src.core.models import StrategyName
                    brier = calibration_tracker.calculate_brier_score(
                        strategy=StrategyName(s["strategy"])
                    )
                    s["brier_score"] = brier
                except Exception as e:
                    logger.debug(f"Brier score lookup failed for {s.get('strategy')}: {e}")
                    s["brier_score"] = None
        return stats


def _register_calibration_routes(app, *, db, calibration_tracker) -> None:
    """Register calibration API routes."""

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


def _register_risk_routes(app, *, db, metrics, position_manager, circuit_breaker) -> None:
    """Register risk, health, and monitoring API routes."""

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

    @app.get("/api/health")
    async def api_health():
        if metrics is not None:
            return metrics.get_health_status()
        return {"status": "unknown", "message": "Metrics not initialized"}

    @app.get("/api/metrics")
    async def api_metrics():
        """Comprehensive operational metrics for monitoring."""
        result: dict = {}

        # Core health
        if metrics is not None:
            result["health"] = metrics.get_health_status()
            result["api_latencies"] = metrics.get_all_latency_stats()
            result["signal_funnel"] = {
                "generated": metrics.signals_generated,
                "risk_gated": metrics.signals_risk_gated,
                "executed": metrics.signals_executed,
                "conversion_rate": (
                    round(metrics.signals_executed / metrics.signals_generated, 3)
                    if metrics.signals_generated > 0 else 0
                ),
                "gate_rate": (
                    round(metrics.signals_risk_gated / metrics.signals_generated, 3)
                    if metrics.signals_generated > 0 else 0
                ),
            }

        # Per-strategy conversion rates from DB
        try:
            result["strategy_conversion"] = _compute_strategy_conversion(db)
        except Exception as e:
            logger.debug(f"Strategy conversion query failed: {e}")

        # Edge decay: predicted vs realized edge from edge_records
        try:
            result["edge_decay"] = _compute_edge_decay(db)
        except Exception as e:
            logger.debug(f"Edge decay query failed: {e}")

        # Risk state
        if circuit_breaker is not None:
            result["circuit_breaker"] = {
                "halted": circuit_breaker.is_halted(),
                "halt_reason": circuit_breaker.halt_reason,
                "reduced_sizing": circuit_breaker.is_reduced_sizing,
                "kelly_multiplier": circuit_breaker.get_kelly_multiplier(),
            }

        if position_manager is not None:
            result["exposure"] = {
                "total": round(position_manager.get_total_exposure(), 2),
                "pct": round(position_manager.get_total_exposure_pct(), 3),
                "positions": position_manager.get_position_count(),
                "unrealized_pnl": round(position_manager.get_total_unrealized_pnl(), 2),
            }

        return result


def _compute_strategy_conversion(db) -> list[dict]:
    """Compute signal-to-trade conversion rates per strategy from DB."""
    conn = db._get_conn()
    rows = conn.execute("""
        SELECT
            strategy,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) as executed,
            SUM(CASE WHEN status = 'risk_gated' THEN 1 ELSE 0 END) as risk_gated,
            ROUND(AVG(edge), 4) as avg_edge,
            ROUND(AVG(CASE WHEN status = 'executed' THEN edge END), 4) as avg_executed_edge,
            ROUND(AVG(CASE WHEN status = 'risk_gated' THEN edge END), 4) as avg_gated_edge
        FROM signals
        WHERE timestamp >= date('now', '-7 days')
        GROUP BY strategy
        ORDER BY total DESC
    """).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["conversion_rate"] = round(r["executed"] / r["total"], 3) if r["total"] > 0 else 0
        result.append(r)
    return result


def _compute_edge_decay(db) -> dict:
    """Compute predicted vs realized edge from edge_records table."""
    conn = db._get_conn()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) as count,
                ROUND(AVG(predicted_edge), 4) as avg_predicted,
                ROUND(AVG(realized_edge), 4) as avg_realized,
                ROUND(AVG(CASE WHEN realized_edge > 0 THEN 1.0 ELSE 0.0 END), 3) as win_rate
            FROM edge_records
            WHERE resolved_at IS NOT NULL
        """).fetchone()
        if row and row["count"] > 0:
            r = dict(row)
            r["shrinkage"] = round(r["avg_realized"] / r["avg_predicted"], 3) if r["avg_predicted"] else None
            return r
    except Exception as e:
        logger.debug(f"Edge decay query failed: {e}")
    return {"count": 0, "avg_predicted": None, "avg_realized": None, "shrinkage": None, "win_rate": None}
