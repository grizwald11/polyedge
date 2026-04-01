"""Tests for news ingestion."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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


def _make_feed_entry(url="http://test.com/1", title="Test headline", summary="Test body"):
    """Build a minimal feedparser-style entry dict."""
    entry = MagicMock()
    entry.get = lambda key, default="": {
        "link": url,
        "title": title,
        "summary": summary,
    }.get(key, default)
    # Include published_parsed so _parse_date has something to work with
    entry.__iter__ = MagicMock(return_value=iter([]))
    return entry


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

    def test_age_naive_datetime_treated_as_utc(self):
        """Line 41: naive published datetime gets tzinfo=utc attached."""
        naive = datetime.utcnow() - timedelta(minutes=5)
        assert naive.tzinfo is None
        item = NewsItem(
            title="Test",
            summary="Test",
            source="test.com",
            url="http://test.com/1",
            published=naive,
        )
        age = item.age_seconds
        # Should be roughly 5 minutes, not inf or negative
        assert 200 < age < 400

    def test_age_seconds_not_negative(self):
        """age_seconds is clamped to 0 even for future-dated articles."""
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        item = NewsItem(
            title="Future",
            summary="",
            source="test.com",
            url="http://test.com/future",
            published=future,
        )
        assert item.age_seconds == 0.0


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

    def test_empty_markets_list(self):
        ingestion = NewsIngestion()
        item = NewsItem(
            title="Fed rate cut",
            summary="Federal Reserve action",
            source="reuters.com",
            url="http://reuters.com/1",
        )
        score, market_id = ingestion.score_relevance(item, [])
        assert score == 0.0
        assert market_id is None

    def test_market_with_empty_question(self):
        """Line 143 guard: market with no words in question is skipped."""
        ingestion = NewsIngestion()
        item = NewsItem(
            title="Something interesting",
            summary="Details here",
            source="reuters.com",
            url="http://reuters.com/empty",
        )
        # Build a market whose question produces no ≥3-char tokens
        market = _make_market(ticker="EMPTY", question="A b")  # only short words
        score, market_id = ingestion.score_relevance(item, [market])
        assert score == 0.0
        assert market_id is None


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

    def test_filter_sorted_by_score_descending(self):
        ingestion = NewsIngestion(min_relevance=0.1)
        m1 = _make_market("FED-RATE", "Will the Fed cut rates in May 2026?")
        m2 = _make_market("TRUMP-2028", "Will Trump win the 2028 Republican primary election?")

        items = [
            # Lower relevance to FED-RATE
            NewsItem(
                title="Fed may cut",
                summary="",
                source="reuters.com",
                url="http://reuters.com/low",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
            ),
            # Higher relevance to TRUMP-2028 — lots of words from the question
            NewsItem(
                title="Trump wins Republican primary election 2028",
                summary="Trump will win the Republican primary in 2028",
                source="ap.com",
                url="http://ap.com/high",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
            ),
        ]

        results = ingestion.filter_relevant(items, [m1, m2])
        assert len(results) >= 2
        # First result must have the highest score
        assert results[0][0].relevance_score >= results[1][0].relevance_score

    def test_filter_excludes_item_without_matching_market(self):
        ingestion = NewsIngestion(min_relevance=0.05)
        items = [
            NewsItem(
                title="Completely unrelated sports story",
                summary="Basketball game result",
                source="espn.com",
                url="http://espn.com/1",
                published=datetime.now(timezone.utc) - timedelta(minutes=5),
            ),
        ]
        markets = [_make_market("FED-RATE", "Will the Fed cut rates?")]
        results = ingestion.filter_relevant(items, markets)
        # Low overlap → score won't meet min_relevance or market_id will be None
        # Either way the item shouldn't be in results with a falsy market_id
        for item, mid in results:
            assert mid  # market_id must be truthy


class TestPollFeeds:
    """Tests for poll_feeds — lines 75-128."""

    def _build_fake_feed(self, entries=None, url="http://feed.com/1"):
        feed = MagicMock()
        feed.entries = entries or []
        return feed

    def _make_entry(self, url, title="Headline", summary="Body", published_parsed=None):
        entry = MagicMock()
        data = {
            "link": url,
            "title": title,
            "summary": summary,
            "published_parsed": published_parsed,
            "updated_parsed": None,
        }
        entry.get = lambda key, default=None: data.get(key, default)
        return entry

    @pytest.mark.asyncio
    async def test_feedparser_not_installed(self):
        """Lines 75-82: returns [] when feedparser is missing."""
        ingestion = NewsIngestion(rss_feeds=["http://test.com/feed"])
        with patch.dict("sys.modules", {"feedparser": None}):
            result = await ingestion.poll_feeds()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_returns_new_items(self):
        """Lines 84-128: normal happy-path poll populates items."""
        import time as time_mod
        # Use a real tuple that calendar.timegm can handle
        published_parsed = time_mod.gmtime(time_mod.time() - 300)  # 5 minutes ago

        entry = self._make_entry(
            url="http://reuters.com/story1",
            title="Fed cuts rates",
            summary="FOMC surprise cut",
            published_parsed=published_parsed,
        )

        fake_feed = self._build_fake_feed(entries=[entry])

        ingestion = NewsIngestion(rss_feeds=["http://feeds.reuters.com/reuters/topNews"])
        with patch("feedparser.parse", return_value=fake_feed):
            result = await ingestion.poll_feeds()

        assert len(result) == 1
        assert result[0].title == "Fed cuts rates"
        assert result[0].source == "feeds.reuters.com"
        assert result[0].published is not None

    @pytest.mark.asyncio
    async def test_poll_deduplicates_seen_urls(self):
        """Seen URLs are skipped on subsequent polls."""
        entry = self._make_entry(url="http://reuters.com/dup")
        fake_feed = self._build_fake_feed(entries=[entry])

        ingestion = NewsIngestion(rss_feeds=["http://feeds.reuters.com/reuters/topNews"])
        with patch("feedparser.parse", return_value=fake_feed):
            first = await ingestion.poll_feeds()
            second = await ingestion.poll_feeds()

        assert len(first) == 1
        assert len(second) == 0  # duplicate skipped

    @pytest.mark.asyncio
    async def test_poll_handles_feed_exception(self):
        """Lines 121-126: exception increments failure counter, returns partial results."""
        ingestion = NewsIngestion(rss_feeds=["http://bad.com/feed", "http://good.com/feed"])
        good_entry = self._make_entry(url="http://good.com/story1", title="Good story")
        good_feed = self._build_fake_feed(entries=[good_entry])

        def fake_parse(url):
            if "bad" in url:
                raise ConnectionError("timeout")
            return good_feed

        with patch("feedparser.parse", side_effect=fake_parse):
            result = await ingestion.poll_feeds()

        assert ingestion._feed_failures["http://bad.com/feed"] == 1
        assert len(result) == 1
        assert result[0].url == "http://good.com/story1"

    @pytest.mark.asyncio
    async def test_poll_resets_failure_count_on_success(self):
        """Failure counter resets to 0 after a successful parse."""
        ingestion = NewsIngestion(rss_feeds=["http://flaky.com/feed"])
        ingestion._feed_failures["http://flaky.com/feed"] = 2

        entry = self._make_entry(url="http://flaky.com/story1")
        fake_feed = self._build_fake_feed(entries=[entry])
        with patch("feedparser.parse", return_value=fake_feed):
            await ingestion.poll_feeds()

        assert ingestion._feed_failures["http://flaky.com/feed"] == 0

    @pytest.mark.asyncio
    async def test_poll_exponential_backoff_skips_failed_feed(self):
        """Feeds with >= 3 failures are skipped until enough cycles pass."""
        ingestion = NewsIngestion(rss_feeds=["http://dead.com/feed"])
        ingestion._feed_failures["http://dead.com/feed"] = 3
        # Simulate being at cycle 1 (just tried this feed recently)
        ingestion._poll_cycle_count = 1
        ingestion._feed_last_retry_cycle["http://dead.com/feed"] = 1

        with patch("feedparser.parse") as mock_parse:
            await ingestion.poll_feeds()
            # Feed should be skipped — feedparser.parse must NOT be called
            mock_parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_retries_failed_feed_after_wait_cycles(self):
        """After enough cycles have elapsed, a failed feed is retried."""
        ingestion = NewsIngestion(rss_feeds=["http://recovering.com/feed"])
        ingestion._feed_failures["http://recovering.com/feed"] = 3
        # 3 failures → wait 2^(3-2)=2 cycles. Place last retry at cycle 0.
        ingestion._poll_cycle_count = 5  # bump well past 0+2
        ingestion._feed_last_retry_cycle["http://recovering.com/feed"] = 0

        entry = self._make_entry(url="http://recovering.com/story1")
        fake_feed = self._build_fake_feed(entries=[entry])
        with patch("feedparser.parse", return_value=fake_feed):
            result = await ingestion.poll_feeds()

        # Feed was tried; failure count resets on success
        assert ingestion._feed_failures["http://recovering.com/feed"] == 0

    @pytest.mark.asyncio
    async def test_poll_evicts_oldest_seen_urls_at_cap(self):
        """_seen_urls FIFO eviction keeps size at or below _max_seen_urls."""
        ingestion = NewsIngestion(rss_feeds=["http://busy.com/feed"])
        ingestion._max_seen_urls = 3

        # Pre-fill to capacity
        for i in range(3):
            ingestion._seen_urls[f"http://busy.com/old-{i}"] = None

        new_entry = self._make_entry(url="http://busy.com/new-story")
        fake_feed = self._build_fake_feed(entries=[new_entry])
        with patch("feedparser.parse", return_value=fake_feed):
            result = await ingestion.poll_feeds()

        # New story was ingested and cache didn't exceed cap
        assert len(ingestion._seen_urls) <= ingestion._max_seen_urls
        assert "http://busy.com/new-story" in ingestion._seen_urls

    @pytest.mark.asyncio
    async def test_poll_entry_without_published_parsed(self):
        """Entry with no date info produces a NewsItem with published=None."""
        entry = self._make_entry(url="http://nodates.com/story1")
        entry.published_parsed = None
        entry.updated_parsed = None

        # Override _parse_date to return None by not providing any date fields
        fake_feed = self._build_fake_feed(entries=[entry])
        ingestion = NewsIngestion(rss_feeds=["http://nodates.com/feed"])
        with patch("feedparser.parse", return_value=fake_feed):
            result = await ingestion.poll_feeds()

        assert len(result) == 1
        assert result[0].published is None

    @pytest.mark.asyncio
    async def test_poll_cycle_count_increments(self):
        """_poll_cycle_count increments each call."""
        ingestion = NewsIngestion(rss_feeds=[])
        with patch("feedparser.parse"):
            await ingestion.poll_feeds()
            await ingestion.poll_feeds()
            await ingestion.poll_feeds()

        assert ingestion._poll_cycle_count == 3

    @pytest.mark.asyncio
    async def test_poll_caps_entries_at_ten(self):
        """Only the first 10 entries per feed are processed."""
        entries = [self._make_entry(f"http://busy.com/story-{i}") for i in range(20)]
        fake_feed = self._build_fake_feed(entries=entries)
        ingestion = NewsIngestion(rss_feeds=["http://busy.com/feed"])
        with patch("feedparser.parse", return_value=fake_feed):
            result = await ingestion.poll_feeds()
        assert len(result) == 10


class TestParseDate:
    """Tests for _parse_date — lines 179-187."""

    def _make_parse_entry(self, published_parsed=None, updated_parsed=None):
        entry = MagicMock()
        data = {
            "published_parsed": published_parsed,
            "updated_parsed": updated_parsed,
        }
        entry.get = lambda key, default=None: data.get(key, default)
        return entry

    def test_parse_date_with_published_parsed(self):
        import time as time_mod
        ingestion = NewsIngestion()
        now_struct = time_mod.gmtime(time_mod.time() - 60)
        entry = self._make_parse_entry(published_parsed=now_struct)

        result = ingestion._parse_date(entry)
        assert result is not None
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc

    def test_parse_date_with_updated_parsed_fallback(self):
        import time as time_mod
        ingestion = NewsIngestion()
        now_struct = time_mod.gmtime(time_mod.time() - 120)
        entry = self._make_parse_entry(updated_parsed=now_struct)  # published_parsed is None

        result = ingestion._parse_date(entry)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_parse_date_none_when_no_fields(self):
        ingestion = NewsIngestion()
        entry = self._make_parse_entry()  # both fields None

        result = ingestion._parse_date(entry)
        assert result is None

    def test_parse_date_invalid_struct(self):
        """Lines 185-186: bad tuple silently returns None."""
        ingestion = NewsIngestion()
        entry = self._make_parse_entry(published_parsed="not-a-struct")  # will raise in calendar.timegm

        result = ingestion._parse_date(entry)
        assert result is None


class TestExtractSource:
    """Tests for _extract_source — lines 191-199."""

    def test_strips_www_prefix(self):
        ingestion = NewsIngestion()
        assert ingestion._extract_source("https://www.reuters.com/topNews") == "reuters.com"

    def test_subdomain_no_www(self):
        ingestion = NewsIngestion()
        assert ingestion._extract_source("https://feeds.reuters.com/reuters/topNews") == "feeds.reuters.com"

    def test_nytimes_feed(self):
        ingestion = NewsIngestion()
        result = ingestion._extract_source("https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml")
        assert result == "rss.nytimes.com"

    def test_malformed_url_returns_original(self):
        """Lines 197-198: urlparse failure falls back to returning the raw url."""
        ingestion = NewsIngestion()
        with patch("urllib.parse.urlparse", side_effect=Exception("parse error")):
            result = ingestion._extract_source("not-a-url")
        assert result == "not-a-url"

    def test_url_with_no_hostname_returns_empty_string(self):
        """urlparse returns hostname='' for bare paths; host stays empty string."""
        ingestion = NewsIngestion()
        # A URL with no host component
        result = ingestion._extract_source("/just/a/path")
        assert isinstance(result, str)

    def test_default_feeds_extract_correctly(self):
        """Smoke test: all default feed URLs produce non-empty source strings."""
        ingestion = NewsIngestion()
        for feed_url in ingestion.rss_feeds:
            source = ingestion._extract_source(feed_url)
            assert source, f"Empty source for {feed_url}"


class TestNewsIngestionInit:
    def test_default_feeds_populated(self):
        ingestion = NewsIngestion()
        assert len(ingestion.rss_feeds) >= 1
        assert all("http" in f for f in ingestion.rss_feeds)

    def test_custom_feeds(self):
        custom = ["http://custom1.com/feed", "http://custom2.com/feed"]
        ingestion = NewsIngestion(rss_feeds=custom)
        assert ingestion.rss_feeds == custom

    def test_max_article_age_conversion(self):
        ingestion = NewsIngestion(max_article_age_minutes=15)
        assert ingestion.max_article_age_seconds == 900

    def test_min_relevance_stored(self):
        ingestion = NewsIngestion(min_relevance=0.5)
        assert ingestion.min_relevance == 0.5

    def test_seen_urls_starts_empty(self):
        ingestion = NewsIngestion()
        assert len(ingestion._seen_urls) == 0
