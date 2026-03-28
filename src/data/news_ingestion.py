"""News ingestion — continuous monitoring layer for breaking news detection.

Polls RSS feeds and scores articles for relevance to active markets.
Built on top of NewsResearcher for search-based enrichment.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.core.models import Market

logger = logging.getLogger(__name__)

MAX_ARTICLE_AGE_SECONDS = 1800  # 30 minutes


@dataclass
class NewsItem:
    """A news article from an RSS feed."""
    title: str
    summary: str
    source: str
    url: str
    published: Optional[datetime] = None
    relevance_score: float = 0.0

    @property
    def age_seconds(self) -> float:
        if self.published is None:
            return float("inf")
        now = datetime.now(timezone.utc)
        pub = self.published
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        return max(0, (now - pub).total_seconds())


class NewsIngestion:
    """Polls RSS feeds and detects breaking news relevant to tracked markets."""

    def __init__(
        self,
        rss_feeds: Optional[list[str]] = None,
        max_article_age_minutes: int = 30,
        min_relevance: float = 0.3,
    ):
        self.rss_feeds = rss_feeds or [
            "https://feeds.reuters.com/reuters/topNews",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        ]
        self.max_article_age_seconds = max_article_age_minutes * 60
        self.min_relevance = min_relevance
        self._seen_urls: set[str] = set()
        self._max_seen_urls = 10000  # Cap to prevent unbounded memory growth

    async def poll_feeds(self) -> list[NewsItem]:
        """Poll all configured RSS feeds for new articles.

        Returns list of new (unseen) articles.
        """
        try:
            import feedparser
        except ImportError:
            logger.debug("feedparser not installed, skipping RSS polling")
            return []

        items: list[NewsItem] = []
        for feed_url in self.rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:10]:
                    url = entry.get("link", "")
                    if url in self._seen_urls:
                        continue
                    # Evict oldest entries when cap reached
                    if len(self._seen_urls) >= self._max_seen_urls:
                        # Remove ~20% of oldest (set is unordered, but clearing
                        # a chunk is sufficient to bound memory)
                        to_remove = list(self._seen_urls)[:self._max_seen_urls // 5]
                        self._seen_urls -= set(to_remove)
                    self._seen_urls.add(url)

                    published = self._parse_date(entry)
                    item = NewsItem(
                        title=entry.get("title", ""),
                        summary=entry.get("summary", ""),
                        source=self._extract_source(feed_url),
                        url=url,
                        published=published,
                    )
                    items.append(item)
            except Exception as e:
                logger.warning(f"RSS feed failed {feed_url}: {e}")

        return items

    def score_relevance(self, item: NewsItem, markets: list[Market]) -> tuple[float, Optional[str]]:
        """Score a news item's relevance to any active market.

        Returns (score, best_matching_market_id). Score 0-1.
        """
        best_score = 0.0
        best_market: Optional[str] = None

        item_words = set(re.findall(r"\w{3,}", (item.title + " " + item.summary).lower()))

        for market in markets:
            q_words = set(re.findall(r"\w{3,}", market.question.lower()))
            if not q_words:
                continue
            overlap = len(q_words & item_words) / len(q_words)
            if overlap > best_score:
                best_score = overlap
                best_market = market.ticker

        return best_score, best_market

    def is_breaking(self, item: NewsItem) -> bool:
        """Check if a news item qualifies as 'breaking' (recent + high relevance)."""
        return (
            item.age_seconds < self.max_article_age_seconds
            and item.relevance_score >= self.min_relevance
        )

    def filter_relevant(
        self, items: list[NewsItem], markets: list[Market]
    ) -> list[tuple[NewsItem, str]]:
        """Filter and score items for relevance to markets.

        Returns list of (item, market_id) tuples for breaking relevant items.
        """
        results: list[tuple[NewsItem, str]] = []

        for item in items:
            score, market_id = self.score_relevance(item, markets)
            item.relevance_score = score

            if self.is_breaking(item) and market_id:
                results.append((item, market_id))

        results.sort(key=lambda x: x[0].relevance_score, reverse=True)
        return results

    def _parse_date(self, entry) -> Optional[datetime]:
        """Parse published date from feed entry."""
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            try:
                import calendar
                ts = calendar.timegm(published)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception as e:
                logger.debug(f"Failed to parse published timestamp: {e}")
        return None

    def _extract_source(self, feed_url: str) -> str:
        """Extract source name from feed URL."""
        try:
            from urllib.parse import urlparse
            host = urlparse(feed_url).hostname or ""
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return feed_url
