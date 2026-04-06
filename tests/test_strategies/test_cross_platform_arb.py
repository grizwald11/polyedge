"""Tests for cross-platform arbitrage strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Platform,
    Signal,
    StrategyName,
    TokenOutcome,
)
from src.strategies.cross_platform_arb import CrossPlatformArbStrategy


def _kalshi_market(ticker="KALSHI-FED", yes_price=0.40, no_price=0.60, **kw) -> Market:
    return Market(
        ticker=ticker,
        question="Will the Fed cut rates?",
        platform=Platform.KALSHI,
        category=MarketCategory.FED_MACRO,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome=TokenOutcome.YES, price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome=TokenOutcome.NO, price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000,
        active=True,
        **kw,
    )


def _poly_market(ticker="0xcond_fed", yes_price=0.50, no_price=0.50, **kw) -> Market:
    return Market(
        ticker=ticker,
        question="Will the Fed cut rates?",
        platform=Platform.POLYMARKET,
        category=MarketCategory.FED_MACRO,
        tokens=[
            MarketToken(token_id="tok_yes", outcome=TokenOutcome.YES, price=yes_price),
            MarketToken(token_id="tok_no", outcome=TokenOutcome.NO, price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=100000,
        active=True,
        **kw,
    )


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.trading.min_edge_arb = 0.03  # 3% min edge
    return settings


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_cross_platform_pairs.return_value = []
    return db


@pytest.fixture
def mock_cross_ref():
    ref = MagicMock()
    ref.get_best_match = AsyncMock(return_value=None)
    return ref


@pytest.fixture
def strategy(mock_settings, mock_db, mock_cross_ref):
    return CrossPlatformArbStrategy(mock_settings, mock_db, mock_cross_ref)


class TestPriceDiscrepancy:
    def test_kalshi_cheaper_generates_buy_kalshi(self, strategy):
        """When Kalshi YES < Poly YES, buy YES on Kalshi."""
        kalshi = _kalshi_market(yes_price=0.40)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.90, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].platform == Platform.KALSHI
        assert signals[0].direction == Direction.BUY_YES
        assert signals[0].edge == pytest.approx(0.10)

    def test_poly_cheaper_generates_buy_no_on_kalshi(self, strategy):
        """When Poly YES < Kalshi YES, buy NO on Kalshi (Polymarket is read-only)."""
        kalshi = _kalshi_market(yes_price=0.55)
        poly = _poly_market(yes_price=0.45)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.85, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].platform == Platform.KALSHI
        assert signals[0].direction == Direction.BUY_NO
        assert signals[0].market_id == kalshi.ticker
        assert signals[0].edge == pytest.approx(0.10)
        assert signals[0].market_price == pytest.approx(0.45)  # Kalshi NO price
        assert signals[0].probability_estimate == pytest.approx(0.55)  # Poly NO price

    def test_no_signal_below_min_edge(self, strategy):
        """No signal when edge is below threshold."""
        kalshi = _kalshi_market(yes_price=0.49)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.90, min_edge=0.03)
        assert len(signals) == 0

    def test_no_signal_on_zero_prices(self, strategy):
        kalshi = _kalshi_market(yes_price=0.0)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.90, min_edge=0.03)
        assert len(signals) == 0

    def test_confidence_capped_by_similarity(self, strategy):
        kalshi = _kalshi_market(yes_price=0.30)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.70, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].confidence == pytest.approx(0.70)

    def test_confidence_capped_at_0_9(self, strategy):
        kalshi = _kalshi_market(yes_price=0.30)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.99, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].confidence == pytest.approx(0.90)

    def test_signal_has_correct_strategy_name(self, strategy):
        kalshi = _kalshi_market(yes_price=0.40)
        poly = _poly_market(yes_price=0.50)
        signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.85, min_edge=0.03)
        assert signals[0].strategy == StrategyName.CROSS_PLATFORM_ARB

    def test_no_signal_ever_targets_polymarket(self, strategy):
        """All signals must target Kalshi — Polymarket is read-only."""
        cases = [
            (0.40, 0.55),  # Kalshi cheaper
            (0.55, 0.40),  # Poly cheaper
            (0.30, 0.50),  # Large edge, Kalshi cheaper
            (0.70, 0.50),  # Large edge, Poly cheaper
        ]
        for kalshi_yes, poly_yes in cases:
            kalshi = _kalshi_market(yes_price=kalshi_yes)
            poly = _poly_market(yes_price=poly_yes)
            signals = strategy._check_price_discrepancy(kalshi, poly, similarity=0.85, min_edge=0.03)
            for sig in signals:
                assert sig.platform == Platform.KALSHI, (
                    f"Signal targeted {sig.platform} instead of Kalshi "
                    f"for kalshi_yes={kalshi_yes}, poly_yes={poly_yes}"
                )


class TestPriceDiscrepancyFromRef:
    def test_kalshi_cheaper_from_ref(self, strategy):
        kalshi = _kalshi_market(yes_price=0.35)
        ref = {"yes_price": 0.50, "condition_id": "0xcond", "question": "Q"}
        signals = strategy._check_price_discrepancy_from_ref(kalshi, ref, similarity=0.80, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].platform == Platform.KALSHI
        assert signals[0].confidence == pytest.approx(0.80)  # Lower cap for ref data

    def test_poly_cheaper_from_ref_generates_buy_no_on_kalshi(self, strategy):
        """Ref-based: when Poly YES is cheaper, generate BUY_NO on Kalshi."""
        kalshi = _kalshi_market(yes_price=0.60)
        ref = {"yes_price": 0.40, "condition_id": "0xcond"}
        signals = strategy._check_price_discrepancy_from_ref(kalshi, ref, similarity=0.80, min_edge=0.03)
        assert len(signals) == 1
        assert signals[0].platform == Platform.KALSHI
        assert signals[0].direction == Direction.BUY_NO
        assert signals[0].market_id == kalshi.ticker
        assert signals[0].edge == pytest.approx(0.20)

    def test_no_signal_missing_ref_price(self, strategy):
        kalshi = _kalshi_market(yes_price=0.40)
        ref = {"condition_id": "0xcond"}
        signals = strategy._check_price_discrepancy_from_ref(kalshi, ref, similarity=0.80, min_edge=0.03)
        assert len(signals) == 0


class TestScanForOpportunities:
    @pytest.mark.asyncio
    async def test_cached_pair_generates_signal(self, strategy):
        """Pre-cached pair should check prices without discovery."""
        kalshi = _kalshi_market(ticker="K-1", yes_price=0.40)
        poly = _poly_market(ticker="0xpoly1", yes_price=0.55)
        strategy._pair_cache["K-1"] = {
            "condition_id": "0xpoly1",
            "question": "Q",
            "similarity": 0.85,
        }
        signals = await strategy.scan_for_opportunities([kalshi], [poly])
        assert len(signals) == 1
        assert signals[0].edge == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_discovery_new_pair(self, strategy, mock_cross_ref):
        """Uncached market triggers discovery via cross_ref."""
        kalshi = _kalshi_market(ticker="K-NEW", yes_price=0.40)
        poly = _poly_market(ticker="0xnew", yes_price=0.55)

        mock_cross_ref.get_best_match = AsyncMock(return_value={
            "condition_id": "0xnew",
            "question": "Will the Fed cut?",
            "similarity": 0.88,
            "yes_price": 0.55,
        })

        signals = await strategy.scan_for_opportunities([kalshi], [poly])
        assert len(signals) == 1
        # Verify pair was cached
        assert "K-NEW" in strategy._pair_cache
        # Verify DB was updated
        strategy.db.upsert_cross_platform_pair.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_match_no_signal(self, strategy, mock_cross_ref):
        """No cross_ref match = no signal."""
        kalshi = _kalshi_market(ticker="K-NONE")
        mock_cross_ref.get_best_match = AsyncMock(return_value=None)
        signals = await strategy.scan_for_opportunities([kalshi], [])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_discovery_limited_to_20(self, strategy, mock_cross_ref):
        """Only discover up to 20 new pairs per cycle."""
        markets = [_kalshi_market(ticker=f"K-{i}") for i in range(30)]
        mock_cross_ref.get_best_match = AsyncMock(return_value=None)
        await strategy.scan_for_opportunities(markets, [])
        assert mock_cross_ref.get_best_match.call_count == 20

    @pytest.mark.asyncio
    async def test_loads_cached_pairs_on_init(self, mock_settings, mock_cross_ref):
        db = MagicMock()
        db.get_cross_platform_pairs.return_value = [
            {
                "kalshi_ticker": "K-CACHED",
                "poly_condition_id": "0xcached",
                "poly_question": "Q",
                "similarity": 0.92,
            }
        ]
        strat = CrossPlatformArbStrategy(mock_settings, db, mock_cross_ref)
        assert "K-CACHED" in strat._pair_cache
        assert strat._pair_cache["K-CACHED"]["condition_id"] == "0xcached"

    @pytest.mark.asyncio
    async def test_cached_pair_below_similarity_threshold_skipped(self, strategy):
        """Cached pair with similarity < 0.55 should not generate signals."""
        kalshi = _kalshi_market(ticker="K-LOW", yes_price=0.30)
        poly = _poly_market(ticker="0xlow", yes_price=0.55)
        strategy._pair_cache["K-LOW"] = {
            "condition_id": "0xlow",
            "question": "Q",
            "similarity": 0.40,  # Below MIN_PAIR_SIMILARITY (0.55)
        }
        signals = await strategy.scan_for_opportunities([kalshi], [poly])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_discovery_below_similarity_threshold_skipped(self, strategy, mock_cross_ref):
        """Discovered pair with similarity < 0.55 should be skipped entirely."""
        kalshi = _kalshi_market(ticker="K-WEAK", yes_price=0.30)
        poly = _poly_market(ticker="0xweak", yes_price=0.55)

        mock_cross_ref.get_best_match = AsyncMock(return_value={
            "condition_id": "0xweak",
            "question": "Q",
            "similarity": 0.30,  # Below threshold
            "yes_price": 0.55,
        })

        signals = await strategy.scan_for_opportunities([kalshi], [poly])
        assert len(signals) == 0
        # Should NOT be cached
        assert "K-WEAK" not in strategy._pair_cache
        # Should NOT be saved to DB
        strategy.db.upsert_cross_platform_pair.assert_not_called()

    @pytest.mark.asyncio
    async def test_discovery_at_similarity_threshold_accepted(self, strategy, mock_cross_ref):
        """Pair at exactly MIN_PAIR_SIMILARITY (0.55) should be accepted."""
        kalshi = _kalshi_market(ticker="K-EDGE", yes_price=0.30)
        poly = _poly_market(ticker="0xedge", yes_price=0.55)

        mock_cross_ref.get_best_match = AsyncMock(return_value={
            "condition_id": "0xedge",
            "question": "Q",
            "similarity": 0.55,
            "yes_price": 0.55,
        })

        signals = await strategy.scan_for_opportunities([kalshi], [poly])
        assert len(signals) == 1
        assert "K-EDGE" in strategy._pair_cache
