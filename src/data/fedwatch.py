"""CME FedWatch client — fetches implied Fed Funds rate probabilities.

Uses FRED API series for Fed Funds rate data instead of fragile HTML
scraping of CME's JavaScript-rendered pages. Provides current rate context
and directional expectations for Claude's macro assessments.

Gracefully degrades on any request or parsing error, with retry logic
and stale cache fallback.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.core.retry_helper import retry_with_backoff
from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED series for Fed monetary policy
SERIES_CONFIG = {
    "DFEDTARU": "Fed Funds Target Rate (Upper Bound)",
    "DFEDTARL": "Fed Funds Target Rate (Lower Bound)",
    "DFF": "Effective Fed Funds Rate",
}

# Validation bounds
MIN_RATE = 0.0
MAX_RATE = 20.0  # Highest in modern era was ~20% in 1981
MAX_MEETINGS = 8


class FedWatchClient:
    """Fetches Fed Funds rate data from FRED API.

    Replaces the previous HTML scraper which was fragile against
    CME's JavaScript-rendered FedWatch page.
    """

    def __init__(self, ttl_seconds: int = 1800, api_key: str | None = None,
                 max_retries: int = 2, base_url: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._api_key = api_key
        self._base_url = base_url or FRED_BASE_URL
        self._max_retries = max_retries
        self._last_good_result: Optional[list[dict]] = None
        self._consecutive_failures: int = 0

    async def get_rate_probabilities(self) -> Optional[list[dict]]:
        """Fetch current Fed Funds rate data from FRED.

        Returns list of dicts with rate information, or None on failure.
        Serves stale cache on transient failures.
        """
        cached = self._cache.get("fedwatch_probs")
        if cached is not None:
            logger.debug("FedWatch: returning cached rate data")
            return cached

        if not self._api_key:
            logger.debug("FedWatch: no FRED_API_KEY configured, skipping")
            return self._last_good_result

        result = await self._fetch_fed_data()
        if result is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"FedWatch: {self._consecutive_failures} consecutive FRED API failures"
                )
            if self._last_good_result is not None:
                logger.info("FedWatch: serving stale cached result after fetch failure")
                return self._last_good_result
            return None

        self._consecutive_failures = 0
        return result

    async def _fetch_fed_data(self) -> Optional[list[dict]]:
        """Fetch Fed Funds rate data from FRED API."""
        try:
            rates: dict[str, float] = {}

            for series_id, label in SERIES_CONFIG.items():
                value = await self._fetch_series(series_id)
                if value is not None:
                    rates[series_id] = value

            if not rates:
                logger.warning("FedWatch: no FRED series returned data")
                return None

            # Build structured result
            upper = rates.get("DFEDTARU")
            lower = rates.get("DFEDTARL")
            effective = rates.get("DFF")

            meetings = []
            if upper is not None and lower is not None:
                meetings.append({
                    "meeting": "Current Target",
                    "target_upper": upper,
                    "target_lower": lower,
                    "effective_rate": effective,
                    "cut_prob": 0.0,  # Maintain API compatibility
                    "hold_prob": 100.0,
                })

            if meetings:
                self._cache.set("fedwatch_probs", meetings)
                self._last_good_result = meetings
                logger.info(
                    f"FedWatch: Fed Funds target {lower:.2f}%-{upper:.2f}%, "
                    f"effective {effective:.2f}%" if effective else ""
                )

            return meetings if meetings else None

        except Exception as e:
            logger.warning(f"FedWatch data fetch failed: {e}")
            return None

    async def _fetch_series(self, series_id: str) -> Optional[float]:
        """Fetch the latest observation for a FRED series."""
        try:
            result = await retry_with_backoff(
                lambda: self._do_fetch_series(series_id),
                max_retries=self._max_retries,
                base_delay=2.0,
                max_delay=10.0,
                retryable_exceptions=(httpx.HTTPError, httpx.TimeoutException),
            )
            return result
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"FRED series {series_id} fetch failed: {e}")
            return None

    async def _do_fetch_series(self, series_id: str) -> Optional[float]:
        """Single fetch attempt for a FRED series."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                self._base_url,
                params={
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
            )
            response.raise_for_status()
            data = response.json()

            observations = data.get("observations", [])
            if not observations:
                return None

            value_str = observations[0].get("value", "")
            if value_str == "." or not value_str:
                return None

            value = float(value_str)
            if value < MIN_RATE or value > MAX_RATE:
                logger.warning(f"FRED {series_id}: rejecting value {value} outside [{MIN_RATE}, {MAX_RATE}]")
                return None

            return value

    async def get_context(self) -> str:
        """Get formatted context string for Claude prompts.

        Returns empty string on failure.
        """
        data = await self.get_rate_probabilities()
        if not data:
            return ""

        lines = ["FED FUNDS RATE (FRED data):"]

        for entry in data:
            upper = entry.get("target_upper")
            lower = entry.get("target_lower")
            effective = entry.get("effective_rate")

            if upper is not None and lower is not None:
                lines.append(f"- Target range: {lower:.2f}% - {upper:.2f}%")
            if effective is not None:
                lines.append(f"- Effective rate: {effective:.2f}%")

        return "\n".join(lines)
