"""Orchestrator package — split from src/main.py for maintainability.

Re-exports the key public functions so existing imports continue to work.
"""

from src.orchestrator.startup import setup_logging, _sync_bankroll, _acquire_pid_lock, _release_pid_lock
from src.orchestrator.scan_cycle import scan_and_trade
from src.orchestrator.trade_cycle import _execute_signals, _process_exits
from src.orchestrator.lifecycle import run_trading_loop, main

__all__ = [
    "setup_logging",
    "_sync_bankroll",
    "_acquire_pid_lock",
    "_release_pid_lock",
    "scan_and_trade",
    "_execute_signals",
    "_process_exits",
    "run_trading_loop",
    "main",
]
