"""Claude forecaster — uses Claude API to assess market probabilities.

Calls Claude with structured prompts, parses JSON responses into
ForecastResult models. Selects model based on position size:
sonnet for routine, opus for high-stakes.

Prompt construction is delegated to prompt_builder and response parsing
to forecast_parser (M-1 audit refactor).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import anthropic

from src.analysis.forecast_parser import (
    build_forecast as _build_forecast,
    extract_text as _extract_text,
    parse_response as _parse_response,
)
from src.analysis.market_classifier import classify_market
from src.analysis.news_researcher import NewsResearcher
from src.analysis.prompt_ab_testing import PromptVariantManager
from src.analysis.prompt_builder import (
    build_forecaster_prompt,
    select_model as _select_model_func,
    select_temperature as _select_temperature_func,
    validate_resolution_criteria as _validate_resolution_criteria,
)
from src.analysis.prompt_templates import SYSTEM_PROMPT
from src.config import Settings
from src.core.models import ForecastResult, Market, MarketCategory

logger = logging.getLogger(__name__)


class ClaudeForecaster:
    """Calls Claude to assess market probabilities."""

    # Cache TTL in seconds — avoid re-assessing same market within 5 minutes
    _CACHE_TTL_SECONDS = 600

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
        # Prompt A/B testing: Thompson sampling for prompt variant selection
        ab_enabled = getattr(settings.claude, 'ab_testing_enabled', True)
        self.variant_manager = PromptVariantManager(enabled=ab_enabled)
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

    # L-2: Edge threshold for high-stakes model selection (was hardcoded at 0.15).
    # Uses config value or falls back to 0.15 if not configured.
    EDGE_HIGHSTAKES_THRESHOLD = 0.15

    def _select_model(self, position_value: float = 0.0, edge: float = 0.0) -> str:
        """Select model based on position value or edge size."""
        edge_threshold = getattr(
            self.settings.claude, "edge_highstakes_threshold",
            self.EDGE_HIGHSTAKES_THRESHOLD,
        )
        return _select_model_func(
            position_value,
            edge,
            highstakes_threshold=self.settings.claude.highstakes_threshold,
            model_highstakes=self.settings.claude.model_highstakes,
            model_primary=self.settings.claude.model_primary,
            edge_highstakes_threshold=edge_threshold,
        )

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
        return _select_temperature_func(
            category,
            self.settings.claude.category_temperatures,
            self.settings.claude.temperature,
        )

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
        return _extract_text(response)

    def _validate_resolution_criteria(self, description: str) -> str:
        """Validate and enhance resolution criteria if missing or too short."""
        return _validate_resolution_criteria(description)

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
        # Pre-call estimation: use rolling average of actual token usage if
        # available, otherwise fall back to 3000 (M-6 audit fix).
        DEFAULT_ESTIMATED_CALL_TOKENS = 3000
        if self._call_count_today > 0:
            ESTIMATED_CALL_TOKENS = int(self._total_tokens_today / self._call_count_today)
        else:
            ESTIMATED_CALL_TOKENS = DEFAULT_ESTIMATED_CALL_TOKENS
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
        accuracy_context: str = "",
    ) -> tuple[str, str, MarketCategory, float]:
        """Build the Claude prompt with news enrichment and context.

        Returns:
            Tuple of (prompt, model, category, temperature)
        """
        edge_threshold = getattr(
            self.settings.claude, "edge_highstakes_threshold",
            self.EDGE_HIGHSTAKES_THRESHOLD,
        )
        prompt, model, category, temperature, variant_name = await build_forecaster_prompt(
            market,
            news_context,
            base_rate_context,
            position_value,
            self.news_researcher,
            self.variant_manager,
            highstakes_threshold=self.settings.claude.highstakes_threshold,
            model_highstakes=self.settings.claude.model_highstakes,
            model_primary=self.settings.claude.model_primary,
            edge_highstakes_threshold=edge_threshold,
            category_temperatures=self.settings.claude.category_temperatures,
            default_temperature=self.settings.claude.temperature,
            accuracy_context=accuracy_context,
        )
        self._last_variant_name = variant_name  # Store for downstream tracking
        return prompt, model, category, temperature

    async def _call_claude(
        self,
        prompt: str,
        model: str,
        temperature: float,
        timeout: float,
    ):
        """Call Claude API with retry logic for rate limits and connection errors.

        Uses the shared retry_with_backoff helper (M-12 consolidation).

        Returns the raw API response object on success.

        Raises:
            asyncio.TimeoutError: On timeout (not retried)
            anthropic.AuthenticationError: On auth failure (non-retryable)
            anthropic.RateLimitError: After exhausting retries
            anthropic.APIConnectionError: After exhausting retries
        """
        from src.core.retry_helper import retry_with_backoff

        def _on_retry(attempt: int, exc: BaseException) -> None:
            exc_name = type(exc).__name__
            logger.warning(
                f"Claude API {exc_name}, retrying "
                f"(attempt {attempt + 1}/3)"
            )
            # M-1: Track estimated tokens for failed attempts. Rate-limited and
            # connection-error calls may still consume input tokens on the server
            # side. Use the running average if available, otherwise a conservative
            # estimate of input-only tokens (half a typical call).
            estimated = (
                self._total_tokens_today // self._call_count_today
                if self._call_count_today > 0
                else 1500  # Conservative: ~1500 input tokens for a typical prompt
            )
            self._total_tokens_today += estimated
            logger.debug(
                f"M-1: Estimated {estimated} tokens for failed attempt "
                f"(total today: {self._total_tokens_today:,})"
            )

        async def _make_request():
            client = self._get_client()
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

        try:
            response = await retry_with_backoff(
                _make_request,
                max_retries=3,
                base_delay=2.0,
                max_delay=10.0,
                retryable_exceptions=(
                    anthropic.RateLimitError,
                    anthropic.APIConnectionError,
                ),
                on_retry=_on_retry,
                abort_check=self.is_budget_exceeded,
            )
            self._consecutive_failures = 0
            return response
        except (anthropic.RateLimitError, anthropic.APIConnectionError):
            logger.warning(
                "Claude API retries exhausted, returning market price as fallback"
            )
            raise

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
        raw_text = _extract_text(response)
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
        forecast = _parse_response(raw_text)
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
        force_model: Optional[str] = None,
        accuracy_context: str = "",
    ) -> ForecastResult:
        """Assess a market's true probability using Claude.

        Args:
            market: The market to assess
            news_context: Additional news/context to include
            position_value: Expected position size (determines model selection)
            base_rate_context: Historical base rate string for the category
            force_model: Override model selection (e.g., force opus for escalation)

        Returns:
            ForecastResult with probability estimate and reasoning
        """
        # Stage 1: Precondition checks (circuit breaker, budget, cache)
        # Skip cache when force_model is set (escalation re-verification)
        if force_model:
            early_result = self._check_preconditions(market)
            if early_result is not None and early_result.parse_failed:
                return early_result  # Respect circuit breaker/budget, but skip cache
        else:
            early_result = self._check_preconditions(market)
            if early_result is not None:
                return early_result

        # Stage 2: Build prompt with news enrichment
        prompt, model, category, temperature = await self._build_prompt(
            market, news_context, base_rate_context, position_value,
            accuracy_context=accuracy_context,
        )
        if force_model:
            model = force_model

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
        except (anthropic.APIError, asyncio.TimeoutError, json.JSONDecodeError, ValueError, KeyError) as e:
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

        # H-4: Check circuit breaker — don't burn API quota during bad states
        if self._circuit_open_until and time.monotonic() < self._circuit_open_until:
            logger.debug("assess_market_with_prompt skipped: circuit breaker open")
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
            raw_text = _extract_text(response)
            if raw_text is None:
                logger.warning("Claude API returned empty content for assess_market_with_prompt")
                return ForecastResult(
                    probability=0.5, reasoning="Empty API response",
                    parse_failed=True, model_used=model, latency_ms=latency_ms,
                )
            tokens_used = response.usage.input_tokens + response.usage.output_tokens

            forecast = _parse_response(raw_text)
            forecast.model_used = model
            forecast.tokens_used = tokens_used
            forecast.latency_ms = latency_ms
            forecast.raw_response = raw_text

            logger.info(
                f"Claude [{model}] custom assessment for '{market.question[:50]}...' → "
                f"{forecast.probability:.0%} (latency: {latency_ms}ms)"
            )
            return forecast

        except (anthropic.APIError, asyncio.TimeoutError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"Custom Claude assessment failed: {e}", exc_info=True)
            return None

    async def cross_check_assess(
        self,
        market: Market,
        news_context: str = "",
        position_value: float = 0.0,
        base_rate_context: str = "",
        accuracy_context: str = "",
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
            accuracy_context=accuracy_context,
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
            raw_text = _extract_text(response)
            if raw_text is None:
                logger.warning("Claude API returned empty content for cross_check_assess")
                return ForecastResult(
                    probability=0.5, reasoning="Empty API response",
                    parse_failed=True, model_used=model, latency_ms=latency_ms,
                )
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
            forecast = _parse_response(raw_text)
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
        except (anthropic.APIError, asyncio.TimeoutError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"Cross-check dual-call failed, returning None: {e}")
            return None

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

        Delegates to forecast_parser.parse_response. Kept as instance method
        for backward compatibility with any subclasses.
        """
        return _parse_response(raw_text)

    @staticmethod
    def _validate_prose_extraction(text: str, prob: float, parse_failed: bool) -> bool:
        """M-2: Validate that extracted probability appears in a forecasting context.

        Delegates to forecast_parser._validate_prose_extraction.
        """
        from src.analysis.forecast_parser import _validate_prose_extraction
        return _validate_prose_extraction(text, prob, parse_failed)

    def _build_forecast(self, data: dict) -> ForecastResult:
        """Build a ForecastResult from parsed JSON data.

        Delegates to forecast_parser.build_forecast.
        """
        return _build_forecast(data)
