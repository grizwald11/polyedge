"""Claude forecaster — uses Claude API to assess market probabilities.

Calls Claude with structured prompts, parses JSON responses into
ForecastResult models. Selects model based on position size:
sonnet for routine, opus for high-stakes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import anthropic

from src.config import Settings
from src.analysis.prompt_templates import SYSTEM_PROMPT, build_prompt
from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.core.models import Market, ForecastResult, MarketCategory

logger = logging.getLogger(__name__)


class ClaudeForecaster:
    """Calls Claude to assess market probabilities."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[anthropic.AsyncAnthropic] = None
        self.news_researcher = NewsResearcher(serper_api_key=settings.serper_api_key)

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            api_key = self.settings.anthropic_api_key
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    def _select_model(self, position_value: float = 0.0) -> str:
        """Select model based on position value."""
        if position_value > self.settings.claude.highstakes_threshold:
            return self.settings.claude.model_highstakes
        return self.settings.claude.model_primary

    async def assess_market(
        self,
        market: Market,
        news_context: str = "",
        position_value: float = 0.0,
    ) -> ForecastResult:
        """Assess a market's true probability using Claude.

        Args:
            market: The market to assess
            news_context: Additional news/context to include
            position_value: Expected position size (determines model selection)

        Returns:
            ForecastResult with probability estimate and reasoning
        """
        model = self._select_model(position_value)
        category = classify_market(market)

        # Enrich with news research if no context was provided
        if not news_context:
            news_context = await self.news_researcher.get_context(market.question)
            if news_context:
                logger.info(
                    f"News research found context for '{market.question[:50]}...'"
                )
            else:
                logger.debug(f"No news context for '{market.question[:50]}...'")

        # Build the prompt
        close_date = ""
        if market.end_date:
            close_date = market.end_date.strftime("%Y-%m-%d %H:%M UTC")

        prompt = build_prompt(
            question=market.question,
            resolution_criteria=market.description,
            market_price=market.yes_price,
            close_date=close_date,
            category=category,
            news_context=news_context or "No additional context available.",
        )

        start_time = time.monotonic()
        try:
            client = self._get_client()
            response = await client.messages.create(
                model=model,
                max_tokens=self.settings.claude.max_tokens,
                temperature=self.settings.claude.temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            raw_text = response.content[0].text
            tokens_used = response.usage.input_tokens + response.usage.output_tokens

            # Parse JSON response
            forecast = self._parse_response(raw_text)
            forecast.model_used = model
            forecast.tokens_used = tokens_used
            forecast.latency_ms = latency_ms
            forecast.raw_response = raw_text

            logger.info(
                f"Claude [{model}] assessed '{market.question[:50]}...' → "
                f"{forecast.probability:.0%} (latency: {latency_ms}ms, tokens: {tokens_used})"
            )
            return forecast

        except anthropic.RateLimitError:
            logger.warning("Claude API rate limited, returning market price as fallback")
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.15),
                confidence_high=min(1, market.yes_price + 0.15),
                reasoning="Rate limited — using market price as fallback",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as e:
            logger.error(f"Claude assessment failed: {e}")
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.20),
                confidence_high=min(1, market.yes_price + 0.20),
                reasoning=f"Assessment failed: {e}",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
            )

    def _parse_response(self, raw_text: str) -> ForecastResult:
        """Parse Claude's JSON response into a ForecastResult.

        Tries multiple strategies to extract JSON:
        1. Direct parse of the full response
        2. Extract from markdown code blocks (```json ... ```)
        3. Find first { and last } and parse that substring
        4. Fall back to 0.5 only as last resort
        """
        import re

        text = raw_text.strip()

        # Strategy 1: Direct parse
        try:
            data = json.loads(text)
            return self._build_forecast(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2: Extract from markdown code blocks
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if code_block_match:
            try:
                data = json.loads(code_block_match.group(1).strip())
                return self._build_forecast(data)
            except (json.JSONDecodeError, ValueError):
                pass

        # Strategy 3: Find first { and last } and try to parse
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                data = json.loads(text[first_brace:last_brace + 1])
                return self._build_forecast(data)
            except (json.JSONDecodeError, ValueError):
                pass

        # Strategy 4: Try to extract probability from prose as last resort
        prob_match = re.search(r"probability[\"'\s:]+\s*(0\.\d+)", text)
        if prob_match:
            prob = float(prob_match.group(1))
            logger.warning(f"Extracted probability {prob} from prose response")
            return ForecastResult(
                probability=max(0.01, min(0.99, prob)),
                reasoning=f"Parsed probability from prose. Raw: {raw_text[:200]}",
            )

        logger.warning(f"Failed to parse Claude response as JSON: {raw_text[:200]}")
        return ForecastResult(
            probability=0.5,
            reasoning=f"JSON parse failed, raw: {raw_text[:200]}",
        )

    def _build_forecast(self, data: dict) -> ForecastResult:
        """Build a ForecastResult from parsed JSON data."""
        probability = float(data.get("probability", 0.5))
        # Clamp to valid range
        probability = max(0.01, min(0.99, probability))

        return ForecastResult(
            probability=probability,
            confidence_low=float(data.get("confidence_low", max(0, probability - 0.15))),
            confidence_high=float(data.get("confidence_high", min(1, probability + 0.15))),
            key_factors_for=data.get("key_factors_for", []),
            key_factors_against=data.get("key_factors_against", []),
            uncertainties=data.get("uncertainties", []),
            reasoning=data.get("reasoning", ""),
        )
