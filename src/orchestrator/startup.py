"""Startup utilities — logging, bankroll sync, disk space, PID lock."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-parseable log files.

    Each log line is a single JSON object with fields:
    timestamp, level, logger, message, and optional exc_info.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(
    level: str = "INFO",
    log_file: str = "data/logs/polyedge.log",
    json_log_file: str | None = "data/logs/polyedge.json.log",
) -> None:
    """Configure logging to console (human-readable) and file (optionally JSON).

    Args:
        level: Log level (DEBUG, INFO, WARNING, etc.)
        log_file: Path for the human-readable rotating log file.
        json_log_file: Path for the structured JSON log file. Set to None to disable.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Clear any existing handlers to prevent duplicates on restart
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(file_handler)

    # Structured JSON log file for machine parsing (monitoring, log aggregation)
    if json_log_file:
        Path(json_log_file).parent.mkdir(parents=True, exist_ok=True)
        json_handler = logging.handlers.RotatingFileHandler(
            json_log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        json_handler.setFormatter(_JsonFormatter())
        root.addHandler(json_handler)
        try:
            os.chmod(json_log_file, 0o600)
        except OSError:
            pass

    # L-5 FIX: Enforce restrictive permissions on log file
    try:
        os.chmod(log_file, 0o600)
    except OSError:
        pass  # May fail on first run before file exists

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def _sync_bankroll(settings, kalshi, risk_engine, position_manager, bankroll, logger) -> None:
    """Re-sync bankroll from Kalshi balance in live mode."""
    if settings.trading.mode != "live":
        return bankroll
    try:
        live_balance = await kalshi.get_balance()
        if live_balance is not None and live_balance > 0:
            if abs(live_balance - bankroll) > 1.0:
                logger.info(f"Bankroll sync: config=${bankroll:.2f} → live=${live_balance:.2f}")
            bankroll = live_balance
            risk_engine.update_bankroll(live_balance)
            position_manager.bankroll = live_balance
    except Exception as e:
        logger.warning(f"Failed to sync live balance: {e} — using config bankroll")
    return bankroll


# Disk space thresholds for health checks
DISK_CRITICAL_PCT = 0.05   # 5% free — database writes may fail
DISK_WARNING_PCT = 0.10    # 10% free — warn operator


def _check_disk_space(settings, logger) -> None:
    """Check disk space and log warnings if low (L-7)."""
    try:
        import shutil
        db_path = settings.database.path
        disk_usage = shutil.disk_usage(db_path if db_path != ":memory:" else ".")
        free_pct = disk_usage.free / disk_usage.total
        if free_pct < DISK_CRITICAL_PCT:
            logger.critical(
                f"DISK SPACE CRITICAL: {free_pct:.1%} free "
                f"({disk_usage.free / (1024**3):.1f} GB) — database writes may fail"
            )
        elif free_pct < DISK_WARNING_PCT:
            logger.warning(
                f"Disk space low: {free_pct:.1%} free "
                f"({disk_usage.free / (1024**3):.1f} GB)"
            )
    except Exception as e:
        logging.getLogger(__name__).debug(f"Disk space check failed (non-critical): {e}")


def _acquire_pid_lock(lock_path: str = "data/polyedge.pid") -> bool:
    """Acquire a PID lock file to prevent concurrent pm2 instances.

    Returns True if lock acquired, False if another instance is running.
    This prevents the race condition where pm2 restarts overlap and
    both instances try to trade the same signals (C-3).
    """
    lock_file = Path(lock_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text().strip())
            # Check if the old process is still running
            os.kill(old_pid, 0)  # Sends no signal, just checks existence
            # Process exists — another instance is running
            return False
        except (ProcessLookupError, ValueError, PermissionError):
            # Old process is dead or PID file is corrupt — safe to overwrite
            pass

    lock_file.write_text(str(os.getpid()))
    return True


def _release_pid_lock(lock_path: str = "data/polyedge.pid"):
    """Release the PID lock file on shutdown."""
    lock_file = Path(lock_path)
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            if pid == os.getpid():
                lock_file.unlink()
        except (ValueError, OSError):
            pass
