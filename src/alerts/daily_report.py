"""Daily report — generates end-of-day P&L summary.

Sends via alert manager at the configured daily report time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.alerts.alert_manager import AlertManager
from src.config import Settings
from src.storage.database import Database

logger = logging.getLogger(__name__)


class DailyReport:
    """Generates and sends daily P&L summary."""

    def __init__(self, db: Database, alert_manager: AlertManager, settings: Settings):
        self.db = db
        self.alert_manager = alert_manager
        self.settings = settings

    async def generate_and_send(self, date_str: Optional[str] = None):
        """Generate and send the daily report.

        Args:
            date_str: Date in YYYY-MM-DD format. Defaults to today.
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        report = self.generate(date_str)
        await self.alert_manager.send_daily_summary(report)
        logger.info(f"Daily report sent for {date_str}")

    def generate(self, date_str: Optional[str] = None) -> str:
        """Generate the daily report text.

        Returns formatted report string.
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        trades = self.db.get_trades_for_date(date_str)
        stats = self.db.get_stats()
        daily_pnl = self.db.get_daily_pnl(date_str)
        summary = self.db.get_portfolio_summary()

        lines = [
            f"=== PolyEdge Daily Report — {date_str} ===",
            "",
        ]

        # Trade summary
        lines.append(f"Trades today: {len(trades)}")
        lines.append(f"Daily P&L: ${daily_pnl:+.2f}")
        lines.append("")

        # Strategy breakdown
        strategy_pnl = self.db.get_strategy_pnl(date_str)
        if strategy_pnl:
            lines.append("Strategy Breakdown:")
            for strategy, data in strategy_pnl.items():
                lines.append(
                    f"  {strategy}: {data['count']} trades, "
                    f"P&L=${data['pnl']:+.2f}"
                )
            lines.append("")

        # Win rate
        if trades:
            wins = sum(1 for t in trades if t.get("realized_pnl", 0) > 0)
            win_rate = wins / len(trades) if trades else 0
            lines.append(f"Win rate today: {win_rate:.0%} ({wins}/{len(trades)})")

        # Biggest win/loss
        if trades:
            pnls = [t.get("realized_pnl", 0) for t in trades]
            if pnls:
                lines.append(f"Biggest win: ${max(pnls):+.2f}")
                lines.append(f"Biggest loss: ${min(pnls):+.2f}")
        lines.append("")

        # Portfolio overview
        lines.append(f"Total P&L (all time): ${stats.get('total_pnl', 0):+.2f}")
        lines.append(f"Total trades (all time): {stats.get('total_trades', 0)}")
        lines.append(f"Active markets tracked: {stats.get('active_markets', 0)}")
        lines.append("")

        # Calibration
        resolved = self.db.get_resolved_predictions(days=7)
        if resolved:
            brier_scores = [
                r["brier_score"] for r in resolved
                if r.get("brier_score") is not None
            ]
            if brier_scores:
                avg_brier = sum(brier_scores) / len(brier_scores)
                lines.append(f"7-day Brier score: {avg_brier:.3f} ({len(brier_scores)} resolved)")

        # Circuit breaker status
        cb_state = self.db.load_circuit_breaker_state()
        if cb_state:
            if cb_state.get("halted"):
                lines.append(f"Circuit breaker: HALTED — {cb_state.get('halt_reason', 'Unknown')}")
            elif cb_state.get("reduced_sizing"):
                lines.append("Circuit breaker: Reduced sizing active")
            elif cb_state.get("consecutive_losing_days", 0) > 0:
                lines.append(
                    f"Consecutive losing days: {cb_state['consecutive_losing_days']}"
                )

        lines.append("")
        lines.append(f"Mode: {self.settings.trading.mode}")
        lines.append("=" * 40)

        return "\n".join(lines)
