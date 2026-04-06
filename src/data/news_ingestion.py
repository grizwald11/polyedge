"""News ingestion — continuous monitoring layer for breaking news detection.

Polls RSS feeds and scores articles for relevance to active markets.
Built on top of NewsResearcher for search-based enrichment.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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
            logger.debug(f"NewsItem missing published timestamp: {self.title[:80]}")
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
        # L-3: These defaults are used when rss_feeds is not passed from config.
        # Prefer configuring via settings.yaml news.rss_feeds.
        self.rss_feeds = rss_feeds or [
            "https://feeds.reuters.com/reuters/topNews",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
        ]
        self.max_article_age_seconds = max_article_age_minutes * 60
        self.min_relevance = min_relevance
        import time as _time
        self._seen_urls: dict[str, float] = {}  # url -> timestamp
        self._max_seen_urls = 10000  # Cap to prevent unbounded memory growth
        self._seen_url_expiry_seconds = 86400  # 24h dedup window
        self._feed_failures: dict[str, int] = {}  # feed_url -> consecutive failure count
        self._feed_last_retry_cycle: dict[str, int] = {}  # feed_url -> cycle number of last retry attempt

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for deduplication by stripping tracking params and www prefix."""
        from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

        _TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                            "utm_content", "fbclid", "gclid", "gclsrc", "msclkid"}

        parsed = urlparse(url)

        # Strip www. prefix from hostname
        hostname = parsed.hostname or ""
        if hostname.startswith("www."):
            hostname = hostname[4:]

        # Filter out tracking query params
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(filtered, doseq=True)

        return urlunparse((
            parsed.scheme,
            hostname + (f":{parsed.port}" if parsed.port else ""),
            parsed.path,
            parsed.params,
            clean_query,
            "",  # drop fragment
        ))

    async def poll_feeds(self) -> list[NewsItem]:
        """Poll all configured RSS feeds for new articles.

        Returns list of new (unseen) articles.
        """
        try:
            import feedparser
        except ImportError:
            logger.warning(
                "feedparser not installed — RSS news feeds will be unavailable. "
                "Install with: pip install feedparser"
            )
            return []

        items: list[NewsItem] = []
        cycle_count = getattr(self, '_poll_cycle_count', 0)
        self._poll_cycle_count = cycle_count + 1

        for feed_url in self.rss_feeds:
            # Exponential backoff for failed feeds: wait 2^(failures-2) cycles before retrying.
            # e.g., 3 failures → wait 2 cycles, 4 failures → 4 cycles, 5+ failures → capped at 100.
            failed = self._feed_failures.get(feed_url, 0)
            if failed >= 3:
                wait_cycles = min(100, 2 ** (failed - 2))
                last_retry = self._feed_last_retry_cycle.get(feed_url, -wait_cycles)
                if cycle_count - last_retry < wait_cycles:
                    continue
                self._feed_last_retry_cycle[feed_url] = cycle_count

            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:10]:
                    url = entry.get("link", "")
                    import time
                    normalized = self._normalize_url(url)
                    now = time.time()
                    if normalized in self._seen_urls:
                        if now - self._seen_urls[normalized] < self._seen_url_expiry_seconds:
                            continue
                    # Periodic cleanup: remove expired entries when cap reached
                    if len(self._seen_urls) >= self._max_seen_urls:
                        cutoff = now - self._seen_url_expiry_seconds
                        expired = [u for u, ts in self._seen_urls.items() if ts < cutoff]
                        for u in expired:
                            del self._seen_urls[u]
                        # If still over cap after expiry cleanup, remove oldest
                        if len(self._seen_urls) >= self._max_seen_urls:
                            oldest = sorted(self._seen_urls.items(), key=lambda x: x[1])
                            for u, _ in oldest[:len(self._seen_urls) // 4]:
                                del self._seen_urls[u]
                    self._seen_urls[normalized] = now

                    published = self._parse_date(entry)
                    item = NewsItem(
                        title=entry.get("title", ""),
                        summary=entry.get("summary", ""),
                        source=self._extract_source(feed_url),
                        url=url,
                        published=published,
                    )
                    items.append(item)
                # Reset failure count on success
                self._feed_failures[feed_url] = 0
            except Exception as e:
                self._feed_failures[feed_url] = self._feed_failures.get(feed_url, 0) + 1
                logger.warning(
                    f"RSS feed failed {feed_url} "
                    f"(consecutive failures: {self._feed_failures[feed_url]}): {e}"
                )

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
        except Exception as e:
            logger.debug(f"Failed to extract source from {feed_url}: {e}")
            return feed_url
