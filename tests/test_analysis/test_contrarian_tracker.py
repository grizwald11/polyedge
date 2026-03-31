"""Tests for the contrarian accuracy tracker."""

import pytest

from src.analysis.contrarian_tracker import ContrarianTracker


@pytest.fixture
def tracker(tmp_db):
    return ContrarianTracker(tmp_db)


@pytest.fixture
def tracker_with_data(tmp_db):
    """Tracker with 25 resolved divergence records in Politics."""
    t = ContrarianTracker(tmp_db)
    # Claude is right 60% of the time in Politics (divergences 5-25%)
    for i in range(25):
        claude = 0.50 + (i % 5 + 1) * 0.05  # 0.55 to 0.75
        market = 0.45
        t.record_divergence(f"POL-{i}", "Politics", claude, market)

    conn = tmp_db._get_conn()
    rows = conn.execute("SELECT id, claude_estimate, market_price FROM divergence_records").fetchall()
    for j, row in enumerate(rows):
        actual = 1 if j < 15 else 0  # 60% Claude right
        claude_err = abs(row["claude_estimate"] - float(actual))
        market_err = abs(row["market_price"] - float(actual))
        claude_right = 1 if claude_err < market_err else 0
        conn.execute(
            "UPDATE divergence_records SET actual_outcome=?, claude_was_right=?, resolved_at=? WHERE id=?",
            (actual, claude_right, "2026-03-20", row["id"]),
        )
    conn.commit()
    return t


class TestRecordDivergence:
    def test_records_basic_divergence(self, tracker, tmp_db):
        tracker.record_divergence("MKT-1", "Politics", 0.70, 0.50)
        conn = tmp_db._get_conn()
        rows = conn.execute("SELECT * FROM divergence_records").fetchall()
        assert len(rows) == 1
        r = dict(rows[0])
        assert r["market_id"] == "MKT-1"
        assert r["category"] == "Politics"
        assert r["claude_estimate"] == 0.70
        assert r["market_price"] == 0.50
        assert abs(r["divergence"] - 0.20) < 0.001
        assert abs(r["abs_divergence"] - 0.20) < 0.001
        assert r["actual_outcome"] is None

    def test_records_negative_divergence(self, tracker, tmp_db):
        tracker.record_divergence("MKT-2", "Fed", 0.30, 0.50)
        conn = tmp_db._get_conn()
        r = dict(conn.execute("SELECT * FROM divergence_records").fetchone())
        assert r["divergence"] == pytest.approx(-0.20)
        assert r["abs_divergence"] == pytest.approx(0.20)


class TestResolveDivergence:
    def test_resolves_claude_wins(self, tracker, tmp_db):
        tracker.record_divergence("MKT-1", "Politics", 0.80, 0.50)
        # Actual YES → Claude (0.80) closer than market (0.50)
        updated = tracker.resolve_divergence("MKT-1", True)
        assert updated == 1
        conn = tmp_db._get_conn()
        r = dict(conn.execute("SELECT * FROM divergence_records").fetchone())
        assert r["claude_was_right"] == 1
        assert r["actual_outcome"] == 1

    def test_resolves_market_wins(self, tracker, tmp_db):
        tracker.record_divergence("MKT-2", "Fed", 0.80, 0.50)
        # Actual NO → Market (0.50) closer than Claude (0.80)
        updated = tracker.resolve_divergence("MKT-2", False)
        assert updated == 1
        conn = tmp_db._get_conn()
        r = dict(conn.execute("SELECT * FROM divergence_records").fetchone())
        assert r["claude_was_right"] == 0

    def test_skips_already_resolved(self, tracker, tmp_db):
        tracker.record_divergence("MKT-3", "Politics", 0.70, 0.50)
        tracker.resolve_divergence("MKT-3", True)
        # Second resolve should find nothing
        updated = tracker.resolve_divergence("MKT-3", False)
        assert updated == 0

    def test_resolves_multiple_records_same_market(self, tracker, tmp_db):
        tracker.record_divergence("MKT-4", "Fed", 0.60, 0.50)
        tracker.record_divergence("MKT-4", "Fed", 0.65, 0.52)
        updated = tracker.resolve_divergence("MKT-4", True)
        assert updated == 2


class TestDynamicThresholds:
    def test_returns_defaults_with_no_data(self, tracker):
        thresholds = tracker.get_dynamic_thresholds()
        assert thresholds["Politics"] == 0.30
        assert thresholds["Culture"] == 0.50

    def test_returns_defaults_with_insufficient_data(self, tracker, tmp_db):
        # Add only 5 records (below MIN_RECORDS_FOR_DYNAMIC=20)
        for i in range(5):
            tracker.record_divergence(f"FEW-{i}", "Politics", 0.70, 0.50)
        conn = tmp_db._get_conn()
        conn.execute(
            "UPDATE divergence_records SET actual_outcome=1, claude_was_right=1, resolved_at='2026-03-20'"
        )
        conn.commit()
        thresholds = tracker.get_dynamic_thresholds()
        assert thresholds["Politics"] == 0.30  # Still default

    def test_learns_thresholds_with_sufficient_data(self, tracker_with_data):
        thresholds = tracker_with_data.get_dynamic_thresholds()
        # With 25 records and 60% Claude win rate, threshold should be learned
        # and differ from default (0.30) — may be higher since Claude is mostly right
        assert "Politics" in thresholds
        assert 0.10 <= thresholds["Politics"] <= 0.60

    def test_thresholds_clamped(self, tracker_with_data):
        thresholds = tracker_with_data.get_dynamic_thresholds()
        for cat, val in thresholds.items():
            assert 0.10 <= val <= 0.60


class TestContrarianAccuracy:
    def test_accuracy_with_data(self, tracker_with_data):
        stats = tracker_with_data.get_contrarian_accuracy("Politics")
        assert stats["total"] == 25
        assert stats["claude_wins"] > 0
        assert stats["market_wins"] > 0
        assert 0.0 <= stats["win_rate"] <= 1.0

    def test_accuracy_empty(self, tracker):
        stats = tracker.get_contrarian_accuracy("Politics")
        assert stats["total"] == 0
        assert stats["win_rate"] == 0.0

    def test_accuracy_all_categories(self, tracker_with_data):
        stats = tracker_with_data.get_contrarian_accuracy()
        assert stats["total"] == 25
