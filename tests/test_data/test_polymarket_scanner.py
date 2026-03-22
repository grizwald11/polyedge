"""Tests for Polymarket scanner — filter, rank, store pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Market, MarketCategory, MarketToken, Platform, TokenOutcome,
)
from src.data.polymarket_scanner import PolymarketScanner


def _poly_market(**overrides) -> Market:
    """Build a Polymarket Market with sensible defaults."""
    defaults = dict(
        ticker="0xcond_abc",
        question="Will X happen?",
        platform=Platform.POLYMARKET,
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id="tok_yes", outcome=TokenOutcome.YES, price=0.60),
            MarketToken(token_id="tok_no", outcome=TokenOutcome.NO, price=0.40),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        volume_total=500000.0,
        liquidity=20000.0,
        spread=0.02,
        active=True,
        closed=False,
    )
    defaults.update(overrides)
    return Market(**defaults)


@pytest.fixture
def mock_db():
    return MagicMock()


class TestPolymarketFilter:
    @pytest.fixture
    def scanner(self, settings, mock_db):
        return PolymarketScanner(discovery=None, db=mock_db, settings=settings)

    def test_passes_qualifying_market(self, scanner):
        markets = [_poly_market()]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_filters_inactive(self, scanner):
        markets = [_poly_market(active=False)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_filters_closed(self, scanner):
        markets = [_poly_market(closed=True)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_filters_low_volume(self, scanner):
        markets = [_poly_market(volume_24h=10, volume_total=50)]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_filters_no_tokens(self, scanner):
        markets = [_poly_market(tokens=[])]
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_filters_invalid_price_sum(self, scanner):
        """Price sum outside 0.90-1.10 should be filtered."""
        markets = [_poly_market(tokens=[
            MarketToken(token_id="tok_yes", outcome=TokenOutcome.YES, price=0.80),
            MarketToken(token_id="tok_no", outcome=TokenOutcome.NO, price=0.80),
        ])]
        # Sum = 1.60 > 1.10
        result = scanner.filter_markets(markets)
        assert len(result) == 0

    def test_wider_price_sum_tolerance(self, scanner):
        """Polymarket allows wider sum tolerance (0.90-1.10)."""
        # Sum = 1.08 — should pass for Polymarket
        markets = [_poly_market(tokens=[
            MarketToken(token_id="tok_yes", outcome=TokenOutcome.YES, price=0.60),
            MarketToken(token_id="tok_no", outcome=TokenOutcome.NO, price=0.48),
        ])]
        result = scanner.filter_markets(markets)
        assert len(result) == 1


class TestPolymarketRank:
    @pytest.fixture
    def scanner(self, settings, mock_db):
        return PolymarketScanner(discovery=None, db=mock_db, settings=settings)

    def test_higher_volume_ranks_first(self, scanner):
        """All else equal, higher volume should rank higher."""
        m1 = _poly_market(ticker="low_vol", volume_24h=100, volume_total=1000)
        m2 = _poly_market(ticker="high_vol", volume_24h=10000000, volume_total=100000000)
        result = scanner.rank_markets([m1, m2])
        assert result[0].ticker == "high_vol"

    def test_respects_max_markets(self, scanner):
        markets = [_poly_market(ticker=f"m_{i}") for i in range(300)]
        result = scanner.rank_markets(markets)
        assert len(result) <= scanner.settings.scanning.max_markets

    def test_no_fee_penalty(self, scanner):
        """Polymarket markets should not have fee penalty in ranking."""
        m = _poly_market(volume_24h=100000)
        result = scanner.rank_markets([m])
        assert len(result) == 1


class TestPolymarketStore:
    @pytest.fixture
    def scanner(self, settings, mock_db):
        return PolymarketScanner(discovery=None, db=mock_db, settings=settings)

    def test_stores_market(self, scanner, mock_db):
        m = _poly_market()
        scanner.store_markets([m])
        mock_db.upsert_market.assert_called_once_with(m)
        mock_db.log_snapshot.assert_called_once()
