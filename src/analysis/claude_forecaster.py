"""Claude forecaster — uses Claude API to assess market probabilities.

Calls Claude with structured prompts, parses JSON responses into
ForecastResult models. Selects model based on position size:
sonnet for routine, opus for high-stakes.
"""

from __future__ import annotations

import asyncio
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

    def _select_temperature(self, category: MarketCategory) -> float:
        """Select temperature based on market category, falling back to default."""
        return self.settings.claude.category_temperatures.get(
            category.value, self.settings.claude.temperature
        )

    def _validate_resolution_criteria(self, description: str) -> str:
        """Validate and enhance resolution criteria if missing or too short."""
        if not description or len(description.strip()) < 20:
            logger.warning("Resolution criteria missing or too short — adding caution")
            caution = (
                "WARNING: No detailed resolution criteria available for this market. "
                "Resolution rules may be ambiguous. Widen your confidence interval "
                "to account for possible resolution surprises."
            )
            if description and description.strip():
                return f"{description.strip()}\n\n{caution}"
            return caution
        return description

    async def assess_market(
        self,
        market: Market,
        news_context: str = "",
        position_value: float = 0.0,
        base_rate_context: str = "",
    ) -> ForecastResult:
        """Assess a market's true probability using Claude.

        Args:
            market: The market to assess
            news_context: Additional news/context to include
            position_value: Expected position size (determines model selection)
            base_rate_context: Historical base rate string for the category

        Returns:
            ForecastResult with probability estimate and reasoning
        """
        model = self._select_model(position_value)
        category = classify_market(market)
        temperature = self._select_temperature(category)

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

        resolution_criteria = self._validate_resolution_criteria(market.description)

        prompt = build_prompt(
            question=market.question,
            resolution_criteria=resolution_criteria,
            market_price=market.yes_price,
            close_date=close_date,
            category=category,
            news_context=news_context or "No additional context available.",
            base_rate_context=base_rate_context,
        )

        start_time = time.monotonic()
        try:
            client = self._get_client()
            response = await client.messages.create(
                model=model,
                max_tokens=self.settings.claude.max_tokens,
                temperature=temperature,
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
                f"{forecast.probability:.0%} (temp={temperature}, latency: {latency_ms}ms, tokens: {tokens_used})"
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
                parse_failed=True,  # Not a real assessment — don't trade on this
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
                parse_failed=True,  # Not a real assessment — don't trade on this
            )

    async def assess_market_with_prompt(
        self,
        market: Market,
        custom_prompt: str,
        position_value: float = 0.0,
    ) -> Optional[ForecastResult]:
        """Assess a market using a custom prompt (e.g., news impact, arb validation).

        Args:
            market: The market being assessed
            custom_prompt: Pre-built prompt string
            position_value: Expected position size (determines model selection)

        Returns:
            ForecastResult or None on failure
        """
        model = self._select_model(position_value)
        start_time = time.monotonic()

        try:
            client = self._get_client()
            response = await client.messages.create(
                model=model,
                max_tokens=self.settings.claude.max_tokens,
                temperature=self.settings.claude.temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": custom_prompt}],
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            raw_text = response.content[0].text
            tokens_used = response.usage.input_tokens + response.usage.output_tokens

            forecast = self._parse_response(raw_text)
            forecast.model_used = model
            forecast.tokens_used = tokens_used
            forecast.latency_ms = latency_ms
            forecast.raw_response = raw_text

            logger.info(
                f"Claude [{model}] custom assessment for '{market.question[:50]}...' → "
                f"{forecast.probability:.0%} (latency: {latency_ms}ms)"
            )
            return forecast

        except Exception as e:
            logger.error(f"Custom Claude assessment failed: {e}")
            return None

    async def cross_check_assess(
        self,
        market: Market,
        news_context: str = "",
        position_value: float = 0.0,
        base_rate_context: str = "",
    ) -> Optional[ForecastResult]:
        """Run dual-temperature cross-check on a market.

        Runs two concurrent Claude calls at different temperatures. If they
        agree (within threshold), returns the averaged result. If they disagree
        beyond the threshold, returns None (caller should skip the market).

        Falls back to single assessment on error.
        """
        model = self._select_model(position_value)
        category = classify_market(market)
        temp_low = self.settings.claude.cross_check_temp_low
        temp_high = self.settings.claude.cross_check_temp_high
        threshold = self.settings.claude.cross_check_disagreement_threshold

        # Enrich with news research
        if not news_context:
            news_context = await self.news_researcher.get_context(market.question)

        close_date = ""
        if market.end_date:
            close_date = market.end_date.strftime("%Y-%m-%d %H:%M UTC")

        resolution_criteria = self._validate_resolution_criteria(market.description)

        prompt = build_prompt(
            question=market.question,
            resolution_criteria=resolution_criteria,
            market_price=market.yes_price,
            close_date=close_date,
            category=category,
            news_context=news_context or "No additional context available.",
            base_rate_context=base_rate_context,
        )

        async def _call_at_temp(temp: float) -> ForecastResult:
            client = self._get_client()
            start = time.monotonic()
            response = await client.messages.create(
                model=model,
                max_tokens=self.settings.claude.max_tokens,
                temperature=temp,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            raw_text = response.content[0].text
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
            forecast = self._parse_response(raw_text)
            forecast.model_used = model
            forecast.tokens_used = tokens_used
            forecast.latency_ms = latency_ms
            forecast.raw_response = raw_text
            return forecast

        try:
            low_result, high_result = await asyncio.gather(
                _call_at_temp(temp_low),
                _call_at_temp(temp_high),
            )
        except Exception as e:
            logger.warning(f"Cross-check failed, falling back to single assess: {e}")
            return await self.assess_market(
                market, news_context, position_value, base_rate_context
            )

        disagreement = abs(low_result.probability - high_result.probability)
        avg_prob = (low_result.probability + high_result.probability) / 2.0

        if disagreement > threshold:
            logger.info(
                f"Cross-check DISAGREE on '{market.question[:50]}...' "
                f"(low={low_result.probability:.0%}, high={high_result.probability:.0%}, "
                f"gap={disagreement:.0%})"
            )
            return None

        logger.info(
            f"Cross-check AGREE on '{market.question[:50]}...' "
            f"(low={low_result.probability:.0%}, high={high_result.probability:.0%}, "
            f"avg={avg_prob:.0%})"
        )

        # Average the two results, widen CI slightly
        ci_low = min(low_result.confidence_low, high_result.confidence_low)
        ci_high = max(low_result.confidence_high, high_result.confidence_high)

        return ForecastResult(
            probability=max(0.01, min(0.99, avg_prob)),
            confidence_low=ci_low,
            confidence_high=ci_high,
            key_factors_for=low_result.key_factors_for,
            key_factors_against=low_result.key_factors_against,
            uncertainties=low_result.uncertainties + high_result.uncertainties,
            reasoning=f"Cross-check avg ({low_result.probability:.0%}/{high_result.probability:.0%}). {low_result.reasoning}",
            model_used=model,
            tokens_used=low_result.tokens_used + high_result.tokens_used,
            latency_ms=max(low_result.latency_ms, high_result.latency_ms),
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
        # Match various formats: "probability": 0.65, probability: 0.7, probability = 0.50, 65%
        prob_match = re.search(
            r'(?:probability|prob)["\'\s:=]+\s*([01]?\.\d+|0|1(?:\.0+)?)', text, re.IGNORECASE
        )
        if not prob_match:
            # Try percentage format: "probability: 65%" or "70%"
            pct_match = re.search(r'(?:probability|prob)["\'\s:=]+\s*(\d{1,3})%', text, re.IGNORECASE)
            if pct_match:
                prob = float(pct_match.group(1)) / 100.0
                logger.warning(f"Extracted probability {prob} from percentage in prose")
                return ForecastResult(
                    probability=max(0.01, min(0.99, prob)),
                    reasoning=f"Parsed probability from prose (%). Raw: {raw_text[:200]}",
                )
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
            parse_failed=True,
        )

    def _build_forecast(self, data: dict) -> ForecastResult:
        """Build a ForecastResult from parsed JSON data."""
        probability = float(data.get("probability", 0.5))
        # Clamp to valid range
        probability = max(0.01, min(0.99, probability))

        return ForecastResult(
            probability=probability,
            confidence_low=max(0.0, min(1.0, float(data.get("confidence_low", max(0, probability - 0.15))))),
            confidence_high=max(0.0, min(1.0, float(data.get("confidence_high", min(1, probability + 0.15))))),
            key_factors_for=data.get("key_factors_for", []),
            key_factors_against=data.get("key_factors_against", []),
            uncertainties=data.get("uncertainties", []),
            reasoning=data.get("reasoning", ""),
        )
