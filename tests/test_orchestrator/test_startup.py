"""Tests for orchestrator startup — logging, PID lock, bankroll sync."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.startup import (
    _acquire_pid_lock,
    _release_pid_lock,
    _sync_bankroll,
    setup_logging,
)


# ──────────────────────────────────────────────
# setup_logging tests
# ──────────────────────────────────────────────

class TestSetupLogging:
    def test_sets_log_level_info(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging("INFO", log_file)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_sets_log_level_debug(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging("DEBUG", log_file)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_creates_log_directory(self, tmp_path):
        log_dir = tmp_path / "logs" / "nested"
        log_file = str(log_dir / "test.log")
        setup_logging("INFO", log_file)
        assert log_dir.exists()

    def test_creates_file_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging("INFO", log_file)
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "RotatingFileHandler" in handler_types
        assert "StreamHandler" in handler_types

    def test_clears_existing_handlers(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        old_count = len(root.handlers)

        setup_logging("INFO", log_file)

        # Should have exactly 2 handlers (console + file), not accumulated
        assert len(root.handlers) == 2

    def test_suppresses_noisy_loggers(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging("DEBUG", log_file)

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING


# ──────────────────────────────────────────────
# PID lock tests
# ──────────────────────────────────────────────

class TestPidLock:
    def test_acquire_creates_lock_file(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        result = _acquire_pid_lock(lock_path)
        assert result is True
        assert Path(lock_path).exists()
        assert int(Path(lock_path).read_text().strip()) == os.getpid()

    def test_acquire_fails_if_process_running(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        # Write current PID — process IS running
        Path(lock_path).write_text(str(os.getpid()))

        result = _acquire_pid_lock(lock_path)
        assert result is False

    def test_acquire_overwrites_dead_process_pid(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        # Write a PID that definitely doesn't exist
        Path(lock_path).write_text("999999999")

        result = _acquire_pid_lock(lock_path)
        assert result is True
        assert int(Path(lock_path).read_text().strip()) == os.getpid()

    def test_acquire_overwrites_corrupt_pid_file(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        Path(lock_path).write_text("not_a_pid")

        result = _acquire_pid_lock(lock_path)
        assert result is True

    def test_release_removes_own_lock(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        Path(lock_path).write_text(str(os.getpid()))

        _release_pid_lock(lock_path)
        assert not Path(lock_path).exists()

    def test_release_does_not_remove_other_process_lock(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")
        Path(lock_path).write_text("12345")  # Some other PID

        _release_pid_lock(lock_path)
        # File should still exist — it belongs to another process
        assert Path(lock_path).exists()

    def test_release_handles_missing_file(self, tmp_path):
        lock_path = str(tmp_path / "nonexistent.pid")
        # Should not raise
        _release_pid_lock(lock_path)

    def test_acquire_creates_parent_directories(self, tmp_path):
        lock_path = str(tmp_path / "nested" / "dir" / "test.pid")
        result = _acquire_pid_lock(lock_path)
        assert result is True
        assert Path(lock_path).exists()

    def test_acquire_then_release_then_reacquire(self, tmp_path):
        lock_path = str(tmp_path / "test.pid")

        assert _acquire_pid_lock(lock_path) is True
        _release_pid_lock(lock_path)
        assert not Path(lock_path).exists()
        assert _acquire_pid_lock(lock_path) is True
        assert Path(lock_path).exists()


# ──────────────────────────────────────────────
# _sync_bankroll tests
# ──────────────────────────────────────────────

class TestSyncBankroll:
    @pytest.mark.asyncio
    async def test_skips_in_paper_mode(self):
        settings = MagicMock()
        settings.trading.mode = "paper"
        kalshi = AsyncMock()
        risk_engine = MagicMock()
        position_manager = MagicMock()
        logger = logging.getLogger("test")

        result = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, 500.0, logger)

        # Returns bankroll unchanged
        assert result == 500.0
        kalshi.get_balance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_syncs_in_live_mode(self):
        settings = MagicMock()
        settings.trading.mode = "live"
        kalshi = AsyncMock()
        kalshi.get_balance = AsyncMock(return_value=750.0)
        risk_engine = MagicMock()
        position_manager = MagicMock()
        logger = logging.getLogger("test")

        result = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, 500.0, logger)

        assert result == 750.0
        risk_engine.update_bankroll.assert_called_once_with(750.0)
        assert position_manager.bankroll == 750.0

    @pytest.mark.asyncio
    async def test_handles_none_balance(self):
        settings = MagicMock()
        settings.trading.mode = "live"
        kalshi = AsyncMock()
        kalshi.get_balance = AsyncMock(return_value=None)
        risk_engine = MagicMock()
        position_manager = MagicMock()
        logger = logging.getLogger("test")

        result = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, 500.0, logger)

        # Falls back to config bankroll
        assert result == 500.0
        risk_engine.update_bankroll.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_zero_balance(self):
        settings = MagicMock()
        settings.trading.mode = "live"
        kalshi = AsyncMock()
        kalshi.get_balance = AsyncMock(return_value=0.0)
        risk_engine = MagicMock()
        position_manager = MagicMock()
        logger = logging.getLogger("test")

        result = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, 500.0, logger)

        # Zero balance is not > 0, so falls back
        assert result == 500.0

    @pytest.mark.asyncio
    async def test_handles_api_exception(self):
        settings = MagicMock()
        settings.trading.mode = "live"
        kalshi = AsyncMock()
        kalshi.get_balance = AsyncMock(side_effect=RuntimeError("API down"))
        risk_engine = MagicMock()
        position_manager = MagicMock()
        logger = logging.getLogger("test")

        result = await _sync_bankroll(settings, kalshi, risk_engine, position_manager, 500.0, logger)

        # Falls back gracefully
        assert result == 500.0
