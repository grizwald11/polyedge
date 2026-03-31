"""Tests for outcome-validated prompt selection (Feature 7).

Verifies that:
- Predictions are recorded per variant with market_id
- Actual outcomes compute real Brier scores and update Thompson params
- State persists to and loads from the database
- Resolution tracker hooks variant outcome recording
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from src.analysis.prompt_ab_testing import PromptVariantManager, VariantRecord
from src.core.models import MarketCategory


@pytest.fixture
def manager():
    return PromptVariantManager(enabled=True)


class TestRecordPrediction:
    def test_records_pending(self, manager):
        manager.record_prediction(
            MarketCategory.POLITICS, "control", "MKT-1", 0.70
        )
        pending = manager._pending_predictions
        assert "Politics" in pending
        assert "MKT-1" in pending["Politics"]
        assert pending["Politics"]["MKT-1"]["variant_name"] == "control"
        assert pending["Politics"]["MKT-1"]["predicted_prob"] == 0.70

    def test_latest_prediction_wins(self, manager):
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.70)
        manager.record_prediction(MarketCategory.POLITICS, "explicit_base_rate", "MKT-1", 0.60)
        # Latest prediction should overwrite
        assert manager._pending_predictions["Politics"]["MKT-1"]["variant_name"] == "explicit_base_rate"

    def test_multiple_markets(self, manager):
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.70)
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-2", 0.40)
        assert len(manager._pending_predictions["Politics"]) == 2


class TestRecordActualOutcome:
    def test_updates_thompson_params(self, manager):
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.80)
        updated = manager.record_actual_outcome("MKT-1", True)
        assert updated == 1

        # Brier = (0.80 - 1.0)^2 = 0.04 — very good prediction
        record = manager._records["Politics"]["control"]
        assert record.total_predictions == 1
        assert record.sum_brier == pytest.approx(0.04, abs=0.001)

    def test_bad_prediction_penalizes(self, manager):
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.80)
        updated = manager.record_actual_outcome("MKT-1", False)
        assert updated == 1

        # Brier = (0.80 - 0.0)^2 = 0.64 — very bad prediction
        record = manager._records["Politics"]["control"]
        assert record.sum_brier == pytest.approx(0.64, abs=0.001)

    def test_removes_from_pending(self, manager):
        manager.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.70)
        manager.record_actual_outcome("MKT-1", True)
        assert "MKT-1" not in manager._pending_predictions.get("Politics", {})

    def test_unknown_market_returns_zero(self, manager):
        updated = manager.record_actual_outcome("NONEXISTENT", True)
        assert updated == 0

    def test_cross_category_resolution(self, manager):
        """Market prediction in one category resolves correctly."""
        manager.record_prediction(MarketCategory.FED_MACRO, "devils_advocate", "FED-1", 0.50)
        manager.record_prediction(MarketCategory.POLITICS, "control", "POL-1", 0.60)

        manager.record_actual_outcome("FED-1", False)

        # Only FED_MACRO should be updated
        fed_record = manager._records["Fed/Macro"]["devils_advocate"]
        assert fed_record.total_predictions == 1
        # POL-1 should still be pending
        assert "POL-1" in manager._pending_predictions["Politics"]


class TestPersistence:
    def _make_db(self, tmp_path):
        """Create a minimal in-memory DB with the prompt_variant_stats table."""
        db = MagicMock()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE prompt_variant_stats (
                category TEXT NOT NULL,
                variant_name TEXT NOT NULL,
                alpha REAL DEFAULT 1.0,
                beta REAL DEFAULT 1.0,
                total_predictions INTEGER DEFAULT 0,
                sum_brier REAL DEFAULT 0.0,
                updated_at TEXT,
                PRIMARY KEY (category, variant_name)
            )
        """)
        db._get_conn.return_value = conn
        return db, conn

    def test_save_and_load_roundtrip(self, tmp_path):
        db, conn = self._make_db(tmp_path)

        # Create manager with some state
        mgr1 = PromptVariantManager(enabled=True)
        mgr1.record_prediction(MarketCategory.POLITICS, "control", "MKT-1", 0.70)
        mgr1.record_actual_outcome("MKT-1", True)
        mgr1.record_prediction(MarketCategory.POLITICS, "control", "MKT-2", 0.30)
        mgr1.record_actual_outcome("MKT-2", False)

        # Save state
        mgr1.save_state(db)

        # Load into a fresh manager
        mgr2 = PromptVariantManager(enabled=True)
        mgr2.load_state(db)

        # Verify state was restored
        assert "Politics" in mgr2._records
        control = mgr2._records["Politics"]["control"]
        assert control.total_predictions == 2
        assert control.sum_brier > 0

    def test_load_skips_unknown_variants(self, tmp_path):
        db, conn = self._make_db(tmp_path)

        # Insert an unknown variant
        conn.execute(
            "INSERT INTO prompt_variant_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Politics", "unknown_variant", 5.0, 3.0, 10, 2.0, "2026-03-31"),
        )
        conn.commit()

        mgr = PromptVariantManager(enabled=True)
        mgr.load_state(db)

        # Should not have loaded the unknown variant
        if "Politics" in mgr._records:
            assert "unknown_variant" not in mgr._records["Politics"]

    def test_save_empty_state(self, tmp_path):
        db, conn = self._make_db(tmp_path)
        mgr = PromptVariantManager(enabled=True)
        # Should not error on empty state
        mgr.save_state(db)


