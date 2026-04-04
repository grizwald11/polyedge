"""Tests for CalibrationMixin methods in src/storage/db_calibration.py."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import CalibrationRecord, StrategyName
from src.storage.database import Database


def _make_calibration(market_id: str = "TEST-MKT", **kwargs) -> CalibrationRecord:
    defaults = dict(
        market_id=market_id,
        market_question="Will X happen?",
        strategy=StrategyName.AI_PROBABILITY,
        predicted_probability=0.70,
        market_price_at_prediction=0.60,
    )
    defaults.update(kwargs)
    return CalibrationRecord(**defaults)


# ──────────────────────────────────────
# log_calibration
# ──────────────────────────────────────


class TestLogCalibration:
    """Tests for log_calibration()."""

    def test_returns_row_id(self, tmp_db: Database):
        rec = _make_calibration()
        row_id = tmp_db.log_calibration(rec)
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_stores_all_fields(self, tmp_db: Database):
        rec = _make_calibration(
            market_id="FIELD-CHECK",
            market_question="Will Y happen?",
            strategy=StrategyName.CROSS_ARB,
            predicted_probability=0.55,
            market_price_at_prediction=0.40,
            actual_outcome=True,
            resolved_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            prompt_variant="v2_concise",
        )
        tmp_db.log_calibration(rec)

        rows = tmp_db.get_all_calibration_records()
        assert len(rows) == 1
        row = rows[0]
        assert row["market_id"] == "FIELD-CHECK"
        assert row["market_question"] == "Will Y happen?"
        assert row["strategy"] == "cross_arb"
        assert row["predicted_probability"] == pytest.approx(0.55)
        assert row["market_price_at_prediction"] == pytest.approx(0.40)
        assert row["actual_outcome"] == 1  # True -> stored as 1
        assert row["resolved_at"] is not None

    def test_unresolved_record_has_null_outcome(self, tmp_db: Database):
        rec = _make_calibration()
        tmp_db.log_calibration(rec)

        rows = tmp_db.get_all_calibration_records()
        assert rows[0]["actual_outcome"] is None
        assert rows[0]["resolved_at"] is None

    def test_sequential_row_ids(self, tmp_db: Database):
        id1 = tmp_db.log_calibration(_make_calibration(market_id="A"))
        id2 = tmp_db.log_calibration(_make_calibration(market_id="B"))
        assert id2 > id1


# ──────────────────────────────────────
# store_prediction
# ──────────────────────────────────────


class TestStorePrediction:
    """Tests for store_prediction()."""

    def test_returns_row_id(self, tmp_db: Database):
        row_id = tmp_db.store_prediction(
            market_ticker="PRED-MKT",
            predicted_probability=0.65,
            predicted_side="YES",
            market_price=0.50,
        )
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_stores_with_defaults(self, tmp_db: Database):
        tmp_db.store_prediction(
            market_ticker="PRED-MKT",
            predicted_probability=0.65,
            predicted_side="YES",
            market_price=0.50,
        )
        rows = tmp_db.get_all_calibration_records()
        assert len(rows) == 1
        row = rows[0]
        assert row["market_id"] == "PRED-MKT"
        assert row["strategy"] == "ai_probability"
        assert row["predicted_probability"] == pytest.approx(0.65)
        assert row["market_price_at_prediction"] == pytest.approx(0.50)
        assert row["actual_outcome"] is None
        assert row["predicted_at"] is not None

    def test_stores_custom_strategy_and_question(self, tmp_db: Database):
        tmp_db.store_prediction(
            market_ticker="CUSTOM",
            predicted_probability=0.80,
            predicted_side="NO",
            market_price=0.75,
            strategy="whale_tracker",
            market_question="Will Z resolve?",
            prompt_variant="experimental_v3",
        )
        rows = tmp_db.get_all_calibration_records()
        row = rows[0]
        assert row["strategy"] == "whale_tracker"
        assert row["market_question"] == "Will Z resolve?"
        assert row["prompt_variant"] == "experimental_v3"

    def test_stores_confidence_bounds(self, tmp_db: Database):
        """confidence_low/high are accepted but not stored in the table (no columns).
        Verify the call doesn't raise."""
        row_id = tmp_db.store_prediction(
            market_ticker="CONF",
            predicted_probability=0.60,
            predicted_side="YES",
            market_price=0.55,
            confidence_low=0.45,
            confidence_high=0.75,
        )
        assert row_id >= 1


# ──────────────────────────────────────
# get_unresolved_predictions
# ──────────────────────────────────────


