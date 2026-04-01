"""Tests for the Late Resolution strategy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Signal,
    StrategyName,
)
from src.strategies.late_resolution import (
    CONFIDENCE_BASE,
    MAX_HOURS_TO_RESOLUTION,
    MAX_MARKET_PRICE,
    MIN_EVIDENCE_PROBABILITY,
    MIN_VOLUME,
    MAX_CONCURRENT,
    LateResolutionStrategy,
    _count_evidence_signals,
)


# ── Helpers ──────────────────────────────────────


def _make_market(
    *,
    ticker: str = "LATE-TEST",
    question: str = "Will the Senate vote pass today?",
    yes_price: float = 0.65,
    no_price: float = 0.35,
    hours_to_resolution: float = 3.0,
    volume_24h: float = 50_000.0,
    category: MarketCategory = MarketCategory.POLITICS,
) -> Market:
    """Build a Market with convenient defaults for late-resolution testing."""
    end_date = datetime.now(timezone.utc) + timedelta(hours=hours_to_resolution)
    return Market(
        ticker=ticker,
        question=question,
        category=category,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
        end_date=end_date,
        volume_24h=volume_24h,
        active=True,
    )


def _make_strategy(news_context: str = "", news_raises: bool = False):
    """Build a LateResolutionStrategy with a mocked news researcher."""
    settings = MagicMock()
    db = MagicMock()
    researcher = AsyncMock()

    if news_raises:
        researcher.get_context = AsyncMock(side_effect=RuntimeError("search failed"))
    else:
        researcher.get_context = AsyncMock(return_value=news_context)

    strategy = LateResolutionStrategy(settings=settings, db=db, news_researcher=researcher)
    return strategy


# ── Tests ────────────────────────────────────────


class TestFilterCandidates:
    """Tests for the _filter_candidates pre-filter."""

    def test_passes_qualifying_market(self):
        strategy = _make_strategy()
        market = _make_market(hours_to_resolution=2.0, yes_price=0.60, volume_24h=20_000)
        result = strategy._filter_candidates([market])
        assert len(result) == 1

    def test_rejects_resolution_too_far(self):
        """Markets resolving beyond MAX_HOURS_TO_RESOLUTION are excluded."""
        strategy = _make_strategy()
        market = _make_market(hours_to_resolution=12.0)
        result = strategy._filter_candidates([market])
        assert len(result) == 0

    def test_rejects_no_end_date(self):
        """Markets without an end_date are excluded."""
        strategy = _make_strategy()
        market = _make_market()
        market.end_date = None
        result = strategy._filter_candidates([market])
        assert len(result) == 0

    def test_rejects_yes_above_max_price(self):
        """Markets where YES is already >= 80% are excluded."""
        strategy = _make_strategy()
        market = _make_market(yes_price=0.85, no_price=0.15)
        result = strategy._filter_candidates([market])
        assert len(result) == 0

    def test_rejects_no_above_max_price(self):
        """Markets where NO is already >= 80% are excluded."""
        strategy = _make_strategy()
        market = _make_market(yes_price=0.15, no_price=0.85)
        result = strategy._filter_candidates([market])
        assert len(result) == 0

    def test_rejects_low_volume(self):
        """Markets below MIN_VOLUME are excluded."""
        strategy = _make_strategy()
        market = _make_market(volume_24h=5_000)
        result = strategy._filter_candidates([market])
        assert len(result) == 0


class TestSignalGeneration:
    """Tests for full signal generation pipeline."""

    @pytest.mark.asyncio
    async def test_generates_buy_yes_signal(self):
        """Strong YES evidence + uncertain market => BUY_YES signal."""
        news = (
            "The Senate vote has been confirmed. The bill was officially approved "
            "and signed by the president. It has been announced that the legislation passed."
        )
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.60, no_price=0.40)

        signals = await strategy.generate_signals([market])

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == Direction.BUY_YES
        assert sig.strategy == StrategyName.LATE_RESOLUTION
        assert sig.market_id == market.ticker
        assert sig.edge > 0
        assert sig.probability_estimate >= MIN_EVIDENCE_PROBABILITY
        assert sig.market_price == 0.60

    @pytest.mark.asyncio
    async def test_generates_buy_no_signal(self):
        """Strong NO evidence + uncertain market => BUY_NO signal."""
        news = (
            "The proposal was rejected by the committee. Officials denied the request. "
            "The measure was defeated and blocked from reaching the floor."
        )
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.55, no_price=0.45)

        signals = await strategy.generate_signals([market])

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == Direction.BUY_NO
        assert sig.edge > 0
        assert sig.probability_estimate >= MIN_EVIDENCE_PROBABILITY

    @pytest.mark.asyncio
    async def test_no_signal_when_market_already_priced(self):
        """If market already reflects the outcome (>80%), no signal."""
        news = "The vote was confirmed and approved officially."
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.85, no_price=0.15)

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_resolution_too_far(self):
        """Markets resolving in >6 hours should produce no signals."""
        news = "The vote was confirmed and approved officially."
        strategy = _make_strategy(news_context=news)
        market = _make_market(hours_to_resolution=24.0)

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_volume_too_low(self):
        """Low-volume markets should produce no signals."""
        news = "The vote was confirmed and approved officially."
        strategy = _make_strategy(news_context=news)
        market = _make_market(volume_24h=2_000)

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_evidence_ambiguous(self):
        """Ambiguous evidence (mixed signals) should produce no signal."""
        news = "Talks are ongoing. Some officials support the measure, others oppose it."
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.50, no_price=0.50)

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_no_news_context(self):
        """Without news context, no signal should be generated."""
        strategy = _make_strategy(news_context="")
        market = _make_market()

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_without_news_researcher(self):
        """If no news_researcher is provided, markets should still be skipped gracefully."""
        settings = MagicMock()
        db = MagicMock()
        strategy = LateResolutionStrategy(settings=settings, db=db, news_researcher=None)
        market = _make_market()

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_multiple_markets_scanned(self):
        """Multiple qualifying markets should each be assessed."""
        news = (
            "The measure was confirmed and has been officially approved. "
            "The legislation passed and was signed."
        )
        strategy = _make_strategy(news_context=news)
        markets = [
            _make_market(ticker="MKT-1", yes_price=0.60, hours_to_resolution=2.0),
            _make_market(ticker="MKT-2", yes_price=0.70, hours_to_resolution=1.0),
            _make_market(ticker="MKT-3", yes_price=0.50, hours_to_resolution=5.0),
        ]

        signals = await strategy.generate_signals(markets)

        # All three are valid candidates; signals depend on evidence strength
        assert len(signals) >= 1
        tickers = {s.market_id for s in signals}
        # All signals should reference one of our markets
        assert tickers.issubset({"MKT-1", "MKT-2", "MKT-3"})

    @pytest.mark.asyncio
    async def test_max_concurrent_caps_assessments(self):
        """Only MAX_CONCURRENT markets should be assessed even if more qualify."""
        news = "The vote was confirmed and approved officially and has been signed."
        strategy = _make_strategy(news_context=news)
        markets = [
            _make_market(ticker=f"MKT-{i}", yes_price=0.60, hours_to_resolution=2.0)
            for i in range(10)
        ]

        signals = await strategy.generate_signals(markets)

        # The news researcher should only be called MAX_CONCURRENT times
        assert strategy.news_researcher.get_context.call_count == MAX_CONCURRENT

    @pytest.mark.asyncio
    async def test_news_researcher_error_handled_gracefully(self):
        """If news researcher raises, the market is skipped without crashing."""
        strategy = _make_strategy(news_raises=True)
        market = _make_market()

        signals = await strategy.generate_signals([market])
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_confidence_within_bounds(self):
        """Signal confidence should be between CONFIDENCE_BASE and 0.95."""
        news = (
            "Confirmed. Officially approved. Passed. Signed. Announced. "
            "Agreed. Completed. Succeeded. Enacted. Ratified."
        )
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.50)

        signals = await strategy.generate_signals([market])
        if signals:
            for sig in signals:
                assert CONFIDENCE_BASE <= sig.confidence <= 0.95

    @pytest.mark.asyncio
    async def test_edge_calculation_correctness(self):
        """Edge should equal probability_estimate - market_price."""
        news = (
            "The vote was officially confirmed. It was approved and has been signed. "
            "The measure passed and was announced to the public."
        )
        strategy = _make_strategy(news_context=news)
        market = _make_market(yes_price=0.55, no_price=0.45)

        signals = await strategy.generate_signals([market])
        for sig in signals:
            expected_edge = round(sig.probability_estimate - sig.market_price, 4)
            assert sig.edge == expected_edge


class TestEvidenceCounting:
    """Tests for the _count_evidence_signals helper."""

    def test_positive_keywords_counted(self):
        text = "the bill was confirmed and officially approved"
        assert _count_evidence_signals(text, positive=True) >= 2

    def test_negative_keywords_counted(self):
        text = "the bill was rejected and defeated"
        assert _count_evidence_signals(text, positive=False) >= 2

    def test_no_keywords_returns_zero(self):
        text = "no relevant information here at all"
        assert _count_evidence_signals(text, positive=True) == 0

    def test_mixed_text_counts_only_requested_polarity(self):
        text = "the bill was confirmed but later rejected"
        pos = _count_evidence_signals(text, positive=True)
        neg = _count_evidence_signals(text, positive=False)
        assert pos >= 1
        assert neg >= 1