class TestStorePredictonVariant:
    """Test that store_prediction passes prompt_variant through."""

    def test_store_prediction_with_variant(self, tmp_db):
        db = tmp_db

        # Need a market for FK
        conn = db._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated) "
            "VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')",
            ("TEST-MKT", "Test question?", "Politics"),
        )
        conn.commit()

        row_id = db.store_prediction(
            market_ticker="TEST-MKT",
            predicted_probability=0.70,
            predicted_side="YES",
            market_price=0.60,
            prompt_variant="explicit_base_rate",
        )
        assert row_id > 0

        # Verify the variant was stored
        row = conn.execute(
            "SELECT prompt_variant FROM calibration_records WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["prompt_variant"] == "explicit_base_rate"

    def test_store_prediction_default_empty(self, tmp_db):
        db = tmp_db

        conn = db._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated) "
            "VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')",
            ("TEST-MKT2", "Test question 2?", "Politics"),
        )
        conn.commit()

        row_id = db.store_prediction(
            market_ticker="TEST-MKT2",
            predicted_probability=0.50,
            predicted_side="YES",
            market_price=0.55,
        )

        row = conn.execute(
            "SELECT prompt_variant FROM calibration_records WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["prompt_variant"] == ""


class TestResolutionTrackerIntegration:
    """Test that resolution tracker calls variant_manager on resolution."""

    def test_resolve_calls_variant_manager(self):
        from src.analysis.resolution_tracker import ResolutionTracker

        db = MagicMock()
        kalshi = MagicMock()
        variant_mgr = MagicMock()
        variant_mgr.record_actual_outcome.return_value = 1

        tracker = ResolutionTracker(kalshi, db, variant_manager=variant_mgr)

        # Mock DB responses for _resolve_predictions
        conn = MagicMock()
        db._get_conn.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"id": 1, "predicted_probability": 0.70, "market_price_at_prediction": 0.60}
        ]

        tracker._resolve_predictions("MKT-1", "kalshi", True)

        variant_mgr.record_actual_outcome.assert_called_once_with("MKT-1", True)
        variant_mgr.save_state.assert_called_once_with(db)

    def test_resolve_works_without_variant_manager(self):
        from src.analysis.resolution_tracker import ResolutionTracker

        db = MagicMock()
        kalshi = MagicMock()

        # No variant_manager passed
        tracker = ResolutionTracker(kalshi, db)

        conn = MagicMock()
        db._get_conn.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"id": 1, "predicted_probability": 0.70, "market_price_at_prediction": 0.60}
        ]

        # Should not raise
        count = tracker._resolve_predictions("MKT-1", "kalshi", True)
        assert count == 1
