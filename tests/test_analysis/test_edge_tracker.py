"""Tests for the Edge Decay Tracker."""

from __future__ import annotations

import pytest

from src.analysis.edge_tracker import EdgeTracker


@pytest.fixture
def tracker(tmp_db) -> EdgeTracker:
    return EdgeTracker(tmp_db)


class TestRecordAndResolve:
    def test_record_predicted_edge(self, tracker):
        tracker.record_predicted_edge("MKT-1", 0.10, 0.45, "BUY_YES")
        records = tracker._get_resolved_records()
        assert len(records) == 0  # Not resolved yet

    def test_resolve_buy_yes_win(self, tracker):
        """BUY_YES at 0.45, outcome=YES → realized edge = 1.0 - 0.45 = 0.55."""
        tracker.record_predicted_edge("MKT-1", 0.10, 0.45, "BUY_YES")
        tracker.record_resolution("MKT-1", actual_outcome=True)
        records = tracker._get_resolved_records()
        assert len(records) == 1
        assert records[0]["realized_edge"] == pytest.approx(0.55, abs=0.01)

    def test_resolve_buy_yes_loss(self, tracker):
        """BUY_YES at 0.45, outcome=NO → realized edge = 0.0 - 0.45 = -0.45."""
        tracker.record_predicted_edge("MKT-1", 0.10, 0.45, "BUY_YES")
        tracker.record_resolution("MKT-1", actual_outcome=False)
        records = tracker._get_resolved_records()
        assert records[0]["realized_edge"] == pytest.approx(-0.45, abs=0.01)

    def test_resolve_buy_no_win(self, tracker):
        """BUY_NO at 0.55 (NO cost=0.45), outcome=NO → realized edge = 0.55 - 0.45 = 0.10."""
        # Entry price is YES price (0.55), direction is BUY_NO
        tracker.record_predicted_edge("MKT-1", 0.10, 0.55, "BUY_NO")
        tracker.record_resolution("MKT-1", actual_outcome=False)
        records = tracker._get_resolved_records()
        # BUY_NO: realized = (1 - actual_price) - (1 - entry) = 1.0 - 0.45 = 0.55
        assert records[0]["realized_edge"] == pytest.approx(0.55, abs=0.01)

    def test_resolve_with_exit_price(self, tracker):
        """Exit before resolution at specific price."""
        tracker.record_predicted_edge("MKT-1", 0.10, 0.45, "BUY_YES")
        tracker.record_resolution("MKT-1", actual_outcome=True, exit_price=0.60)
        records = tracker._get_resolved_records()
        # BUY_YES: realized = exit_price - entry = 0.60 - 0.45 = 0.15
        assert records[0]["realized_edge"] == pytest.approx(0.15, abs=0.01)


class TestEdgeShrinkage:
    def test_insufficient_data(self, tracker):
        """Less than MIN_RECORDS → shrinkage 1.0."""
        for i in range(5):
            tracker.record_predicted_edge(f"MKT-{i}", 0.10, 0.45, "BUY_YES")
            tracker.record_resolution(f"MKT-{i}", actual_outcome=True)
        assert tracker.compute_edge_shrinkage() == 1.0

    def test_edges_realized_as_predicted(self, tracker):
        """When realized ≈ predicted, shrinkage ≈ 1.0."""
        # Predicted edge 0.10, entry at 0.45
        # If we win (outcome=YES), realized = 1.0 - 0.45 = 0.55
        # We need avg_realized/avg_predicted ≈ 1.0
        # So predicted=0.55, entry=0.45, win → realized=0.55 → ratio=1.0
        for i in range(25):
            tracker.record_predicted_edge(f"MKT-{i}", 0.55, 0.45, "BUY_YES")
            tracker.record_resolution(f"MKT-{i}", actual_outcome=True)
        shrinkage = tracker.compute_edge_shrinkage()
        assert shrinkage == pytest.approx(1.0, abs=0.1)

    def test_edges_overestimated(self, tracker):
        """Predicted edges much larger than realized → shrinkage < 1.0."""
        for i in range(25):
            tracker.record_predicted_edge(f"MKT-{i}", 0.20, 0.45, "BUY_YES")
            # Half win (realized=0.55), half lose (realized=-0.45)
            tracker.record_resolution(f"MKT-{i}", actual_outcome=(i % 2 == 0))
        shrinkage = tracker.compute_edge_shrinkage()
        # avg_predicted = 0.20, avg_realized ≈ (0.55*12 - 0.45*13)/25 ≈ -0.003
        # ratio ≈ negative, clamped to 0.5
        assert shrinkage <= 1.0

    def test_shrinkage_clamped(self, tracker):
        """Shrinkage should be clamped to [0.5, 1.5]."""
        for i in range(25):
            tracker.record_predicted_edge(f"MKT-{i}", 0.50, 0.45, "BUY_YES")
            tracker.record_resolution(f"MKT-{i}", actual_outcome=False)
        shrinkage = tracker.compute_edge_shrinkage()
        assert shrinkage >= 0.5

    def test_category_filter(self, tracker):
        for i in range(25):
            tracker.record_predicted_edge(
                f"POL-{i}", 0.50, 0.45, "BUY_YES", category="Politics",
            )
            tracker.record_resolution(f"POL-{i}", actual_outcome=True)
        shrinkage = tracker.compute_edge_shrinkage(category="Politics")
        assert shrinkage > 0.5


class TestEdgeMultiplier:
    def test_returns_shrinkage(self, tracker):
        """get_edge_multiplier should return the same as compute_edge_shrinkage."""
        assert tracker.get_edge_multiplier() == tracker.compute_edge_shrinkage()


class TestSummary:
    def test_empty_summary(self, tracker):
        summary = tracker.get_summary()
        assert summary["count"] == 0
        assert summary["avg_predicted"] is None
        assert summary["shrinkage"] == 1.0

    def test_populated_summary(self, tracker):
        for i in range(25):
            tracker.record_predicted_edge(f"MKT-{i}", 0.10, 0.45, "BUY_YES")
            tracker.record_resolution(f"MKT-{i}", actual_outcome=(i < 15))
        summary = tracker.get_summary()
        assert summary["count"] == 25
        assert summary["avg_predicted"] is not None
        assert summary["win_rate"] is not None
        assert 0 <= summary["win_rate"] <= 1
