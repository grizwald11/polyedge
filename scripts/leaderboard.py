"""Leaderboard scraper — discovers profitable traders for whale tracking.

Identifies high-performing traders from Kalshi leaderboard data.
Note: Kalshi doesn't expose public user positions, so whale tracking
relies on curated baskets and leaderboard rankings.

NOTE: This module currently has zero test coverage (audit finding L-8).
When adding features or fixing bugs here, please add corresponding tests
in tests/test_scripts/test_leaderboard.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LeaderboardEntry:
    """A trader from the leaderboard."""
    username: str
    rank: int
    total_trades: int = 0
    profit: float = 0.0
    win_rate: float = 0.0
    categories: list[str] = None

    def __post_init__(self):
        if self.categories is None:
            self.categories = []


class LeaderboardScraper:
    """Discovers high-performing traders from public leaderboard data."""

    def __init__(self, min_trades: int = 50, min_win_rate: float = 0.55):
        self.min_trades = min_trades
        self.min_win_rate = min_win_rate

    def filter_candidates(self, entries: list[LeaderboardEntry]) -> list[LeaderboardEntry]:
        """Filter leaderboard entries for whale-quality traders.

        Criteria:
        - At least min_trades total trades
        - Win rate above min_win_rate
        - Positive total profit
        """
        return [
            entry for entry in entries
            if (
                entry.total_trades >= self.min_trades
                and entry.win_rate >= self.min_win_rate
                and entry.profit > 0
            )
        ]

    def rank_candidates(self, entries: list[LeaderboardEntry]) -> list[LeaderboardEntry]:
        """Rank filtered candidates by profit-adjusted win rate."""
        scored = sorted(
            entries,
            key=lambda e: e.profit * e.win_rate,
            reverse=True,
        )
        return scored
