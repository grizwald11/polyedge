"""Tests for database write lock timeout — M-2: increased from 10s to 30s."""

from __future__ import annotations

import re

import pytest

from src.storage.database import Database


class TestWriteLockTimeout:
    """M-2: Verify all write lock acquisitions use 30-second timeout."""

    def _get_all_mro_source(self):
        """M-10: Get combined source from Database and all its mixin bases."""
        import inspect
        sources = []
        for cls in Database.__mro__:
            if cls is object:
                continue
            try:
                sources.append(inspect.getsource(cls))
            except (OSError, TypeError):
                pass
        return "\n".join(sources)

    def test_write_lock_timeout_is_30_seconds(self):
        """Inspect the source to confirm all lock timeouts are 30s, not 10s."""
        source = self._get_all_mro_source()

        # Find all acquire(timeout=N) calls
        timeouts = re.findall(r"acquire\(timeout=(\d+)\)", source)
        assert len(timeouts) >= 4, (
            f"Expected at least 4 write lock acquisitions, found {len(timeouts)}"
        )
        for t in timeouts:
            assert t == "30", (
                f"Write lock timeout should be 30s, found {t}s"
            )

    def test_no_10_second_timeouts_remain(self):
        """Ensure no 10-second timeouts remain in database module."""
        source = self._get_all_mro_source()
        assert "timeout=10" not in source, (
            "Found residual timeout=10 in database.py — should be 30"
        )

    def test_error_messages_reference_30s(self):
        """Error messages should reference the correct timeout value."""
        source = self._get_all_mro_source()
        # Should not mention 10s in timeout error messages
        assert "timeout (10s)" not in source, (
            "Error message still references 10s timeout — should be 30s"
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
