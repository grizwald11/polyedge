"""Resolution criteria analyzer — detects ambiguities before forecasting.

Parses market resolution criteria for edge cases, date boundaries,
definitional issues, and ambiguities. Injects analysis into the forecast
prompt so Claude is aware of resolution risks. Results are cached per
market since resolution criteria never change.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from src.core.models import Market

logger = logging.getLogger(__name__)

RESOLUTION_ANALYSIS_TEMPLATE = """RESOLUTION CRITERIA ANALYSIS

Analyze the resolution criteria for potential ambiguities, edge cases, and interpretation risks.

MARKET QUESTION: {question}
RESOLUTION CRITERIA: {resolution_criteria}
MARKET CLOSES: {close_date}

Identify:
1. AMBIGUITIES: Where the criteria could be interpreted in multiple ways
2. EDGE CASES: Scenarios where the outcome is unclear (e.g., partial fulfillment, technicalities)
3. DATE BOUNDARIES: Specific date/time constraints that could cause surprising resolutions
4. DEFINITIONAL ISSUES: Terms that could be interpreted differently from common understanding (e.g., "announced" vs "enacted", "above" vs "at or above")

RISK SCORE: Rate the overall resolution risk 0-100 where:
0 = unambiguous, crystal clear resolution
50 = some interpretation questions
100 = highly ambiguous, likely to cause disputes

CRITICAL: Respond ONLY with valid JSON.
{{"ambiguities": ["<string>", ...], "edge_cases": ["<string>", ...], "date_boundaries": ["<string>", ...], "definitional_issues": ["<string>", ...], "risk_score": <int 0-100>, "summary": "<one-sentence summary of key resolution risk>"}}"""


@dataclass
class ResolutionAnalysis:
    """Analysis of a market's resolution criteria."""
    ambiguities: list[str] = field(default_factory=list)
    edge_cases: list[str] = field(default_factory=list)
    date_boundaries: list[str] = field(default_factory=list)
    definitional_issues: list[str] = field(default_factory=list)
    risk_score: float = 0.0  # 0-1 normalized
    summary: str = ""

    @property
    def is_high_risk(self) -> bool:
        return self.risk_score > 0.8

    def format_for_prompt(self) -> str:
        """Format as a prompt injection block for the forecast prompt."""
        if not self.ambiguities and not self.edge_cases and not self.definitional_issues:
            return ""

        lines = ["RESOLUTION CRITERIA RISKS (review before estimating):"]
        if self.ambiguities:
            lines.append(f"- Ambiguities: {'; '.join(self.ambiguities[:3])}")
        if self.edge_cases:
            lines.append(f"- Edge cases: {'; '.join(self.edge_cases[:3])}")
        if self.date_boundaries:
            lines.append(f"- Date constraints: {'; '.join(self.date_boundaries[:2])}")
        if self.definitional_issues:
            lines.append(f"- Definitional issues: {'; '.join(self.definitional_issues[:2])}")
        if self.summary:
            lines.append(f"- Key risk: {self.summary}")
        return "\n".join(lines)


class ResolutionAnalyzer:
    """Analyzes resolution criteria for ambiguities using Claude."""

    def __init__(self, forecaster):
        """Initialize with a ClaudeForecaster instance for API calls."""
        self.forecaster = forecaster
        self._cache: dict[str, ResolutionAnalysis] = {}
        self._MAX_CACHE_SIZE = 500

    async def analyze(self, market: Market) -> ResolutionAnalysis:
        """Analyze a market's resolution criteria.

        Results are cached by market ticker since criteria don't change.
        Uses a single low-temperature Sonnet call.
        """
        cache_key = market.ticker
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Evict oldest entries if cache is full
        if len(self._cache) >= self._MAX_CACHE_SIZE:
            # Drop first half of cache (oldest insertions in dict order)
            keys = list(self._cache.keys())
            for k in keys[: len(keys) // 2]:
                del self._cache[k]
            logger.debug(f"Resolution cache evicted to {len(self._cache)} entries")

        # Don't analyze markets with no description
        if not market.description or len(market.description.strip()) < 20:
            result = ResolutionAnalysis(risk_score=0.3, summary="No detailed resolution criteria available")
            self._cache[cache_key] = result
            return result

        close_date = ""
        if market.end_date:
            close_date = market.end_date.strftime("%Y-%m-%d %H:%M UTC")

        prompt = RESOLUTION_ANALYSIS_TEMPLATE.format(
            question=market.question[:500],
            resolution_criteria=market.description[:2000],
            close_date=close_date or "Not specified",
        )

        try:
            forecast = await self.forecaster.assess_market_with_prompt(
                market=market,
                custom_prompt=prompt,
                position_value=0.0,  # Use Sonnet (cheapest model)
            )
            if forecast is None or getattr(forecast, "parse_failed", False):
                result = ResolutionAnalysis(risk_score=0.5, summary="Analysis failed")
                self._cache[cache_key] = result
                return result

            result = self._parse_analysis(forecast.raw_response or "")
            self._cache[cache_key] = result
            logger.info(
                f"Resolution analysis for {market.ticker}: "
                f"risk={result.risk_score:.0%}, {len(result.ambiguities)} ambiguities, "
                f"{len(result.edge_cases)} edge cases"
            )
            return result

        except Exception as e:
            logger.debug(f"Resolution analysis failed for {market.ticker}: {e}")
            result = ResolutionAnalysis(risk_score=0.5, summary=f"Analysis error: {e}")
            self._cache[cache_key] = result
            return result

    def _parse_analysis(self, raw_text: str) -> ResolutionAnalysis:
        """Parse Claude's JSON response into ResolutionAnalysis."""
        try:
            # Try direct JSON parse
            data = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            # Try extracting JSON from code blocks
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    return ResolutionAnalysis(risk_score=0.5, summary="Parse failed")
            else:
                # Try finding first { ... }
                start = raw_text.find('{')
                end = raw_text.rfind('}')
                if start >= 0 and end > start:
                    try:
                        data = json.loads(raw_text[start:end + 1])
                    except json.JSONDecodeError:
                        return ResolutionAnalysis(risk_score=0.5, summary="Parse failed")
                else:
                    return ResolutionAnalysis(risk_score=0.5, summary="Parse failed")

        risk_raw = data.get("risk_score", 50)
        risk_normalized = max(0.0, min(1.0, float(risk_raw) / 100.0))

        return ResolutionAnalysis(
            ambiguities=data.get("ambiguities", [])[:5],
            edge_cases=data.get("edge_cases", [])[:5],
            date_boundaries=data.get("date_boundaries", [])[:3],
            definitional_issues=data.get("definitional_issues", [])[:3],
            risk_score=risk_normalized,
            summary=data.get("summary", ""),
        )
