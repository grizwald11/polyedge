"""HTMX partial HTML routes for the dashboard."""

from __future__ import annotations

import html
import logging

logger = logging.getLogger(__name__)


def register_partial_routes(app, *, db, position_manager) -> None:
    """Register HTMX partial routes on the FastAPI app."""
    from fastapi.responses import HTMLResponse

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
