"""Tests for database write lock — M-4: retry helper with 60s timeout."""

from __future__ import annotations

import inspect
import re

import pytest

from src.storage.database import Database
from src.storage.db_trades import _acquire_write_lock


class TestWriteLockTimeout:
    """M-4: Verify write lock uses retry helper with 60-second timeout."""

    def _get_all_mro_source(self):
        """M-10: Get combined source from Database and all its mixin bases."""
        sources = []
        for cls in Database.__mro__:
            if cls is object:
                continue
            try:
                sources.append(inspect.getsource(cls))
            except (OSError, TypeError):
                pass
        return "\n".join(sources)

    def test_write_lock_helper_default_timeout(self):
        """Verify _acquire_write_lock helper defaults to 60s timeout."""
        sig = inspect.signature(_acquire_write_lock)
        assert sig.parameters["timeout"].default == 60

    def test_write_lock_helper_default_retries(self):
        """Verify _acquire_write_lock helper defaults to 1 retry."""
        sig = inspect.signature(_acquire_write_lock)
        assert sig.parameters["max_retries"].default == 1

    def test_trades_mixin_uses_helper(self):
        """Verify TradesMixin uses _acquire_write_lock (not raw acquire)."""
        from src.storage.db_trades import TradesMixin
        source = inspect.getsource(TradesMixin)
        assert "_acquire_write_lock" in source
        # No raw acquire calls should remain in the mixin
        raw_acquires = re.findall(r"_write_lock\.acquire\(", source)
        assert len(raw_acquires) == 0, (
            f"Found {len(raw_acquires)} raw lock.acquire() calls"
        )

    def test_no_10_second_timeouts_remain(self):
        """Ensure no 10-second timeouts remain in database module."""
        source = self._get_all_mro_source()
        assert "timeout=10" not in source, (
            "Found residual timeout=10 in database.py — should be 60"
        )

    def test_log_trade_uses_write_lock(self, tmp_db):
        """Verify log_trade acquires the write lock (basic smoke test)."""
        from src.core.models import Side, StrategyName, Trade
        trade = Trade(
            order_id="PE-test-1",
            market_id="TEST-MKT",
            token_id="TEST-MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
            paper=True,
        )
        row_id = tmp_db.log_trade(trade)
        assert row_id is not None
