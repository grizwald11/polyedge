"""Cleveland Fed Inflation Nowcast — CPI estimates via FRED API.

Uses FRED API series for CPI and inflation data instead of fragile HTML
scraping of the Cleveland Fed's JavaScript-rendered pages. Provides
recent CPI data for Claude's macro/inflation assessments.

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

# CPI-related FRED series
CPI_SERIES = {
    "CPIAUCSL": "CPI All Urban Consumers (YoY)",
    "CPILFESL": "Core CPI (ex Food & Energy, YoY)",
    "PCEPILFE": "Core PCE (Fed's preferred, YoY)",
}

# Validation bounds for CPI values (year-over-year percentage)
MIN_CPI = -5.0   # Deflation floor
MAX_CPI = 50.0   # Hyperinflation ceiling


class ClevelandFedNowcast:
    """Fetches CPI/inflation data from FRED API.

    Replaces the previous HTML scraper which was fragile against
    the Cleveland Fed's JavaScript-rendered nowcasting page.
    """

    def __init__(self, ttl_seconds: int = 3600, api_key: str | None = None,
                 max_retries: int = 2, base_url: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._api_key = api_key
        self._base_url = base_url or FRED_BASE_URL
        self._max_retries = max_retries
        self._last_good_result: Optional[dict] = None
        self._consecutive_failures: int = 0

    async def get_nowcast(self) -> Optional[dict]:
        """Fetch latest CPI/inflation data from FRED.

        Returns dict with 'cpi', 'core_cpi', and 'as_of' keys,
        or None on failure. Serves stale cache on transient failures.
        """
        cached = self._cache.get("cleveland_fed_nowcast")
        if cached is not None:
            logger.debug("Cleveland Fed: returning cached data")
            return cached

        if not self._api_key:
            logger.debug("Cleveland Fed: no FRED_API_KEY configured, skipping")
            return self._last_good_result

        result = await self._fetch_cpi_data()
        if result is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"Cleveland Fed: {self._consecutive_failures} consecutive FRED API failures"
                )
            if self._last_good_result is not None:
                logger.info("Cleveland Fed: serving stale cached result after fetch failure")
                return self._last_good_result
            return None

        self._consecutive_failures = 0
        return result

    async def _fetch_cpi_data(self) -> Optional[dict]:
        """Fetch CPI data from FRED API."""
        try:
            values: dict[str, tuple[float, str]] = {}  # series_id -> (value, date)

            for series_id in CPI_SERIES:
                result = await self._fetch_series(series_id)
                if result is not None:
                    values[series_id] = result

            if not values:
                logger.warning("Cleveland Fed: no FRED CPI series returned data")
                return None

            cpi_data = values.get("CPIAUCSL")
            core_data = values.get("CPILFESL")

            result = {
                "cpi": cpi_data[0] if cpi_data else None,
                "core_cpi": core_data[0] if core_data else None,
                "as_of": cpi_data[1] if cpi_data else (core_data[1] if core_data else "unknown"),
            }

            # Also include Core PCE if available
            pce_data = values.get("PCEPILFE")
            if pce_data:
                result["core_pce"] = pce_data[0]

            self._cache.set("cleveland_fed_nowcast", result)
            self._last_good_result = result
            logger.info(
                f"Cleveland Fed: CPI={result['cpi']}, Core CPI={result['core_cpi']}, "
                f"as of {result['as_of']}"
            )
            return result

        except Exception as e:
            logger.warning(f"Cleveland Fed data fetch failed: {e}")
            return None

    async def _fetch_series(self, series_id: str) -> Optional[tuple[float, str]]:
        """Fetch the latest YoY percent change for a FRED series.

        Returns (value, date) tuple or None.
        """
        try:
            result = await retry_with_backoff(
                lambda sid=series_id: self._do_fetch_series(sid),
                max_retries=self._max_retries,
                base_delay=2.0,
                max_delay=10.0,
                retryable_exceptions=(httpx.HTTPError, httpx.TimeoutException),
            )
            return result
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"FRED series {series_id} fetch failed: {e}")
            return None

    async def _do_fetch_series(self, series_id: str) -> Optional[tuple[float, str]]:
        """Single fetch attempt for a FRED series (latest 2 observations for YoY)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                self._base_url,
                params={
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 13,  # ~13 months for YoY calculation
                    "units": "pc1",  # Percent change from year ago
                },
            )
            response.raise_for_status()
            data = response.json()

            observations = data.get("observations", [])
            if not observations:
                return None

            # Latest observation
            latest = observations[0]
            value_str = latest.get("value", "")
            date_str = latest.get("date", "unknown")

            if value_str == "." or not value_str:
                return None

            value = float(value_str)
            if value < MIN_CPI or value > MAX_CPI:
                logger.warning(
                    f"FRED {series_id}: rejecting YoY change {value}% "
                    f"outside [{MIN_CPI}, {MAX_CPI}]"
                )
                return None

            return (round(value, 2), date_str)

    async def get_context(self) -> str:
        """Get formatted context string for Claude prompts.

        Returns empty string on failure.
        """
        data = await self.get_nowcast()
        if data is None:
            return ""

        lines = ["INFLATION DATA (FRED, latest available):"]

        if data.get("cpi") is not None:
            lines.append(f"- CPI: {data['cpi']:.1f}% YoY (as of {data['as_of']})")
        if data.get("core_cpi") is not None:
            lines.append(f"- Core CPI (ex Food & Energy): {data['core_cpi']:.1f}% YoY")
        if data.get("core_pce") is not None:
            lines.append(f"- Core PCE (Fed's preferred): {data['core_pce']:.1f}% YoY")

        return "\n".join(lines)
