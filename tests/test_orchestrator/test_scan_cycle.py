"""Tests for scan_cycle module — M-1 key freshness check integration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.scan_cycle import _check_fills_and_cleanup


@pytest.fixture
def mock_fill_tracker():
    ft = AsyncMock()
    ft.check_fills = AsyncMock(return_value=[])
    return ft


@pytest.fixture
def mock_position_manager():
    return MagicMock()


@pytest.fixture
def mock_order_router():
    router = AsyncMock()
    router.cancel_stale_orders = AsyncMock(return_value=0)
    return router


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.execution.stale_order_age_seconds = 600
    return s


@pytest.fixture
def mock_logger():
    return MagicMock()


class TestKeyFreshnessInFillCleanup:
    """M-1: kalshi.check_key_freshness() is called during fill/cleanup cycle."""

    @pytest.mark.asyncio
    async def test_calls_check_key_freshness(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        kalshi = MagicMock()
        kalshi.check_key_freshness = MagicMock(return_value=True)

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=kalshi,
        )

        kalshi.check_key_freshness.assert_called_once()

    @pytest.mark.asyncio
    async def test_key_freshness_exception_does_not_halt(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """Key freshness failure should log debug and continue, not crash."""
        kalshi = MagicMock()
        kalshi.check_key_freshness = MagicMock(side_effect=RuntimeError("key rotated"))

        # Should not raise
        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=kalshi,
        )

        # Fill tracker should still run
        mock_fill_tracker.check_fills.assert_awaited_once()
        mock_logger.debug.assert_called()

    @pytest.mark.asyncio
    async def test_no_kalshi_skips_freshness_check(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """When kalshi is None, key freshness check is skipped gracefully."""
        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        # Fill tracker should still run
        mock_fill_tracker.check_fills.assert_awaited_once()
