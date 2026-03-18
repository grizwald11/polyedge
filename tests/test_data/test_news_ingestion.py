"""Tests for news ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Market, MarketCategory, MarketToken
from src.data.news_ingestion import NewsIngestion, NewsItem


def _make_market(ticker="FED-RATE", question="Will the Fed cut rates in May 2026?"):
    return Market(
        ticker=ticker,
        question=question,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=0.34),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
    )


class TestNewsItem:
    def test_age_seconds(self):
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
            published=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        assert 590 < item.age_seconds < 610

    def test_age_no_date(self):
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
        )
        assert item.age_seconds == float("inf")


class TestRelevanceScoring:
    def test_high_relevance(self):
        ingestion = NewsIngestion()
        item = NewsItem(
            title="Federal Reserve signals rate cut in May 2026",
            summary="The FOMC is expected to cut rates at the May meeting.",
            source="reuters.com",
            url="http://reuters.com/1",
        )
        market = _make_market()
        score, market_id = ingestion.score_relevance(item, [market])

        assert score > 0.3
        assert market_id == "FED-RATE"

    def test_low_relevance(self):
        ingestion = NewsIngestion()
        item = NewsItem(
            title="New iPhone released",
            summary="Apple launches new smartphone",
            source="techcrunch.com",
            url="http://techcrunch.com/1",
        )
        market = _make_market()
        score, market_id = ingestion.score_relevance(item, [market])

        assert score < 0.2

    def test_best_match(self):
        ingestion = NewsIngestion()
        item = NewsItem(
            title="Federal Reserve discusses rate cut",
            summary="Fed officials debating May rate decision",
            source="reuters.com",
            url="http://reuters.com/1",
        )
        m1 = _make_market("FED-RATE", "Will the Fed cut rates in May 2026?")
        m2 = _make_market("TRUMP", "Will Trump win the 2028 election?")

        score, market_id = ingestion.score_relevance(item, [m1, m2])
        assert market_id == "FED-RATE"


class TestBreakingDetection:
    def test_breaking_recent_relevant(self):
        ingestion = NewsIngestion(min_relevance=0.3)
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
            published=datetime.now(timezone.utc) - timedelta(minutes=5),
            relevance_score=0.5,
        )
        assert ingestion.is_breaking(item) is True

    def test_not_breaking_old(self):
        ingestion = NewsIngestion(min_relevance=0.3)
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
            published=datetime.now(timezone.utc) - timedelta(hours=2),
            relevance_score=0.5,
        )
        assert ingestion.is_breaking(item) is False

    def test_not_breaking_irrelevant(self):
        ingestion = NewsIngestion(min_relevance=0.3)
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
            published=datetime.now(timezone.utc) - timedelta(minutes=5),
            relevance_score=0.1,
        )
        assert ingestion.is_breaking(item) is False


class TestFilterRelevant:
    def test_filter_returns_breaking_items(self):
        ingestion = NewsIngestion(min_relevance=0.2)
        items = [
            NewsItem(
                title="Fed rate cut expected in May",
                summary="Federal Reserve officials signal rate cut",
                source="reuters.com",
                url="http://reuters.com/1",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
            ),
            NewsItem(
                title="New recipe for cookies",
                summary="Delicious chocolate chip cookies",
                source="foodnetwork.com",
                url="http://food.com/1",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
            ),
        ]
        markets = [_make_market()]

        results = ingestion.filter_relevant(items, markets)

        # Only the Fed article should match
        assert len(results) >= 1
        assert results[0][1] == "FED-RATE"
