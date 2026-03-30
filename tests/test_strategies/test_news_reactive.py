"""Tests for news-reactive strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    ForecastResult,
    Market,
    MarketCategory,
    MarketToken,
    StrategyName,
)
from src.data.news_ingestion import NewsItem
from src.strategies.news_reactive import NewsReactiveStrategy


def _make_market(ticker="FED-RATE", yes_price=0.34, no_price=0.66):
    return Market(
        ticker=ticker,
        question="Will the Fed cut rates in May 2026?",
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
    )


@pytest.fixture
def mock_forecaster():
    forecaster = MagicMock()
    forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
        probability=0.50,
        confidence_low=0.40,
        confidence_high=0.60,
        reasoning="News suggests higher probability",
    ))
    return forecaster


@pytest.fixture
def mock_news():
    news = MagicMock()
    news.poll_feeds = AsyncMock(return_value=[
        NewsItem(
            title="Fed signals rate cut",
            summary="Federal Reserve expected to cut rates in May",
            source="reuters.com",
            url="http://reuters.com/1",
            published=datetime.now(timezone.utc) - timedelta(minutes=5),
        ),
    ])
    news.filter_relevant = MagicMock(return_value=[
        (
            NewsItem(
                title="Fed signals rate cut",
                summary="Federal Reserve expected to cut rates in May",
                source="reuters.com",
                url="http://reuters.com/1",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
                relevance_score=0.6,
            ),
            "FED-RATE",
        ),
    ])
    return news


class TestNewsReactiveStrategy:
    @pytest.mark.asyncio
    async def test_generates_signal_on_edge(self, mock_forecaster, mock_news, tmp_db):
        settings = Settings()
        strategy = NewsReactiveStrategy(mock_forecaster, mock_news, settings, tmp_db)
        markets = [_make_market()]

        signals = await strategy.scan_for_opportunities(markets)

        assert len(signals) == 1
        assert signals[0].strategy == StrategyName.NEWS_REACTIVE
        assert signals[0].market_id == "FED-RATE"
        assert signals[0].edge > 0

    @pytest.mark.asyncio
    async def test_no_signal_below_min_edge(self, mock_forecaster, mock_news, tmp_db):
        # Forecaster returns probability close to market price (no edge)
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
            probability=0.35,
            reasoning="Minimal change",
        ))
        settings = Settings()
        strategy = NewsReactiveStrategy(mock_forecaster, mock_news, settings, tmp_db)

        signals = await strategy.scan_for_opportunities([_make_market()])

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_news_no_signals(self, mock_forecaster, tmp_db):
        news = MagicMock()
        news.poll_feeds = AsyncMock(return_value=[])
        settings = Settings()
        strategy = NewsReactiveStrategy(mock_forecaster, news, settings, tmp_db)

        signals = await strategy.scan_for_opportunities([_make_market()])

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_buy_no_direction(self, mock_forecaster, mock_news, tmp_db):
        # Forecaster returns probability lower than market → should buy NO
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
            probability=0.20,
            confidence_low=0.15,
            reasoning="News is bearish",
        ))
        settings = Settings()
        strategy = NewsReactiveStrategy(mock_forecaster, mock_news, settings, tmp_db)

        signals = await strategy.scan_for_opportunities([_make_market()])

        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_NO

    @pytest.mark.asyncio
    async def test_forecaster_failure_skips(self, mock_news, tmp_db):
        forecaster = MagicMock()
        forecaster.assess_market_with_prompt = AsyncMock(return_value=None)
        settings = Settings()
        strategy = NewsReactiveStrategy(forecaster, mock_news, settings, tmp_db)

        signals = await strategy.scan_for_opportunities([_make_market()])

        assert len(signals) == 0
