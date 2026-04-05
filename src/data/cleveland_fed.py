"""Cleveland Fed Inflation Nowcast — pre-release CPI estimates.

Scrapes the Cleveland Fed's inflation nowcasting page for real-time
CPI and Core CPI estimates. These are published before the official
BLS release, giving edge on CPI-related markets.

Gracefully degrades on any parsing error, with retry logic
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

CLEVELAND_FED_URL = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"

# Validation bounds for CPI values (year-over-year percentage)
MIN_CPI = -5.0   # Deflation floor (historically rare below -2%)
MAX_CPI = 50.0   # Hyperinflation ceiling


class ClevelandFedNowcast:
    """Fetches inflation nowcast data from the Cleveland Fed."""

    def __init__(self, ttl_seconds: int = 3600, base_url: str | None = None,
                 max_retries: int = 2):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._base_url = base_url or CLEVELAND_FED_URL
        self._max_retries = max_retries
        # Stale fallback: last successful result, served when fetch + parse fail
        self._last_good_result: Optional[dict] = None
        # H-9: Track consecutive scraping failures for observability.
        # This HTML scraper is fragile — if Cleveland Fed changes their page
        # format, parsing will silently fail. Consider FRED API
        # (fred.stlouisfed.org) as an alternative data source with stable
        # JSON responses.
        self._consecutive_failures: int = 0

    async def get_nowcast(self) -> Optional[dict]:
        """Fetch the latest inflation nowcast.

        Returns dict with 'cpi', 'core_cpi', and 'as_of' keys,
        or None on failure. Serves stale cache on transient failures.
        """
        cached = self._cache.get("cleveland_fed_nowcast")
        if cached is not None:
            logger.debug("Cleveland Fed: returning cached nowcast")
            return cached

        html = await self._fetch_page()
        if html is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"Cleveland Fed: {self._consecutive_failures} consecutive scraping failures — "
                    "site format may have changed. Consider FRED API as fallback."
                )
            if self._last_good_result is not None:
                logger.info("Cleveland Fed: serving stale cached result after fetch failure")
                return self._last_good_result
            return None

        result = self._parse_nowcast(html)
        if result is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    f"Cleveland Fed: {self._consecutive_failures} consecutive scraping failures — "
                    "site format may have changed. Consider FRED API as fallback."
                )
            if self._last_good_result is not None:
                logger.info("Cleveland Fed: parse failed, serving stale cached result")
                return self._last_good_result
            return None

        # Success — reset counter
        self._consecutive_failures = 0
        return result

    async def _fetch_page(self) -> Optional[str]:
        """Fetch the Cleveland Fed HTML page with retry logic."""
        try:
            return await retry_with_backoff(
                self._do_fetch,
                max_retries=self._max_retries,
                base_delay=2.0,
                max_delay=10.0,
                retryable_exceptions=(httpx.HTTPError, httpx.TimeoutException),
                on_retry=lambda attempt, e: logger.warning(
                    f"Cleveland Fed retry {attempt + 1}/{self._max_retries}: "
                    f"{type(e).__name__}: {e}"
                ),
            )
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(
                f"Cleveland Fed request failed after {self._max_retries + 1} attempts: {e}"
            )
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
            logger.debug(f"Cleveland Fed: fetched {len(response.text)} bytes")
            return response.text

    def _validate_cpi(self, value: float, label: str) -> Optional[float]:
        """Validate a CPI value is within plausible bounds."""
        if value < MIN_CPI or value > MAX_CPI:
            logger.warning(
                f"Cleveland Fed: {label} value {value}% outside plausible range "
                f"[{MIN_CPI}, {MAX_CPI}] — rejecting"
            )
            return None
        return value

    def _parse_nowcast(self, html: str) -> Optional[dict]:
        """Parse nowcast values from the HTML page.

        Looks for CPI and Core CPI nowcast percentages in the page content.
        Returns None if parsing fails (page structure changed, etc.).
        """
        try:
            # Look for CPI nowcast values — patterns like "X.XX percent" or "X.X%"
            cpi_match = re.search(
                r"(?:CPI|Consumer\s+Price\s+Index)\s*(?:Nowcast|nowcast|Forecast|forecast)"
                r"[:\s]*(\d+\.?\d*)\s*%",
                html,
                re.IGNORECASE,
            )

            core_match = re.search(
                r"(?:Core\s+CPI|Core\s+Consumer\s+Price)"
                r"[:\s]*(?:Nowcast|nowcast|Forecast|forecast)?"
                r"[:\s]*(\d+\.?\d*)\s*%",
                html,
                re.IGNORECASE,
            )

            # Also try to find values in structured data or table cells
            if not cpi_match:
                cpi_match = re.search(
                    r"CPI.*?(\d+\.\d+)\s*(?:%|percent)",
                    html[:5000],
                    re.IGNORECASE | re.DOTALL,
                )

            if not core_match:
                core_match = re.search(
                    r"Core.*?CPI.*?(\d+\.\d+)\s*(?:%|percent)",
                    html[:5000],
                    re.IGNORECASE | re.DOTALL,
                )

            # Extract "as of" date
            date_match = re.search(
                r"(?:as\s+of|updated|through)\s+(\w+\s+\d{1,2},?\s+\d{4})",
                html,
                re.IGNORECASE,
            )

            if not cpi_match and not core_match:
                logger.warning(
                    "Cleveland Fed: could not parse nowcast values from page — "
                    "site format may have changed"
                )
                return None

            cpi_val = float(cpi_match.group(1)) if cpi_match else None
            core_val = float(core_match.group(1)) if core_match else None

            # Validate extracted values
            if cpi_val is not None:
                cpi_val = self._validate_cpi(cpi_val, "CPI")
            if core_val is not None:
                core_val = self._validate_cpi(core_val, "Core CPI")

            if cpi_val is None and core_val is None:
                logger.warning("Cleveland Fed: all parsed values failed validation")
                return None

            result = {
                "cpi": cpi_val,
                "core_cpi": core_val,
                "as_of": date_match.group(1) if date_match else "unknown date",
            }

            self._cache.set("cleveland_fed_nowcast", result)
            self._last_good_result = result
            logger.info(
                f"Cleveland Fed nowcast: CPI={result['cpi']}, "
                f"Core CPI={result['core_cpi']}, as of {result['as_of']}"
            )
            return result

        except Exception as e:
            logger.warning(f"Cleveland Fed parsing failed: {e}")
            return None

    async def get_context(self) -> str:
        """Get formatted context string for Claude prompts.

        Returns empty string on failure.
        """
        data = await self.get_nowcast()
        if data is None:
            return ""

        lines = ["CLEVELAND FED INFLATION NOWCAST (pre-release estimate):"]

        if data.get("cpi") is not None:
            lines.append(f"- CPI Nowcast: {data['cpi']:.1f}% YoY (as of {data['as_of']})")
        if data.get("core_cpi") is not None:
            lines.append(f"- Core CPI Nowcast: {data['core_cpi']:.1f}% YoY")

        lines.append("Note: These are real-time estimates BEFORE official BLS release.")
        return "\n".join(lines)
