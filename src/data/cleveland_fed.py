"""Cleveland Fed Inflation Nowcast — pre-release CPI estimates.

Scrapes the Cleveland Fed's inflation nowcasting page for real-time
CPI and Core CPI estimates. These are published before the official
BLS release, giving edge on CPI-related markets.

Gracefully degrades on any parsing error.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

CLEVELAND_FED_URL = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"


class ClevelandFedNowcast:
    """Fetches inflation nowcast data from the Cleveland Fed."""

    def __init__(self, ttl_seconds: int = 3600, base_url: str | None = None):
        self._cache = TTLCache(ttl_seconds=ttl_seconds)
        self._base_url = base_url or CLEVELAND_FED_URL

    async def get_nowcast(self) -> Optional[dict]:
        """Fetch the latest inflation nowcast.

        Returns dict with 'cpi', 'core_cpi', and 'as_of' keys,
        or None on failure.
        """
        cached = self._cache.get("cleveland_fed_nowcast")
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
            logger.warning(f"Cleveland Fed request failed: {e}")
            return None

        return self._parse_nowcast(html)

    def _parse_nowcast(self, html: str) -> Optional[dict]:
        """Parse nowcast values from the HTML page.

        Looks for CPI and Core CPI nowcast percentages in the page content.
        Returns None if parsing fails (page structure changed, etc.).
        """
        try:
            # Look for CPI nowcast values — patterns like "X.XX percent" or "X.X%"
            # The page typically contains text like "CPI Nowcast: 3.1%"
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
                # Try broader pattern: look for percentage values near "CPI" text
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
                logger.warning("Cleveland Fed: could not parse nowcast values from page — site format may have changed")
                return None

            result = {
                "cpi": float(cpi_match.group(1)) if cpi_match else None,
                "core_cpi": float(core_match.group(1)) if core_match else None,
                "as_of": date_match.group(1) if date_match else "unknown date",
            }

            self._cache.set("cleveland_fed_nowcast", result)
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
