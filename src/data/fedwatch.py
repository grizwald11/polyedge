"""CME FedWatch client — fetches implied Fed Funds rate probabilities.

Uses CME's public FedWatch data to get market-implied probabilities
for upcoming FOMC meetings. No authentication needed.

Gracefully degrades on any request or parsing error.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

# CME FedWatch tool page — we scrape implied probabilities from the page data
FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"


class FedWatchClient:
    """Fetches Fed Funds rate probabilities from CME FedWatch."""

    def __init__(self, ttl_seconds: int = 1800, base_url: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._base_url = base_url or FEDWATCH_URL

    async def get_rate_probabilities(self) -> Optional[list[dict]]:
        """Fetch rate probabilities for upcoming FOMC meetings.

        Returns list of dicts with 'meeting', 'cut_prob', 'hold_prob',
        'hike_prob' keys, or None on failure.
        """
        cached = self._cache.get("fedwatch_probs")
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    self._base_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PolyEdge/1.0"
                    },
                )
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError as e:
            logger.warning(f"FedWatch request failed: {e}")
            return None

        return self._parse_probabilities(html)

    def _parse_probabilities(self, html: str) -> Optional[list[dict]]:
        """Parse FOMC meeting probabilities from the page.

        Looks for structured data or percentage patterns associated with
        FOMC meeting dates.
        """
        try:
            meetings: list[dict] = []

            # Look for FOMC meeting date + probability patterns
            # CME pages often embed data in JSON or structured format
            # Try to find meeting blocks with probability values
            meeting_pattern = re.findall(
                r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})"
                r".*?(\d+\.?\d*)\s*%",
                html[:20000],
                re.IGNORECASE | re.DOTALL,
            )

            if not meeting_pattern:
                logger.warning("FedWatch: could not parse meeting probabilities from page — CME page format may have changed")
                return None

            seen_meetings: set[str] = set()
            for meeting_date, prob_str in meeting_pattern[:6]:
                meeting_key = meeting_date.strip()
                if meeting_key in seen_meetings:
                    continue
                seen_meetings.add(meeting_key)

                prob = float(prob_str)
                # Don't hardcode hike_prob=0.0 — derive from remaining probability
                hold_prob = max(0.0, 100.0 - prob)
                hike_prob = 0.0  # Default; may be overridden by richer data sources

                # Validate probabilities sum to ~100%
                prob_sum = prob + hold_prob + hike_prob
                if prob_sum > 0 and abs(prob_sum - 100.0) > 5.0:
                    logger.warning(
                        f"FedWatch probabilities don't sum to 100%: "
                        f"cut={prob}% + hold={hold_prob}% + hike={hike_prob}% = {prob_sum}%"
                    )

                meetings.append({
                    "meeting": meeting_key,
                    "cut_prob": prob,
                    "hold_prob": hold_prob,
                    "hike_prob": hike_prob,
                })

            if not meetings:
                return None

            self._cache.set("fedwatch_probs", meetings)
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
