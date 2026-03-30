"""FRED client — fetches economic data from the Federal Reserve (FRED API).

Provides latest values for key economic indicators (CPI, unemployment,
Fed Funds rate, etc.) to enrich Claude's probability assessments on
FED_MACRO and EARNINGS markets.

Gracefully degrades if no FRED_API_KEY is configured.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.data.cache import TTLCache

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# Key economic series and their display names
MACRO_SERIES = {
    "CPIAUCSL": "CPI (All Urban Consumers)",
    "UNRATE": "Unemployment Rate",
    "FEDFUNDS": "Fed Funds Rate",
    "PAYEMS": "Nonfarm Payrolls",
    "GDP": "GDP",
    "PCEPILFE": "Core PCE",
}

# Series that are reported as YoY percent change vs level
YOY_SERIES = {"CPIAUCSL", "GDP", "PCEPILFE"}
LEVEL_SERIES = {"UNRATE", "FEDFUNDS"}
DIFF_SERIES = {"PAYEMS"}  # Show month-over-month change in thousands


class FREDClient:
    """Fetches latest economic data from the FRED API."""

    def __init__(self, api_key: Optional[str] = None, ttl_seconds: int = 3600, base_url: Optional[str] = None):
        self.api_key = api_key
        self._base_url = base_url or FRED_BASE_URL
        self._cache = TTLCache(ttl_seconds=ttl_seconds)

    async def get_series_latest(self, series_id: str) -> Optional[dict]:
        """Fetch the most recent observations for a FRED series.

        Returns dict with 'value', 'date', and 'previous_value' keys,
        or None on failure.
        """
        if not self.api_key:
            return None

        cached = self._cache.get(f"fred_{series_id}")
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self._base_url,
                    params={
                        "series_id": series_id,
                        "api_key": self.api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": 3,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.warning(f"FRED API request failed for {series_id}: {e}")
            return None

        observations = data.get("observations", [])
        if not observations:
            return None

        # Filter out missing values (".")
        valid = [o for o in observations if o.get("value", ".") != "."]
        if not valid:
            return None

        latest = valid[0]
        previous = valid[1] if len(valid) > 1 else None

        result = {
            "value": float(latest["value"]),
            "date": latest["date"],
            "previous_value": float(previous["value"]) if previous else None,
        }
        self._cache.set(f"fred_{series_id}", result)
        return result

    async def get_macro_summary(self) -> str:
        """Fetch all key macro series and format a summary block.

        Returns formatted multi-line string, or empty string if no API key
        or all requests fail.
        """
        if not self.api_key:
            return ""

        lines: list[str] = []
        for series_id, name in MACRO_SERIES.items():
            data = await self.get_series_latest(series_id)
            if data is None:
                continue

            value = data["value"]
            date = data["date"]
            prev = data.get("previous_value")

            if series_id in YOY_SERIES:
                line = f"- {name}: {value:.1f}% YoY ({date})"
                if prev is not None:
                    direction = "up" if value > prev else "down" if value < prev else "flat"
                    line += f", {direction} from {prev:.1f}%"
            elif series_id in LEVEL_SERIES:
                line = f"- {name}: {value:.2f}% ({date})"
                if prev is not None:
                    if value == prev:
                        line += ", unchanged"
                    else:
                        direction = "up" if value > prev else "down"
                        line += f", {direction} from {prev:.2f}%"
            elif series_id in DIFF_SERIES:
                # Payrolls in thousands
                line = f"- {name}: {value:+,.0f}K ({date})"
                if prev is not None:
                    line += f", prev {prev:+,.0f}K"
            else:
                line = f"- {name}: {value:.2f} ({date})"

            lines.append(line)

        if not lines:
            return ""

        return "ECONOMIC DATA (FRED — Federal Reserve):\n" + "\n".join(lines)
