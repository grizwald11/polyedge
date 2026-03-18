"""Tests for leaderboard scraper."""

from __future__ import annotations

import pytest

from src.data.leaderboard import LeaderboardEntry, LeaderboardScraper


class TestLeaderboardScraper:
    def test_filter_candidates(self):
        scraper = LeaderboardScraper(min_trades=50, min_win_rate=0.55)
        entries = [
            LeaderboardEntry(username="good", rank=1, total_trades=100, profit=5000, win_rate=0.60),
            LeaderboardEntry(username="low_trades", rank=2, total_trades=10, profit=500, win_rate=0.70),
            LeaderboardEntry(username="low_wr", rank=3, total_trades=100, profit=500, win_rate=0.45),
            LeaderboardEntry(username="negative", rank=4, total_trades=100, profit=-500, win_rate=0.60),
        ]

        filtered = scraper.filter_candidates(entries)

        assert len(filtered) == 1
        assert filtered[0].username == "good"

    def test_rank_candidates(self):
        scraper = LeaderboardScraper()
        entries = [
            LeaderboardEntry(username="a", rank=1, total_trades=100, profit=5000, win_rate=0.55),
            LeaderboardEntry(username="b", rank=2, total_trades=100, profit=10000, win_rate=0.60),
            LeaderboardEntry(username="c", rank=3, total_trades=100, profit=3000, win_rate=0.70),
        ]

        ranked = scraper.rank_candidates(entries)

        # b should be first: 10000 * 0.60 = 6000
        assert ranked[0].username == "b"
