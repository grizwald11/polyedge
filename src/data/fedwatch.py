"""CME FedWatch client — fetches implied Fed Funds rate probabilities.

Uses CME's public FedWatch data to get market-implied probabilities
for upcoming FOMC meetings. No authentication needed.

Gracefully degrades on any request or parsing error, with retry logic
and stale cache fallback.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.core.retry_helper import retry_with_backoff
from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

# CME FedWatch tool page — we scrape implied probabilities from the page data
FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

# Validation bounds
MIN_PROBABILITY = 0.0
MAX_PROBABILITY = 100.0
MAX_MEETINGS = 8  # Don't return more than 8 FOMC meetings


class FedWatchClient:
    """Fetches Fed Funds rate probabilities from CME FedWatch."""

    def __init__(self, ttl_seconds: int = 1800, base_url: str | None = None,
                 max_retries: int = 2):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._base_url = base_url or FEDWATCH_URL
        self._max_retries = max_retries
        # Stale fallback: last successful result, served when fetch + parse fail
        self._last_good_result: Optional[list[dict]] = None
        # H-9: Track consecutive scraping failures for observability.
        # This HTML scraper is fragile — if CME changes their page format,
        # parsing will silently fail. Consider FRED API (fred.stlouisfed.org)
        # as an alternative data source with stable JSON responses.
        self._consecutive_failures: int = 0

    async def get_rate_probabilities(self) -> Optional[list[dict]]:
        """Fetch rate probabilities for upcoming FOMC meetings.

        Returns list of dicts with 'meeting', 'cut_prob', 'hold_prob'
        keys, or None on failure. Serves stale cache on transient failures.
        """
        cached = self._cache.get("fedwatch_probs")
        if cached is not None:
            logger.debug("FedWatch: returning cached probabilities")
            return cached

        html = await self._fetch_page()
        if html is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"FedWatch: {self._consecutive_failures} consecutive scraping failures — "
                    "CME page format may have changed. Consider FRED API as fallback."
                )
            if self._last_good_result is not None:
                logger.info("FedWatch: serving stale cached result after fetch failure")
                return self._last_good_result
            return None

        result = self._parse_probabilities(html)
        if result is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"FedWatch: {self._consecutive_failures} consecutive scraping failures — "
                    "CME page format may have changed. Consider FRED API as fallback."
                )
            if self._last_good_result is not None:
                logger.info("FedWatch: parse failed, serving stale cached result")
                return self._last_good_result
            return None

        # Success — reset counter
        self._consecutive_failures = 0
        return result

    async def _fetch_page(self) -> Optional[str]:
        """Fetch the FedWatch HTML page with retry logic."""
        try:
            return await retry_with_backoff(
                self._do_fetch,
                max_retries=self._max_retries,
                base_delay=2.0,
                max_delay=10.0,
                retryable_exceptions=(httpx.HTTPError, httpx.TimeoutException),
                on_retry=lambda attempt, e: logger.warning(
                    f"FedWatch retry {attempt + 1}/{self._max_retries}: {type(e).__name__}: {e}"
                ),
            )
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"FedWatch request failed after {self._max_retries + 1} attempts: {e}")
            return None

    async def _do_fetch(self) -> str:
        """Single HTTP fetch attempt."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                self._base_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PolyEdge/1.0"
                },
            )
            response.raise_for_status()
            logger.debug(f"FedWatch: fetched {len(response.text)} bytes")
            return response.text

    def _parse_probabilities(self, html: str) -> Optional[list[dict]]:
        """Parse FOMC meeting probabilities from the page.

        Looks for structured data or percentage patterns associated with
        FOMC meeting dates. Validates all extracted values.
        """
        try:
            meetings: list[dict] = []

            # Look for FOMC meeting date + probability patterns
            # CME pages often embed data in JSON or structured format
            meeting_pattern = re.findall(
                r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})"
                r".*?(\d+\.?\d*)\s*%",
                html[:20000],
                re.IGNORECASE | re.DOTALL,
            )

            if not meeting_pattern:
                logger.warning(
                    "FedWatch: could not parse meeting probabilities from page — "
                    "CME page format may have changed"
                )
                return None

            seen_meetings: set[str] = set()
            for meeting_date, prob_str in meeting_pattern[:MAX_MEETINGS]:
                meeting_key = meeting_date.strip()
                if meeting_key in seen_meetings:
                    continue
                seen_meetings.add(meeting_key)

                prob = float(prob_str)

                # Validate probability is in [0, 100]
                if prob < MIN_PROBABILITY or prob > MAX_PROBABILITY:
                    logger.warning(
                        f"FedWatch: rejecting invalid probability {prob}% "
                        f"for {meeting_key} (outside [0, 100])"
                    )
                    continue

                hold_prob = max(0.0, 100.0 - prob)

                # Normalize probabilities to sum to exactly 100%
                prob_sum = prob + hold_prob
                if prob_sum > 0 and abs(prob_sum - 100.0) > 0.01:
                    logger.info(
                        f"FedWatch normalizing probabilities: "
                        f"cut={prob}% + hold={hold_prob}% = {prob_sum}% → 100%"
                    )
                    prob = (prob / prob_sum) * 100.0
                    hold_prob = (hold_prob / prob_sum) * 100.0

                meetings.append({
                    "meeting": meeting_key,
                    "cut_prob": round(prob, 2),
                    "hold_prob": round(hold_prob, 2),
                })

            if not meetings:
                logger.warning("FedWatch: no valid meetings after validation")
                return None

            self._cache.set("fedwatch_probs", meetings)
            self._last_good_result = meetings
            logger.info(
                f"FedWatch: parsed {len(meetings)} meetings — "
                f"{meetings[0]['meeting']}: {meetings[0]['cut_prob']:.0f}% cut"
            )
            return meetings

        except Exception as e:
            logger.warning(f"FedWatch parsing failed: {e}")
            return None

    async def get_context(self) -> str:
        """Get formatted context string for Claude prompts.

        Returns empty string on failure.
        """
        data = await self.get_rate_probabilities()
        if not data:
            return ""

        lines = ["FED FUNDS FUTURES (CME FedWatch implied probabilities):"]

        for meeting in data[:3]:
            cut = meeting["cut_prob"]
            hold = meeting["hold_prob"]
            hike = meeting.get("hike_prob", 0.0)
            line = f"- {meeting['meeting']} FOMC: {cut:.0f}% cut, {hold:.0f}% hold"
            if hike > 1.0:
                line += f", {hike:.0f}% hike"
            lines.append(line)

        return "\n".join(lines)
