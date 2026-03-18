"""Tests for alert manager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.alerts.alert_manager import AlertManager, LogBackend


class MockBackend:
    def __init__(self):
        self.messages = []

    async def send(self, title: str, body: str) -> bool:
        self.messages.append((title, body))
        return True


class FailingBackend:
    async def send(self, title: str, body: str) -> bool:
        raise RuntimeError("Backend down")


class TestAlertManager:
    @pytest.mark.asyncio
    async def test_dispatch_to_backend(self):
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_trade_alert(
            market_id="FED-RATE",
            direction="BUY_YES",
            size=10,
            price=0.34,
            cost=3.40,
            strategy="ai_probability",
            edge=0.08,
        )

        assert len(backend.messages) == 1
        title, body = backend.messages[0]
        assert "FED-RATE" in title
        assert "ai_probability" in body

    @pytest.mark.asyncio
    async def test_circuit_breaker_alert(self):
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_circuit_breaker_alert("Daily loss limit")

        assert len(backend.messages) == 1
        assert "CIRCUIT BREAKER" in backend.messages[0][0]

    @pytest.mark.asyncio
    async def test_error_alert(self):
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_error_alert("API failure", context="Kalshi")

        assert len(backend.messages) == 1
        assert "Error" in backend.messages[0][0]
        assert "Kalshi" in backend.messages[0][1]

    @pytest.mark.asyncio
    async def test_daily_summary(self):
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_daily_summary("P&L: +$42.00")

        assert len(backend.messages) == 1
        assert "Daily Report" in backend.messages[0][0]

    @pytest.mark.asyncio
    async def test_multiple_backends(self):
        manager = AlertManager()
        b1 = MockBackend()
        b2 = MockBackend()
        manager.register(b1)
        manager.register(b2)

        await manager.send_error_alert("test")

        assert len(b1.messages) == 1
        assert len(b2.messages) == 1

    @pytest.mark.asyncio
    async def test_failing_backend_isolated(self):
        """A failing backend should not prevent other backends from receiving alerts."""
        manager = AlertManager()
        failing = FailingBackend()
        working = MockBackend()
        manager.register(failing)
        manager.register(working)

        await manager.send_error_alert("test")

        assert len(working.messages) == 1

    @pytest.mark.asyncio
    async def test_no_backends_no_error(self):
        manager = AlertManager()
        await manager.send_error_alert("test")  # Should not raise


class TestLogBackend:
    @pytest.mark.asyncio
    async def test_log_backend(self):
        backend = LogBackend()
        result = await backend.send("Title", "Body")
        assert result is True
