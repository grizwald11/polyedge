"""Tests for resolution tracker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.analysis.resolution_tracker import ResolutionTracker
from src.core.models import StrategyName


@pytest.fixture
def tracker(tmp_db) -> ResolutionTracker:
    kalshi = AsyncMock()
    return ResolutionTracker(kalshi=kalshi, db=tmp_db)


def _seed_unresolved(tmp_db, n: int = 3):
    """Seed unresolved predictions into the database."""
    from datetime import datetime, timezone

    conn = tmp_db._get_conn()
    for i in range(n):
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, strategy, predicted_probability,
                market_price_at_prediction, predicted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"MKT-{i}",
                f"Will event {i} happen?",
                "ai_probability",
                0.6 + i * 0.1,
                0.5 + i * 0.1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    conn.commit()




class TestCheckResolutions:
    @pytest.mark.asyncio
    async def test_no_unresolved(self, tracker):
        count = await tracker.check_resolutions()
        assert count == 0

    @pytest.mark.asyncio
    async def test_resolves_settled_yes(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 1)

        tracker.kalshi.get_market = AsyncMock(return_value={
            "status": "settled",
            "result": "yes",
        })

        count = await tracker.check_resolutions()
        assert count == 1

        # Verify the prediction was updated
        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["actual_outcome"] == 1
        assert resolved[0]["brier_score"] is not None

    @pytest.mark.asyncio
    async def test_resolves_settled_no(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 1)

        tracker.kalshi.get_market = AsyncMock(return_value={
            "status": "settled",
            "result": "no",
        })

        count = await tracker.check_resolutions()
        assert count == 1

        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["actual_outcome"] == 0

    @pytest.mark.asyncio
    async def test_ignores_unsettled(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 1)

        tracker.kalshi.get_market = AsyncMock(return_value={
            "status": "active",
            "result": "",
        })

        count = await tracker.check_resolutions()
        assert count == 0

        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1

    @pytest.mark.asyncio
    async def test_multiple_markets(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 3)

        async def mock_get_market(ticker):
            if ticker == "MKT-0":
                return {"status": "settled", "result": "yes"}
            elif ticker == "MKT-1":
                return {"status": "settled", "result": "no"}
            else:
                return {"status": "active", "result": ""}

        tracker.kalshi.get_market = AsyncMock(side_effect=mock_get_market)

        count = await tracker.check_resolutions()
        assert count == 2

        unresolved = tmp_db.get_unresolved_predictions()
        assert len(unresolved) == 1

    @pytest.mark.asyncio
    async def test_api_failure_continues(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 2)

        call_count = 0

        async def mock_get_market(ticker):
            nonlocal call_count
            call_count += 1
            if ticker == "MKT-0":
                raise Exception("API error")
            return {"status": "settled", "result": "yes"}

        tracker.kalshi.get_market = AsyncMock(side_effect=mock_get_market)

        count = await tracker.check_resolutions()
        # MKT-0 fails, MKT-1 succeeds
        assert count == 1

    @pytest.mark.asyncio
    async def test_brier_score_computed(self, tracker, tmp_db):
        """Verify Brier score is correctly computed on resolution."""
        _seed_unresolved(tmp_db, 1)  # MKT-0 has predicted_probability=0.6

        tracker.kalshi.get_market = AsyncMock(return_value={
            "status": "settled",
            "result": "yes",
        })

        await tracker.check_resolutions()

        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        # Brier = (0.6 - 1.0)^2 = 0.16
        assert resolved[0]["brier_score"] == pytest.approx(0.16)

    @pytest.mark.asyncio
    async def test_multiple_predictions_same_market(self, tracker, tmp_db):
        """Multiple predictions for the same market all get resolved."""
        from datetime import datetime, timezone

        conn = tmp_db._get_conn()
        for i in range(3):
            conn.execute(
                """INSERT INTO calibration_records
                   (market_id, market_question, strategy, predicted_probability,
                    market_price_at_prediction, predicted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("MKT-SAME", "Same market?", "ai_probability",
                 0.5 + i * 0.1, 0.45, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    


        tracker.kalshi.get_market = AsyncMock(return_value={
            "status": "settled",
            "result": "no",
        })

        count = await tracker.check_resolutions()
        assert count == 1  # 1 market resolved

        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 3  # but 3 predictions updated

    @pytest.mark.asyncio
    async def test_null_api_response(self, tracker, tmp_db):
        _seed_unresolved(tmp_db, 1)

        tracker.kalshi.get_market = AsyncMock(return_value=None)

        count = await tracker.check_resolutions()
        assert count == 0
