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

from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.analysis.prompt_templates import SYSTEM_PROMPT, build_prompt
from src.config import Settings
from src.core.models import ForecastResult, Market, MarketCategory

logger = logging.getLogger(__name__)


class ClaudeForecaster:
    """Calls Claude to assess market probabilities."""

    # Cache TTL in seconds — avoid re-assessing same market within 5 minutes
    _CACHE_TTL_SECONDS = 300

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[anthropic.AsyncAnthropic] = None
        self.news_researcher = NewsResearcher(
            serper_api_key=settings.serper_api_key,
            searxng_url=settings.searxng_url,
            serper_url=settings.news.serper_url,
            staleness_thresholds=settings.news.staleness_thresholds,
        )
        # Token and cost tracking for budget awareness
        self._total_tokens_today: int = 0
        self._total_cost_today: float = 0.0  # Estimated USD cost
        self._today_date: str = ""
        self._call_count_today: int = 0  # L-5: Track call count for avg token estimate
        # Per-million-token pricing (input/output) by model family.
        # Defaults as of March 2026 — verify at https://www.anthropic.com/pricing
        # and override via settings.claude.model_pricing if prices change.
        self._cost_per_million: dict[str, tuple[float, float]] = {
            "claude-sonnet-4-6": (3.0, 15.0),
            "claude-opus-4-6": (15.0, 60.0),
        }
        # Override from settings if configured
        if hasattr(settings.claude, 'model_pricing') and settings.claude.model_pricing:
            self._cost_per_million.update(settings.claude.model_pricing)
        # Forecast cache: avoids duplicate Claude calls for same market in a cycle
        from src.data.cache import TTLCache
        self._forecast_cache = TTLCache(ttl_seconds=self._CACHE_TTL_SECONDS)
        # Circuit breaker: disable API calls after repeated consecutive failures
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0  # monotonic time; 0 = circuit closed

    async def close(self) -> None:
        """Close the underlying Anthropic client, releasing connections."""
        if self._client is not None:
            await self._client.close()
            self._client = None

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

    async def health_check(self) -> None:
        """Validate API key with a minimal Claude call.

        Raises on failure so callers can log/warn appropriately.
        """
        client = self._get_client()
        response = await client.messages.create(
            model=self.settings.claude.model_primary,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        if not response.content:
            raise RuntimeError("Empty response from Claude API health check")

    def _select_temperature(self, category: MarketCategory) -> float:
        """Select temperature based on market category, falling back to default."""
        temp = self.settings.claude.category_temperatures.get(category.value)
        if temp is None:
            logger.debug(f"Using default temperature for unmapped category {category.value}")
            return self.settings.claude.temperature
        return temp

    def _estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """Estimate USD cost for a Claude API call."""
        prices = self._cost_per_million.get(model, (3.0, 15.0))
        return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000

    def _track_tokens(self, tokens: int, input_tokens: int = 0, output_tokens: int = 0, model: str = ""):
        """Track daily token usage for cost awareness."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._today_date:
            if self._today_date and self._total_tokens_today > 0:
                # L-5: Log actual avg tokens per call for budget estimate refinement
                avg_tokens = (
                    self._total_tokens_today / self._call_count_today
                    if self._call_count_today > 0 else 0
                )
                logger.info(
                    f"Claude API usage yesterday: {self._total_tokens_today:,} tokens "
                    f"across {self._call_count_today} calls "
                    f"(avg {avg_tokens:,.0f} tokens/call), ~${self._total_cost_today:.2f}"
                )
            self._today_date = today
            self._total_tokens_today = 0
            self._total_cost_today = 0.0
            self._call_count_today = 0
        self._total_tokens_today += tokens
        self._call_count_today += 1
        if input_tokens or output_tokens:
            self._total_cost_today += self._estimate_cost(input_tokens, output_tokens, model)
        budget = self.settings.claude.daily_token_budget
        if self._total_tokens_today > budget:
            logger.warning(
                f"Claude API daily token usage ({self._total_tokens_today:,}) "
                f"exceeds soft limit ({budget:,})"
            )

    def is_budget_exceeded(self) -> bool:
        """Check if the hard daily token budget has been exceeded.

        The hard limit is 2x the configured soft budget. When exceeded,
        no further API calls should be made until the next day.
        """
        hard_limit = self.settings.claude.daily_token_budget * 2
        if self._total_tokens_today > hard_limit:
            logger.critical(
                f"Claude API hard budget exceeded: {self._total_tokens_today:,} tokens "
                f"> {hard_limit:,} hard limit — refusing further API calls today"
            )
            return True
        return False

    def _record_api_failure(self):
        """Track consecutive API failures for the circuit breaker.

        After 3 consecutive failures, opens the circuit for 5 minutes to prevent
        hammering a failing API and burning budget on retry storms.
        """
        import time as _time
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            open_duration = 300.0  # 5 minutes
            self._circuit_open_until = _time.monotonic() + open_duration
            logger.error(
                f"Claude API circuit breaker OPENED after {self._consecutive_failures} "
                f"consecutive failures — disabling calls for {open_duration:.0f}s"
            )

    @staticmethod
    def _extract_text(response) -> str | None:
        """Safely extract text from Claude API response. Returns None if empty."""
        if not response.content:
            return None
        return response.content[0].text

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

    def _check_preconditions(
        self, market: Market,
    ) -> Optional[ForecastResult]:
        """Check circuit breaker, budget limits, and cache before calling Claude.

        Returns a ForecastResult if an early return is needed (circuit open,
        budget exceeded, or cache hit). Returns None if the caller should
        proceed with the API call.
        """
        # Circuit breaker check — disable calls after 3 consecutive failures
        import time as _time
        now_mono = _time.monotonic()
        if self._circuit_open_until > now_mono:
            remaining = self._circuit_open_until - now_mono
            logger.warning(
                f"Claude API circuit breaker open — skipping call for "
                f"'{market.question[:50]}...' ({remaining:.0f}s remaining)"
            )
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0.0, market.yes_price - 0.20),
                confidence_high=min(1.0, market.yes_price + 0.20),
                reasoning="Circuit breaker open — too many consecutive API failures",
                model_used="none (circuit open)",
                parse_failed=True,
            )

        # Hard budget check — refuse API calls if daily hard limit exceeded.
        # Pre-call estimation: a typical assess_market call uses ~1500-3000 tokens.
        # If adding the estimated usage would exceed the hard limit, skip the call.
        # L-11: Approximate token estimate for pre-call budget check. This is a
        # rough heuristic; should be refined with actual usage data once sufficient
        # API calls have been logged (track via _total_tokens_today / call_count).
        ESTIMATED_CALL_TOKENS = 3000
        hard_limit = self.settings.claude.daily_token_budget * 2
        if self._total_tokens_today + ESTIMATED_CALL_TOKENS > hard_limit:
            logger.critical(
                f"Claude API pre-call budget check: would exceed hard limit "
                f"({self._total_tokens_today:,} + {ESTIMATED_CALL_TOKENS:,} > {hard_limit:,}) "
                f"— refusing API call"
            )
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0.0, market.yes_price - 0.20),
                confidence_high=min(1.0, market.yes_price + 0.20),
                reasoning="Budget exceeded — returning market price as estimate",
                model_used="none (budget exceeded)",
                parse_failed=True,
            )
        if self.is_budget_exceeded():
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0.0, market.yes_price - 0.20),
                confidence_high=min(1.0, market.yes_price + 0.20),
                reasoning="Budget exceeded — returning market price as estimate",
                model_used="none (budget exceeded)",
            )

        # Check forecast cache — avoid re-calling Claude for same market.
        # Invalidate if the market price has moved >5% since the cached forecast,
        # since a large price move means the market has new information and the
        # old forecast may be stale.
        cache_key = f"assess:{market.ticker}"
        cached = self._forecast_cache.get(cache_key)
        if cached is not None:
            cached_price = getattr(cached, "_cached_market_price", None)
            price_moved = (
                cached_price is not None
                and abs(market.yes_price - cached_price) > 0.05
            )
            if price_moved:
                logger.info(
                    f"Forecast cache invalidated for {market.ticker}: "
                    f"price moved from {cached_price:.2f} to {market.yes_price:.2f}"
                )
            else:
                logger.debug(f"Forecast cache hit for {market.ticker}")
                return cached

        return None

    async def _build_prompt(
        self,
        market: Market,
        news_context: str,
        base_rate_context: str,
        position_value: float,
    ) -> tuple[str, str, MarketCategory, float]:
        """Build the Claude prompt with news enrichment and context.

        Returns:
            Tuple of (prompt, model, category, temperature)
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

        return prompt, model, category, temperature

    async def _call_claude(
        self,
        prompt: str,
        model: str,
        temperature: float,
        timeout: float,
    ):
        """Call Claude API with retry logic for rate limits and connection errors.

        Returns the raw API response object on success.

        Raises:
            asyncio.TimeoutError: On initial timeout (no retries for timeouts)
            anthropic.AuthenticationError: On auth failure (non-retryable)
            Exception: On unhandled errors after exhausting retries
        """
        client = self._get_client()
        try:
            return await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=self.settings.claude.max_tokens,
                    temperature=temperature,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
        except anthropic.RateLimitError:
            # Retry up to 3 times with exponential backoff before falling back
            for retry_attempt in range(1, 4):
                wait = min(10, 2 ** retry_attempt)  # Cap backoff at 10s
                logger.warning(
                    f"Claude API rate limited, retrying in {wait}s "
                    f"(attempt {retry_attempt}/3)"
                )
                await asyncio.sleep(wait)
                # Check budget before retry to prevent overrun
                if self.is_budget_exceeded():
                    logger.warning("Budget exceeded during rate limit retry — aborting")
                    break
                try:
                    client = self._get_client()
                    response = await asyncio.wait_for(
                        client.messages.create(
                            model=model,
                            max_tokens=self.settings.claude.max_tokens,
                            temperature=temperature,
                            system=SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}],
                        ),
                        timeout=timeout,
                    )
                    raw_text = self._extract_text(response)
                    if raw_text is None:
                        continue
                    logger.info(
                        f"Claude rate limit retry {retry_attempt} succeeded"
                    )
                    return response
                except anthropic.RateLimitError:
                    continue
                except Exception as e:
                    logger.debug(f"Non-rate-limit error during retry: {e}")
                    break

            logger.warning("Claude API rate limit retries exhausted, returning market price as fallback")
            raise  # Re-raise the original RateLimitError

        except anthropic.APIConnectionError as e:
            # M-11: Retryable connection error — backoff like rate limits
            for retry_attempt in range(1, 4):
                wait = min(10, 2 ** retry_attempt)
                logger.warning(
                    f"Claude API connection error, retrying in {wait}s "
                    f"(attempt {retry_attempt}/3): {e}"
                )
                await asyncio.sleep(wait)
                try:
                    client = self._get_client()
                    response = await asyncio.wait_for(
                        client.messages.create(
                            model=model,
                            max_tokens=self.settings.claude.max_tokens,
                            temperature=temperature,
                            system=SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}],
                        ),
                        timeout=timeout,
                    )
                    raw_text = self._extract_text(response)
                    if raw_text is None:
                        continue
                    logger.info(
                        f"Claude connection retry {retry_attempt} succeeded"
                    )
                    self._consecutive_failures = 0
                    return response
                except anthropic.APIConnectionError:
                    continue
                except Exception as retry_e:
                    logger.debug(f"Non-connection error during retry: {retry_e}")
                    break

            logger.warning("Claude API connection retries exhausted, returning market price as fallback")
            raise  # Re-raise the original APIConnectionError

    def _parse_api_response(
        self,
        response,
        market: Market,
        model: str,
        start_time: float,
        temperature: float,
    ) -> ForecastResult:
        """Parse API response, track tokens, check divergence, and cache result.

        Handles empty responses, token tracking, divergence flagging, and
        caching of successful forecasts.

        Args:
            response: Raw Claude API response object
            market: The market being assessed
            model: Model name used for the call
            start_time: Monotonic time when the call started
            temperature: Temperature used for the call

        Returns:
            ForecastResult with all metadata populated
        """
        latency_ms = int((time.monotonic() - start_time) * 1000)
        raw_text = self._extract_text(response)
        if raw_text is None:
            logger.warning("Claude API returned empty content for assess_market")
            return ForecastResult(
                probability=0.5, reasoning="Empty API response",
                parse_failed=True, model_used=model, latency_ms=latency_ms,
            )
        tokens_used = response.usage.input_tokens + response.usage.output_tokens
        self._track_tokens(
            tokens_used,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=model,
        )

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
        # Flag extreme divergence from market price at the forecaster level
        # so downstream callers can make informed decisions.
        max_div = self.settings.claude.max_divergence_from_market
        divergence = abs(forecast.probability - market.yes_price)
        if divergence > max_div:
            logger.warning(
                f"High divergence: Claude ({forecast.probability:.0%}) vs market "
                f"({market.yes_price:.0%}) = {divergence:.0%} for '{market.question[:50]}...'"
            )
            forecast.high_divergence = True

        # Cache successful forecasts to avoid redundant API calls.
        # Store the market price at cache time for price-based invalidation.
        cache_key = f"assess:{market.ticker}"
        if not forecast.parse_failed:
            forecast._cached_market_price = market.yes_price  # type: ignore[attr-defined]
            self._forecast_cache.set(cache_key, forecast)
        # Successful call — reset circuit breaker failure counter
        self._consecutive_failures = 0
        return forecast

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
        # Stage 1: Precondition checks (circuit breaker, budget, cache)
        early_result = self._check_preconditions(market)
        if early_result is not None:
            return early_result

        # Stage 2: Build prompt with news enrichment
        prompt, model, category, temperature = await self._build_prompt(
            market, news_context, base_rate_context, position_value,
        )

        # Stage 3: Call Claude API with retry logic
        start_time = time.monotonic()
        timeout = self.settings.claude.api_timeout_seconds
        try:
            response = await self._call_claude(prompt, model, temperature, timeout)

            # Stage 4: Parse response, track tokens, check divergence, cache
            return self._parse_api_response(
                response, market, model, start_time, temperature,
            )

        except asyncio.TimeoutError:
            logger.warning(f"Claude API call timed out after {timeout}s")
            self._record_api_failure()
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.25),
                confidence_high=min(1, market.yes_price + 0.25),
                reasoning="API call timed out after 60s",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
                parse_failed=True,
            )
        except anthropic.RateLimitError:
            # Rate limit exhausted counts as a failure for circuit breaker purposes
            self._record_api_failure()
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.25),
                confidence_high=min(1, market.yes_price + 0.25),
                reasoning="Rate limited — retries exhausted, using market price as fallback",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
                parse_failed=True,
            )
        except anthropic.APIConnectionError:
            self._record_api_failure()
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.25),
                confidence_high=min(1, market.yes_price + 0.25),
                reasoning="Connection error — retries exhausted, using market price as fallback",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
                parse_failed=True,
            )
        except anthropic.AuthenticationError as e:
            # M-11: Non-retryable auth error — do not retry, fail immediately
            logger.error(
                f"Claude API authentication failed (non-retryable): {e}. "
                "Check ANTHROPIC_API_KEY is valid and not expired."
            )
            self._record_api_failure()
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.25),
                confidence_high=min(1, market.yes_price + 0.25),
                reasoning=f"Authentication failed (non-retryable): {e}",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
                parse_failed=True,
            )
        except Exception as e:
            logger.exception(f"Claude assessment failed: {e}")
            self._record_api_failure()
            return ForecastResult(
                probability=market.yes_price,
                confidence_low=max(0, market.yes_price - 0.25),
                confidence_high=min(1, market.yes_price + 0.25),
                reasoning=f"Assessment failed: {e}",
                model_used=model,
                latency_ms=int((time.monotonic() - start_time) * 1000),
                parse_failed=True,
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
        if self.is_budget_exceeded():
            return None

        model = self._select_model(position_value)
        start_time = time.monotonic()

        try:
            client = self._get_client()
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=self.settings.claude.max_tokens,
                    temperature=self.settings.claude.temperature,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": custom_prompt}],
                ),
                timeout=self.settings.claude.api_timeout_seconds,
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            raw_text = self._extract_text(response)
            if raw_text is None:
                logger.warning("Claude API returned empty content for assess_market_with_prompt")
                return ForecastResult(
                    probability=0.5, reasoning="Empty API response",
                    parse_failed=True, model_used=model, latency_ms=latency_ms,
                )
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
            logger.error(f"Custom Claude assessment failed: {e}", exc_info=True)
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
        if self.is_budget_exceeded():
            return None

        # Circuit breaker check for cross_check_assess
        import time as _time
        now_mono = _time.monotonic()
        if self._circuit_open_until > now_mono:
            remaining = self._circuit_open_until - now_mono
            logger.warning(
                f"Claude API circuit breaker open — skipping cross-check for "
                f"'{market.question[:50]}...' ({remaining:.0f}s remaining)"
            )
            return None

        # Build prompt (also handles news enrichment and model selection)
        prompt, model, category, _temperature = await self._build_prompt(
            market, news_context, base_rate_context, position_value,
        )

        temp_low = self.settings.claude.cross_check_temp_low
        temp_high = self.settings.claude.cross_check_temp_high
        threshold = self.settings.claude.cross_check_disagreement_threshold

        async def _call_at_temp(temp: float) -> ForecastResult:
            client = self._get_client()
            start = time.monotonic()
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=self.settings.claude.max_tokens,
                    temperature=temp,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=self.settings.claude.api_timeout_seconds,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            raw_text = self._extract_text(response)
            if raw_text is None:
                logger.warning("Claude API returned empty content for cross_check_assess")
                return ForecastResult(
                    probability=0.5, reasoning="Empty API response",
                    parse_failed=True, model_used=model, latency_ms=latency_ms,
                )
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

        # Average the two results, widen CI based on disagreement
        ci_low = min(low_result.confidence_low, high_result.confidence_low)
        ci_high = max(low_result.confidence_high, high_result.confidence_high)

        if disagreement > threshold:
            # Instead of returning None (skipping the market entirely), return
            # the averaged result with widened CI and a "low_confidence" flag.
            # This lets the ensemble and Kelly sizer naturally reduce position
            # size rather than missing potentially profitable opportunities.
            logger.info(
                f"Cross-check DISAGREE on '{market.question[:50]}...' "
                f"(low={low_result.probability:.0%}, high={high_result.probability:.0%}, "
                f"gap={disagreement:.0%}) — returning with widened CI"
            )
            # Widen CI proportional to disagreement
            ci_low = max(0.01, avg_prob - disagreement)
            ci_high = min(0.99, avg_prob + disagreement)
            # Validate CI bounds after widening
            if ci_low >= ci_high:
                # Fall back to a reasonable interval centered on avg_prob
                ci_low = max(0.01, avg_prob - 0.10)
                ci_high = min(0.99, avg_prob + 0.10)
                logger.warning(
                    f"CI bounds inverted after widening for {market.ticker} — "
                    f"falling back to ±10% around {avg_prob:.2f}"
                )
        else:
            logger.info(
                f"Cross-check AGREE on '{market.question[:50]}...' "
                f"(low={low_result.probability:.0%}, high={high_result.probability:.0%}, "
                f"avg={avg_prob:.0%})"
            )

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
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"JSON direct parse failed, trying fallbacks: {e}")

        # Strategy 2: Extract from markdown code blocks
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if code_block_match:
            try:
                data = json.loads(code_block_match.group(1).strip())
                return self._build_forecast(data)
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"JSON code block parse failed: {e}")

        # Strategy 3: Find first { and last } and try to parse
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                data = json.loads(text[first_brace:last_brace + 1])
                return self._build_forecast(data)
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"JSON brace extraction parse failed: {e}")

        # Strategy 4: Try to extract probability from prose as last resort.
        # Use findall + take LAST match to avoid picking up stale references
        # like "probability shifted from 0.73 to 0.85" (we want 0.85, not 0.73).
        prob_matches = re.findall(
            r'(?:probability|prob)["\'\s:=]+\s*([01]?\.\d+|0|1(?:\.0+)?)', text, re.IGNORECASE
        )
        if not prob_matches:
            # Try percentage format: "probability: 65%"
            pct_matches = re.findall(r'(?:probability|prob)["\'\s:=]+\s*(\d{1,3})%', text, re.IGNORECASE)
            if pct_matches:
                prob = float(pct_matches[-1]) / 100.0
                parse_failed = True
                # M-2: Validate extraction context — check surrounding sentence
                parse_failed = self._validate_prose_extraction(text, prob, parse_failed)
                # M-2: Check for ambiguous numbers (large spread among extracted values)
                all_vals = [float(v) / 100.0 for v in pct_matches]
                if len(all_vals) > 1 and (max(all_vals) - min(all_vals)) > 0.30:
                    logger.warning(
                        f"Ambiguous prose extraction: values span {min(all_vals):.2f}–{max(all_vals):.2f} "
                        f"(delta {max(all_vals) - min(all_vals):.2f} > 0.30)"
                    )
                    parse_failed = True
                logger.warning(f"Extracted probability {prob} from percentage in prose (last of {len(pct_matches)} matches)")
                return ForecastResult(
                    probability=max(0.01, min(0.99, prob)),
                    reasoning=f"Parsed probability from prose (%). Raw: {raw_text[:200]}",
                    parse_failed=parse_failed,
                )
        if prob_matches:
            prob = float(prob_matches[-1])
            parse_failed = True
            # M-2: Validate extraction context — check surrounding sentence
            parse_failed = self._validate_prose_extraction(text, prob, parse_failed)
            # M-2: Check for ambiguous numbers (large spread among extracted values)
            all_vals = [float(v) for v in prob_matches]
            if len(all_vals) > 1 and (max(all_vals) - min(all_vals)) > 0.30:
                logger.warning(
                    f"Ambiguous prose extraction: values span {min(all_vals):.2f}–{max(all_vals):.2f} "
                    f"(delta {max(all_vals) - min(all_vals):.2f} > 0.30)"
                )
                parse_failed = True
            logger.warning(f"Extracted probability {prob} from prose response (last of {len(prob_matches)} matches)")
            return ForecastResult(
                probability=max(0.01, min(0.99, prob)),
                reasoning=f"Parsed probability from prose. Raw: {raw_text[:200]}",
                parse_failed=parse_failed,
            )

        logger.warning(f"Failed to parse Claude response as JSON: {raw_text[:200]}")
        return ForecastResult(
            probability=0.5,
            reasoning=f"JSON parse failed, raw: {raw_text[:200]}",
            parse_failed=True,
        )

    @staticmethod
    def _validate_prose_extraction(text: str, prob: float, parse_failed: bool) -> bool:
        """M-2: Validate that extracted probability appears in a forecasting context.

        Returns updated parse_failed flag (may be set to True if context is suspect).
        """
        # Check if the extracted value appears near forecasting-related words
        context_words = {"probability", "estimate", "likely", "chance", "forecast", "predict", "assessment"}
        # Search for any sentence containing the extracted number
        # Build a pattern matching the number as decimal or percentage
        text_lower = text.lower()
        has_context = any(word in text_lower for word in context_words)
        if not has_context:
            logger.warning(
                f"Prose extraction lacks forecasting context words — "
                f"extracted {prob} may not be a probability estimate"
            )
            parse_failed = True
        return parse_failed

    def _build_forecast(self, data: dict) -> ForecastResult:
        """Build a ForecastResult from parsed JSON data."""
        # H-11: Validate that a probability key exists; check common alternatives
        if "probability" not in data:
            alt_keys = {"prob": None, "p": None, "forecast": None, "prediction": None}
            found_key = None
            for alt in alt_keys:
                if alt in data:
                    found_key = alt
                    break
            if found_key is not None:
                logger.warning(
                    f"JSON schema validation: 'probability' key missing, "
                    f"using alternative key '{found_key}' = {data[found_key]}"
                )
                data["probability"] = data[found_key]
            else:
                logger.warning(
                    "JSON schema validation: 'probability' key missing and no "
                    f"known alternatives found in keys: {list(data.keys())}"
                )
                return ForecastResult(
                    probability=0.5,
                    reasoning=f"Missing 'probability' key in JSON. Keys: {list(data.keys())}",
                    parse_failed=True,
                )

        raw_probability = float(data.get("probability", 0.5))
        probability = max(0.01, min(0.99, raw_probability))

        if raw_probability != probability:
            logger.debug(
                f"Clamped probability from {raw_probability:.6f} to {probability:.2f}"
            )

        # Safe CI extraction with fallback defaults
        def _safe_float(value, default: float) -> float:
            """Safely convert to float, returning default on failure."""
            if value is None:
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                logger.warning(f"Non-numeric CI value: {value!r} — using default {default}")
                return default

        ci_low_raw = _safe_float(data.get("confidence_low"), max(0, probability - 0.20))
        ci_high_raw = _safe_float(data.get("confidence_high"), min(1, probability + 0.20))

        return ForecastResult(
            probability=probability,
            confidence_low=max(0.0, min(1.0, ci_low_raw)),
            confidence_high=max(0.0, min(1.0, ci_high_raw)),
            key_factors_for=data.get("key_factors_for", []),
            key_factors_against=data.get("key_factors_against", []),
            uncertainties=data.get("uncertainties", []),
            reasoning=data.get("reasoning", ""),
        )
