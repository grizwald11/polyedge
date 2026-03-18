"""Tests for daily report."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.alerts.alert_manager import AlertManager
from src.alerts.daily_report import DailyReport
from src.config import Settings
from src.core.models import Side, StrategyName, Trade


class MockAlertBackend:
    def __init__(self):
        self.messages = []

    async def send(self, title: str, body: str) -> bool:
        self.messages.append((title, body))
        return True


class TestDailyReport:
    def test_generate_empty_day(self, tmp_db):
        settings = Settings()
        manager = AlertManager()
        report = DailyReport(tmp_db, manager, settings)

        text = report.generate("2026-03-18")

        assert "2026-03-18" in text
        assert "Trades today: 0" in text
        assert "Daily P&L: $+0.00" in text

    def test_generate_with_trades(self, tmp_db):
        settings = Settings()
        manager = AlertManager()

        # Add some trades for today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trade = Trade(
            order_id="PE-test1",
            market_id="FED-RATE",
            token_id="FED-RATE_yes",
            side=Side.BUY,
            price=0.34,
            size=10,
            fee=0.02,
            realized_pnl=1.50,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
        )
        tmp_db.log_trade(trade)

        report = DailyReport(tmp_db, manager, settings)
        text = report.generate(today)

        assert "Trades today: 1" in text
        assert "ai_probability" in text

    @pytest.mark.asyncio
    async def test_generate_and_send(self, tmp_db):
        settings = Settings()
        manager = AlertManager()
        backend = MockAlertBackend()
        manager.register(backend)

        report = DailyReport(tmp_db, manager, settings)
        await report.generate_and_send("2026-03-18")

        assert len(backend.messages) == 1
        assert "Daily Report" in backend.messages[0][0]
        assert "2026-03-18" in backend.messages[0][1]

    def test_report_includes_mode(self, tmp_db):
        settings = Settings()
        manager = AlertManager()
        report = DailyReport(tmp_db, manager, settings)

        text = report.generate()
        assert "Mode: paper" in text
