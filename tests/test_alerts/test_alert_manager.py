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


class TestCooldownDedup:
    """Tests for alert cooldown/deduplication."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_suppressed_on_repeat(self):
        """Same circuit breaker alert within cooldown should be suppressed."""
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_circuit_breaker_alert("Daily loss limit")
        await manager.send_circuit_breaker_alert("Daily loss limit")

        assert len(backend.messages) == 1  # Second one suppressed

    @pytest.mark.asyncio
    async def test_circuit_breaker_different_reason_not_suppressed(self):
        """Different circuit breaker reasons should not be deduplicated."""
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_circuit_breaker_alert("Daily loss limit")
        await manager.send_circuit_breaker_alert("Consecutive losing days")

        assert len(backend.messages) == 2

    @pytest.mark.asyncio
    async def test_error_suppressed_on_repeat(self):
        """Same error within cooldown should be suppressed."""
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_error_alert("API timeout")
        await manager.send_error_alert("API timeout")

        assert len(backend.messages) == 1

    @pytest.mark.asyncio
    async def test_low_balance_suppressed_on_repeat(self):
        """Low balance alerts should be suppressed within cooldown."""
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        await manager.send_low_balance_alert(500.0, 1000.0)
        await manager.send_low_balance_alert(480.0, 1000.0)

        assert len(backend.messages) == 1

    @pytest.mark.asyncio
    async def test_trade_alerts_never_suppressed(self):
        """Trade alerts should always be sent (cooldown=0)."""
        manager = AlertManager()
        backend = MockBackend()
        manager.register(backend)

        for i in range(3):
            await manager.send_trade_alert(
                market_id="MKT-1", direction="BUY_YES",
                size=10, price=0.50, cost=5.00,
                strategy="ai_probability", edge=0.05,
            )

        assert len(backend.messages) == 3

    @pytest.mark.asyncio
    async def test_custom_cooldowns(self):
        """Custom cooldown values should override defaults."""
        manager = AlertManager(cooldowns={"error": 0})  # No cooldown for errors
        backend = MockBackend()
        manager.register(backend)

        await manager.send_error_alert("test error")
        await manager.send_error_alert("test error")

        assert len(backend.messages) == 2

    @pytest.mark.asyncio
    async def test_cooldown_expires(self):
        """After cooldown expires, the alert should be sent again."""
        import time
        manager = AlertManager(cooldowns={"error": 0.01})  # 10ms cooldown
        backend = MockBackend()
        manager.register(backend)

        await manager.send_error_alert("test error")
        time.sleep(0.02)  # Wait past cooldown
        await manager.send_error_alert("test error")

        assert len(backend.messages) == 2


class TestLogBackend:
    @pytest.mark.asyncio
    async def test_log_backend(self):
        backend = LogBackend()
        result = await backend.send("Title", "Body")
        assert result is True
