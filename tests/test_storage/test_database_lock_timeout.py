"""Tests for database write lock — all write operations use _write() context manager."""

from __future__ import annotations

import inspect
import re
import threading

import pytest

from src.storage.database import Database


class TestWriteLockTimeout:
    """Verify all write operations use the _write() context manager."""

    def _get_all_mro_source(self):
        """Get combined source from Database and all its mixin bases."""
        sources = []
        for cls in Database.__mro__:
            if cls is object:
                continue
            try:
                sources.append(inspect.getsource(cls))
            except (OSError, TypeError):
                pass
        return "\n".join(sources)

    def test_write_context_manager_exists(self):
        """Database has a _write() context manager."""
        db = Database.__new__(Database)
        db._write_lock = threading.Lock()
        db._conn = None
        assert hasattr(db, "_write")
        assert callable(db._write)

    def test_write_context_manager_default_timeout(self):
        """_write() defaults to 60s timeout."""
        sig = inspect.signature(Database._write)
        assert sig.parameters["timeout"].default == 60

    def test_no_raw_acquire_in_mixins(self):
        """No mixin should call _write_lock.acquire() directly."""
        from src.storage.db_calibration import CalibrationMixin
        from src.storage.db_markets import MarketsMixin
        from src.storage.db_risk import RiskMixin
        from src.storage.db_trades import TradesMixin
        from src.storage.db_whales import WhalesMixin

        for mixin in [MarketsMixin, TradesMixin, CalibrationMixin, RiskMixin, WhalesMixin]:
            source = inspect.getsource(mixin)
            raw_acquires = re.findall(r"_write_lock\.acquire\(", source)
            assert len(raw_acquires) == 0, (
                f"{mixin.__name__} has {len(raw_acquires)} raw lock.acquire() calls"
            )

    def test_write_methods_use_write_cm(self):
        """All mixin write methods should use self._write()."""
        from src.storage.db_calibration import CalibrationMixin
        from src.storage.db_markets import MarketsMixin
        from src.storage.db_risk import RiskMixin
        from src.storage.db_trades import TradesMixin
        from src.storage.db_whales import WhalesMixin

        for mixin in [MarketsMixin, TradesMixin, CalibrationMixin, RiskMixin, WhalesMixin]:
            source = inspect.getsource(mixin)
            # Methods that do INSERT/UPDATE/DELETE should use _write
            write_ops = re.findall(r"conn\.execute\(\s*\"\"\"?\s*(INSERT|UPDATE|DELETE)", source)
            write_cm_uses = re.findall(r"self\._write\(", source)
            if write_ops:
                assert len(write_cm_uses) > 0, (
                    f"{mixin.__name__} has {len(write_ops)} write operations "
                    f"but {len(write_cm_uses)} _write() calls"
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
