"""Tests for leaderboard scraper — L-5 audit fix for zero test coverage."""

from __future__ import annotations

import pytest

from scripts.leaderboard import LeaderboardEntry, LeaderboardScraper


# ──────────────────────────────────────────────
# LeaderboardEntry dataclass
# ──────────────────────────────────────────────

class TestLeaderboardEntry:
    """Tests for the LeaderboardEntry dataclass."""

    def test_defaults(self):
        entry = LeaderboardEntry(username="alice", rank=1)
        assert entry.total_trades == 0
        assert entry.profit == 0.0
        assert entry.win_rate == 0.0
        assert entry.categories == []

    def test_categories_default_not_shared(self):
        """Each entry gets its own categories list (no mutable default sharing)."""
        a = LeaderboardEntry(username="a", rank=1)
        b = LeaderboardEntry(username="b", rank=2)
        a.categories.append("Politics")
        assert b.categories == []

    def test_explicit_values(self):
        entry = LeaderboardEntry(
            username="bob",
            rank=5,
            total_trades=100,
            profit=5000.0,
            win_rate=0.62,
            categories=["Politics", "Tech"],
        )
        assert entry.username == "bob"
        assert entry.rank == 5
        assert entry.total_trades == 100
        assert entry.profit == 5000.0
        assert entry.win_rate == 0.62
        assert entry.categories == ["Politics", "Tech"]

    def test_categories_none_becomes_empty_list(self):
        entry = LeaderboardEntry(username="c", rank=3, categories=None)
        assert entry.categories == []


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def scraper() -> LeaderboardScraper:
    return LeaderboardScraper(min_trades=50, min_win_rate=0.55)


@pytest.fixture
def sample_entries() -> list[LeaderboardEntry]:
    return [
        LeaderboardEntry("whale1", rank=1, total_trades=200, profit=10000.0, win_rate=0.65),
        LeaderboardEntry("whale2", rank=2, total_trades=150, profit=8000.0, win_rate=0.60),
        LeaderboardEntry("newbie", rank=50, total_trades=10, profit=100.0, win_rate=0.70),
        LeaderboardEntry("loser", rank=30, total_trades=100, profit=-500.0, win_rate=0.48),
        LeaderboardEntry("borderline", rank=20, total_trades=50, profit=1.0, win_rate=0.55),
    ]


# ──────────────────────────────────────────────
# LeaderboardScraper.filter_candidates
# ──────────────────────────────────────────────

class TestFilterCandidates:
    """Tests for filter_candidates — min trades, win rate, and positive profit."""

    def test_filters_by_all_criteria(self, scraper, sample_entries):
        result = scraper.filter_candidates(sample_entries)
        names = [e.username for e in result]
        assert "whale1" in names
        assert "whale2" in names
        assert "borderline" in names
        # newbie has too few trades, loser has negative profit and low win rate
        assert "newbie" not in names
        assert "loser" not in names

    def test_filters_low_trades(self, scraper):
        entries = [LeaderboardEntry("few", rank=1, total_trades=49, profit=100.0, win_rate=0.60)]
        assert scraper.filter_candidates(entries) == []

    def test_filters_low_win_rate(self, scraper):
        entries = [LeaderboardEntry("bad", rank=1, total_trades=100, profit=100.0, win_rate=0.54)]
        assert scraper.filter_candidates(entries) == []

    def test_filters_negative_profit(self, scraper):
        entries = [LeaderboardEntry("neg", rank=1, total_trades=100, profit=-1.0, win_rate=0.60)]
        assert scraper.filter_candidates(entries) == []

    def test_filters_zero_profit(self, scraper):
        entries = [LeaderboardEntry("zero", rank=1, total_trades=100, profit=0.0, win_rate=0.60)]
        assert scraper.filter_candidates(entries) == []

    def test_empty_list(self, scraper):
        assert scraper.filter_candidates([]) == []

    def test_all_filtered_out(self, scraper):
        entries = [
            LeaderboardEntry("a", rank=1, total_trades=5, profit=-10.0, win_rate=0.30),
            LeaderboardEntry("b", rank=2, total_trades=20, profit=0.0, win_rate=0.40),
        ]
        assert scraper.filter_candidates(entries) == []

    def test_custom_thresholds(self):
        scraper = LeaderboardScraper(min_trades=10, min_win_rate=0.50)
        entries = [LeaderboardEntry("ok", rank=1, total_trades=15, profit=50.0, win_rate=0.51)]
        assert len(scraper.filter_candidates(entries)) == 1


# ──────────────────────────────────────────────
# LeaderboardScraper.rank_candidates
# ──────────────────────────────────────────────

class TestRankCandidates:
    """Tests for rank_candidates — sorted by profit * win_rate descending."""

    def test_ranking_order(self, scraper, sample_entries):
        ranked = scraper.rank_candidates(sample_entries)
        scores = [e.profit * e.win_rate for e in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_single_entry(self, scraper):
        entries = [LeaderboardEntry("solo", rank=1, total_trades=100, profit=5000.0, win_rate=0.60)]
        ranked = scraper.rank_candidates(entries)
        assert len(ranked) == 1
        assert ranked[0].username == "solo"

    def test_empty_list(self, scraper):
        assert scraper.rank_candidates([]) == []

    def test_ties_in_score(self, scraper):
        """Entries with the same profit * win_rate should all appear (stable sort)."""
        entries = [
            LeaderboardEntry("a", rank=1, total_trades=100, profit=1000.0, win_rate=0.50),
            LeaderboardEntry("b", rank=2, total_trades=100, profit=500.0, win_rate=1.0),
            LeaderboardEntry("c", rank=3, total_trades=100, profit=1000.0, win_rate=0.50),
        ]
        ranked = scraper.rank_candidates(entries)
        assert len(ranked) == 3
        # All three have score 500; stable sort preserves original order
        names = [e.username for e in ranked]
        assert set(names) == {"a", "b", "c"}

    def test_rank_preserves_entries(self, scraper, sample_entries):
        """Ranking should not add or remove entries."""
        ranked = scraper.rank_candidates(sample_entries)
        assert len(ranked) == len(sample_entries)

    def test_negative_profit_ranked_last(self, scraper):
        entries = [
            LeaderboardEntry("neg", rank=1, total_trades=100, profit=-1000.0, win_rate=0.60),
            LeaderboardEntry("pos", rank=2, total_trades=100, profit=100.0, win_rate=0.55),
        ]
        ranked = scraper.rank_candidates(entries)
        assert ranked[0].username == "pos"
        assert ranked[1].username == "neg"