class TestGetUnresolvedPredictions:
    """Tests for get_unresolved_predictions()."""

    def test_empty_when_no_records(self, tmp_db: Database):
        assert tmp_db.get_unresolved_predictions() == []

    def test_returns_only_unresolved(self, tmp_db: Database):
        # Unresolved
        tmp_db.store_prediction("MKT-A", 0.70, "YES", 0.60)
        tmp_db.store_prediction("MKT-B", 0.40, "NO", 0.50)
        # Resolved
        resolved = _make_calibration(
            market_id="MKT-C",
            actual_outcome=True,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(resolved)

        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 2
        ids = {r["market_id"] for r in unresolved}
        assert ids == {"MKT-A", "MKT-B"}

    def test_all_resolved_returns_empty(self, tmp_db: Database):
        resolved = _make_calibration(
            actual_outcome=False,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(resolved)
        assert tmp_db.get_unresolved_predictions() == []


# ──────────────────────────────────────
# get_latest_prediction
# ──────────────────────────────────────


class TestGetLatestPrediction:
    """Tests for get_latest_prediction()."""

    def test_returns_none_when_no_predictions(self, tmp_db: Database):
        assert tmp_db.get_latest_prediction("NONEXISTENT") is None

    def test_returns_most_recent(self, tmp_db: Database):
        # Insert two predictions for same market with different timestamps
        early = _make_calibration(
            market_id="MKT-X",
            predicted_probability=0.50,
            market_price_at_prediction=0.45,
            predicted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        late = _make_calibration(
            market_id="MKT-X",
            predicted_probability=0.80,
            market_price_at_prediction=0.70,
            predicted_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        tmp_db.log_calibration(early)
        tmp_db.log_calibration(late)

        result = tmp_db.get_latest_prediction("MKT-X")
        assert result is not None
        assert result["predicted_probability"] == pytest.approx(0.80)
        assert result["market_price_at_prediction"] == pytest.approx(0.70)

    def test_does_not_return_other_markets(self, tmp_db: Database):
        tmp_db.store_prediction("MKT-A", 0.60, "YES", 0.50)
        tmp_db.store_prediction("MKT-B", 0.90, "YES", 0.80)

        result = tmp_db.get_latest_prediction("MKT-A")
        assert result["predicted_probability"] == pytest.approx(0.60)

    def test_result_has_expected_keys(self, tmp_db: Database):
        tmp_db.store_prediction("MKT-K", 0.55, "YES", 0.45)
        result = tmp_db.get_latest_prediction("MKT-K")
        assert "predicted_probability" in result
        assert "market_price_at_prediction" in result
        assert "predicted_at" in result


# ──────────────────────────────────────
# get_all_calibration_records
# ──────────────────────────────────────


class TestGetAllCalibrationRecords:
    """Tests for get_all_calibration_records()."""

    def test_empty_when_no_records(self, tmp_db: Database):
        assert tmp_db.get_all_calibration_records() == []

    def test_returns_all_records(self, tmp_db: Database):
        tmp_db.store_prediction("A", 0.50, "YES", 0.40)
        tmp_db.store_prediction("B", 0.60, "YES", 0.50)
        resolved = _make_calibration(market_id="C", actual_outcome=True,
                                      resolved_at=datetime.now(timezone.utc))
        tmp_db.log_calibration(resolved)

        rows = tmp_db.get_all_calibration_records()
        assert len(rows) == 3

    def test_ordered_by_predicted_at_desc(self, tmp_db: Database):
        old = _make_calibration(
            market_id="OLD",
            predicted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        new = _make_calibration(
            market_id="NEW",
            predicted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        tmp_db.log_calibration(old)
        tmp_db.log_calibration(new)

        rows = tmp_db.get_all_calibration_records()
        assert rows[0]["market_id"] == "NEW"
        assert rows[1]["market_id"] == "OLD"


# ──────────────────────────────────────
# get_resolved_calibration_records
# ──────────────────────────────────────


class TestGetResolvedCalibrationRecords:
    """Tests for get_resolved_calibration_records() (Platt scaling format)."""

    def test_empty_when_no_resolved(self, tmp_db: Database):
        tmp_db.store_prediction("MKT", 0.70, "YES", 0.60)
        assert tmp_db.get_resolved_calibration_records() == []

    def test_returns_only_resolved(self, tmp_db: Database):
        # Unresolved
        tmp_db.store_prediction("UNRES", 0.50, "YES", 0.40)
        # Resolved YES
        yes_rec = _make_calibration(
            market_id="RES-YES",
            predicted_probability=0.75,
            actual_outcome=True,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(yes_rec)
        # Resolved NO
        no_rec = _make_calibration(
            market_id="RES-NO",
            predicted_probability=0.30,
            actual_outcome=False,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(no_rec)

        rows = tmp_db.get_resolved_calibration_records()
        assert len(rows) == 2

    def test_format_for_platt_scaling(self, tmp_db: Database):
        """Verify output has predicted_probability and actual_outcome as 'Yes'/'No'."""
        yes_rec = _make_calibration(
            market_id="PLATT-Y",
            predicted_probability=0.80,
            actual_outcome=True,
            resolved_at=datetime.now(timezone.utc),
        )
        no_rec = _make_calibration(
            market_id="PLATT-N",
            predicted_probability=0.20,
            actual_outcome=False,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(yes_rec)
        tmp_db.log_calibration(no_rec)

        rows = tmp_db.get_resolved_calibration_records()
        probs = {r["predicted_probability"]: r["actual_outcome"] for r in rows}
        assert probs[0.80] == "Yes"
        assert probs[0.20] == "No"

    def test_only_predicted_probability_and_outcome_keys(self, tmp_db: Database):
        rec = _make_calibration(
            actual_outcome=True,
            resolved_at=datetime.now(timezone.utc),
        )
        tmp_db.log_calibration(rec)

        rows = tmp_db.get_resolved_calibration_records()
        assert set(rows[0].keys()) == {"predicted_probability", "actual_outcome"}


# ──────────────────────────────────────
# get_resolved_predictions
# ──────────────────────────────────────


class TestGetResolvedPredictions:
    """Tests for get_resolved_predictions() with optional strategy/days filters."""

    def _seed_resolved(self, tmp_db: Database):
        """Insert a mix of resolved and unresolved predictions across strategies."""
        # AI probability, resolved YES, recent
        rec1 = _make_calibration(
            market_id="AI-1",
            strategy=StrategyName.AI_PROBABILITY,
            predicted_probability=0.70,
            actual_outcome=True,
            resolved_at=datetime.now(timezone.utc),
        )
        # Whale tracker, resolved NO, recent
        rec2 = _make_calibration(
            market_id="WHALE-1",
            strategy=StrategyName.WHALE_TRACKER,
            predicted_probability=0.30,
            actual_outcome=False,
            resolved_at=datetime.now(timezone.utc),
        )
        # AI probability, unresolved
        rec3 = _make_calibration(
            market_id="AI-UNRES",
            strategy=StrategyName.AI_PROBABILITY,
            predicted_probability=0.50,
        )
        # AI probability, resolved, old (60 days ago)
        rec4 = _make_calibration(
            market_id="AI-OLD",
            strategy=StrategyName.AI_PROBABILITY,
            predicted_probability=0.65,
            actual_outcome=True,
            predicted_at=datetime.now(timezone.utc) - timedelta(days=60),
            resolved_at=datetime.now(timezone.utc) - timedelta(days=55),
        )
        for r in [rec1, rec2, rec3, rec4]:
            tmp_db.log_calibration(r)

    def test_no_filters_returns_all_resolved(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        rows = tmp_db.get_resolved_predictions()
        assert len(rows) == 3  # 3 resolved, 1 unresolved excluded

    def test_filter_by_strategy(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        rows = tmp_db.get_resolved_predictions(strategy="ai_probability")
        assert len(rows) == 2
        assert all(r["strategy"] == "ai_probability" for r in rows)

    def test_filter_by_strategy_no_match(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        rows = tmp_db.get_resolved_predictions(strategy="news_reactive")
        assert len(rows) == 0

    def test_filter_by_days(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        # Only records from last 7 days
        rows = tmp_db.get_resolved_predictions(days=7)
        # Should exclude the 60-day-old record
        market_ids = {r["market_id"] for r in rows}
        assert "AI-OLD" not in market_ids
        assert len(rows) == 2  # AI-1 and WHALE-1

    def test_filter_by_strategy_and_days(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        rows = tmp_db.get_resolved_predictions(strategy="ai_probability", days=7)
        assert len(rows) == 1
        assert rows[0]["market_id"] == "AI-1"

    def test_ordered_by_resolved_at_desc(self, tmp_db: Database):
        self._seed_resolved(tmp_db)
        rows = tmp_db.get_resolved_predictions()
        # All should have resolved_at, most recent first
        for i in range(len(rows) - 1):
            assert rows[i]["resolved_at"] >= rows[i + 1]["resolved_at"]


# ──────────────────────────────────────
# update_resolution
# ──────────────────────────────────────


class TestUpdateResolution:
    """Tests for update_resolution()."""

    def test_resolves_unresolved_predictions(self, tmp_db: Database):
        tmp_db.store_prediction("MKT-RES", 0.70, "YES", 0.60)
        tmp_db.store_prediction("MKT-RES", 0.65, "YES", 0.55)

        count = tmp_db.update_resolution("MKT-RES", actual_outcome=1)
        assert count == 2

        unresolved = tmp_db.get_unresolved_predictions()
        mkt_res_unresolved = [r for r in unresolved if r["market_id"] == "MKT-RES"]
        assert len(mkt_res_unresolved) == 0

    def test_returns_rowcount(self, tmp_db: Database):
        tmp_db.store_prediction("SINGLE", 0.50, "YES", 0.40)
        count = tmp_db.update_resolution("SINGLE", actual_outcome=0)
        assert count == 1

    def test_returns_zero_when_no_match(self, tmp_db: Database):
        count = tmp_db.update_resolution("NONEXISTENT", actual_outcome=1)
        assert count == 0

    def test_does_not_double_resolve(self, tmp_db: Database):
        """Resolving an already-resolved prediction should not update it again."""
        tmp_db.store_prediction("DBL", 0.60, "YES", 0.50)

        # First resolution
        count1 = tmp_db.update_resolution("DBL", actual_outcome=1, brier_score=0.16)
        assert count1 == 1

        # Second resolution attempt — should find 0 unresolved rows
        count2 = tmp_db.update_resolution("DBL", actual_outcome=0, brier_score=0.36)
        assert count2 == 0

        # Verify original resolution is preserved
        rows = tmp_db.get_resolved_predictions()
        dbl_rows = [r for r in rows if r["market_id"] == "DBL"]
        assert len(dbl_rows) == 1
        assert dbl_rows[0]["actual_outcome"] == 1
        assert dbl_rows[0]["brier_score"] == pytest.approx(0.16)

    def test_stores_brier_score_and_pnl(self, tmp_db: Database):
        tmp_db.store_prediction("METRICS", 0.80, "YES", 0.70)
        tmp_db.update_resolution(
            "METRICS",
            actual_outcome=1,
            brier_score=0.04,
            profit_loss=12.50,
        )
        rows = tmp_db.get_resolved_predictions()
        row = [r for r in rows if r["market_id"] == "METRICS"][0]
        assert row["brier_score"] == pytest.approx(0.04)
        assert row["profit_loss"] == pytest.approx(12.50)

    def test_sets_resolved_at_timestamp(self, tmp_db: Database):
        tmp_db.store_prediction("TS-CHECK", 0.55, "YES", 0.45)
        before = datetime.now(timezone.utc)
        tmp_db.update_resolution("TS-CHECK", actual_outcome=0)

        rows = tmp_db.get_resolved_predictions()
        row = [r for r in rows if r["market_id"] == "TS-CHECK"][0]
        assert row["resolved_at"] is not None
        # Resolved_at should be a recent ISO timestamp string
        resolved_dt = datetime.fromisoformat(row["resolved_at"])
        assert resolved_dt >= before.replace(microsecond=0) - timedelta(seconds=2)

    def test_only_resolves_target_market(self, tmp_db: Database):
        """Resolving one market should not affect another."""
        tmp_db.store_prediction("TARGET", 0.70, "YES", 0.60)
        tmp_db.store_prediction("OTHER", 0.40, "NO", 0.50)

        tmp_db.update_resolution("TARGET", actual_outcome=1)

        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["market_id"] == "OTHER"


# ──────────────────────────────────────
# Integration: full lifecycle
# ──────────────────────────────────────


class TestCalibrationLifecycle:
    """End-to-end test covering the prediction -> resolution lifecycle."""

    def test_predict_then_resolve_then_query(self, tmp_db: Database):
        # 1. Store predictions
        id1 = tmp_db.store_prediction("LIFE-A", 0.75, "YES", 0.65, strategy="ai_probability")
        id2 = tmp_db.store_prediction("LIFE-B", 0.30, "NO", 0.40, strategy="whale_tracker")
        id3 = tmp_db.store_prediction("LIFE-C", 0.55, "YES", 0.50, strategy="ai_probability")
        assert all(isinstance(i, int) for i in [id1, id2, id3])

        # 2. All three should be unresolved
        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 3

        # 3. Resolve LIFE-A as YES
        cnt = tmp_db.update_resolution("LIFE-A", actual_outcome=1, brier_score=0.0625)
        assert cnt == 1

        # 4. Resolve LIFE-B as YES (whale was wrong)
        cnt = tmp_db.update_resolution("LIFE-B", actual_outcome=1, brier_score=0.49)
        assert cnt == 1

        # 5. LIFE-C still unresolved
        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["market_id"] == "LIFE-C"

        # 6. Resolved predictions query
        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 2

        # 7. Filter by strategy
        ai_resolved = tmp_db.get_resolved_predictions(strategy="ai_probability")
        assert len(ai_resolved) == 1
        assert ai_resolved[0]["market_id"] == "LIFE-A"

        # 8. Platt scaling format
        platt = tmp_db.get_resolved_calibration_records()
        assert len(platt) == 2
        assert all("predicted_probability" in r for r in platt)
        assert all(r["actual_outcome"] in ("Yes", "No") for r in platt)

        # 9. Latest prediction for LIFE-C
        latest = tmp_db.get_latest_prediction("LIFE-C")
        assert latest["predicted_probability"] == pytest.approx(0.55)

        # 10. All records
        all_recs = tmp_db.get_all_calibration_records()
        assert len(all_recs) == 3
