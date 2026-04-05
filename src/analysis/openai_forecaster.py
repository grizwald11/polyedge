"""OpenAI forecaster — uses GPT-4o to assess market probabilities.

Mirrors the ClaudeForecaster interface for ensemble diversity. Uses the
same category-specific prompt templates (they're model-agnostic) and the
same response parsing pipeline (forecast_parser).

Falls back gracefully if OPENAI_API_KEY is not set — just skips GPT-4o
forecasts and lets the ensemble run with Claude alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from src.analysis.forecast_parser import (
    build_forecast as _build_forecast,
    parse_response as _parse_response,
)
from src.analysis.prompt_builder import (
    build_forecaster_prompt,
    select_temperature as _select_temperature_func,
)
from src.analysis.prompt_templates import SYSTEM_PROMPT
from src.config import Settings
from src.core.models import ForecastResult, Market, MarketCategory

logger = logging.getLogger(__name__)


class OpenAIForecaster:
    """Calls GPT-4o to assess market probabilities.

    Mirrors ClaudeForecaster's interface so both can feed into the ensemble.
    """

    _CACHE_TTL_SECONDS = 600

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._available = False

        # Token and cost tracking
        self._total_tokens_today: int = 0
        self._total_cost_today: float = 0.0
        self._today_date: str = ""
        self._call_count_today: int = 0

        # GPT-4o pricing per million tokens (input/output)
        self._cost_per_million: tuple[float, float] = (2.50, 10.0)

        # Circuit breaker: disable after 3 consecutive failures, 5-min cooldown
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0

        # Forecast cache
        from src.data.cache import TTLCache
        self._forecast_cache = TTLCache(ttl_seconds=self._CACHE_TTL_SECONDS)

        # OpenAI config from settings
        openai_cfg = getattr(settings, 'openai', None)
        self._model = getattr(openai_cfg, 'model', 'gpt-4o') if openai_cfg else 'gpt-4o'
        self._max_tokens = getattr(openai_cfg, 'max_tokens', 2000) if openai_cfg else 2000
        self._timeout = getattr(openai_cfg, 'timeout', 60) if openai_cfg else 60
        self._daily_budget = getattr(openai_cfg, 'daily_budget', 500_000) if openai_cfg else 500_000

        # Try to initialize the client
        self._init_client()

    def _init_client(self) -> None:
        """Initialize the OpenAI async client if API key is available."""
        import os
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.info(
                "OPENAI_API_KEY not set — GPT-4o forecaster disabled. "
                "Set OPENAI_API_KEY to enable ensemble diversity."
            )
            self._available = False
            return

        try:
            import openai
            self._openai = openai
            self._client = openai.AsyncOpenAI(api_key=api_key)
            self._available = True
            logger.info("OpenAI GPT-4o forecaster initialized")
        except ImportError:
            logger.info(
                "openai package not installed — GPT-4o forecaster disabled. "
                "Install with: pip install openai"
            )
            self._available = False
        except Exception as e:
            logger.warning(f"OpenAI client initialization failed: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        """Return True if the OpenAI client is configured and operational."""
        return self._available and self._client is not None

    def is_circuit_open(self) -> bool:
        """Check if the circuit breaker is currently open."""
        return self._circuit_open_until > time.monotonic()

    async def close(self) -> None:
        """Close the underlying OpenAI client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._available = False

    def _record_api_failure(self) -> None:
        """Track consecutive failures for circuit breaker."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            open_duration = 300.0  # 5 minutes
            self._circuit_open_until = time.monotonic() + open_duration
            logger.error(
                f"GPT-4o circuit breaker OPENED after {self._consecutive_failures} "
                f"consecutive failures — disabling calls for {open_duration:.0f}s"
            )

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for a GPT-4o API call."""
        return (
            input_tokens * self._cost_per_million[0]
            + output_tokens * self._cost_per_million[1]
        ) / 1_000_000

    def _track_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Track daily token usage for budget awareness."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._today_date:
            if self._today_date and self._total_tokens_today > 0:
                avg = self._total_tokens_today / max(1, self._call_count_today)
                logger.info(
                    f"GPT-4o usage yesterday: {self._total_tokens_today:,} tokens "
                    f"across {self._call_count_today} calls "
                    f"(avg {avg:,.0f} tokens/call), ~${self._total_cost_today:.2f}"
                )
            self._today_date = today
            self._total_tokens_today = 0
            self._total_cost_today = 0.0
            self._call_count_today = 0

        total = input_tokens + output_tokens
        self._total_tokens_today += total
        self._call_count_today += 1
        self._total_cost_today += self._estimate_cost(input_tokens, output_tokens)

        if self._total_tokens_today > self._daily_budget:
            logger.warning(
                f"GPT-4o daily token usage ({self._total_tokens_today:,}) "
                f"exceeds budget ({self._daily_budget:,})"
            )

    def is_budget_exceeded(self) -> bool:
        """Check if the hard daily budget (2x soft limit) has been exceeded."""
        hard_limit = self._daily_budget * 2
        return self._total_tokens_today > hard_limit

    def _select_temperature(self, category: MarketCategory) -> float:
        """Select temperature based on market category."""
        return _select_temperature_func(
            category,
            self.settings.claude.category_temperatures,
            self.settings.claude.temperature,
        )

    async def assess_market(
        self,
        market: Market,
        news_context: str = "",
        position_value: float = 0.0,
        base_rate_context: str = "",
        accuracy_context: str = "",
    ) -> Optional[ForecastResult]:
        """Assess a market's true probability using GPT-4o.

        Returns ForecastResult on success, None if unavailable or failed.
        """
        if not self.is_available:
            return None

        # Circuit breaker check
        if self.is_circuit_open():
            remaining = self._circuit_open_until - time.monotonic()
            logger.debug(
                f"GPT-4o circuit breaker open ({remaining:.0f}s remaining) — skipping"
            )
            return None

        # Budget check
        if self.is_budget_exceeded():
            logger.warning("GPT-4o daily budget exceeded — skipping")
            return None

        # Cache check
        cache_key = f"gpt4o:{market.ticker}"
        cached = self._forecast_cache.get(cache_key)
        if cached is not None:
            cached_price = getattr(cached, "_cached_market_price", None)
            price_moved = (
                cached_price is not None
                and abs(market.yes_price - cached_price) > 0.05
            )
            if not price_moved:
                logger.debug(f"GPT-4o cache hit for {market.ticker}")
                return cached

        # Build prompt using the same templates as Claude
        from src.analysis.market_classifier import classify_market
        category = classify_market(market)
        temperature = self._select_temperature(category)

        # Build a simple prompt (reuse the template system)
        from src.analysis.prompt_builder import build_forecaster_prompt
        from src.analysis.prompt_ab_testing import PromptVariantManager

        # Provide a no-op news researcher to avoid None dereference
        class _NoOpResearcher:
            async def get_context(self, _q: str) -> str:
                return ""

        variant_manager = PromptVariantManager(enabled=False)
        prompt, _model, _category, _temp, _variant = await build_forecaster_prompt(
            market,
            news_context,
            base_rate_context,
            position_value,
            news_researcher=_NoOpResearcher(),
            variant_manager=variant_manager,
            highstakes_threshold=self.settings.claude.highstakes_threshold,
            model_highstakes=self._model,
            model_primary=self._model,
            edge_highstakes_threshold=0.15,
            category_temperatures=self.settings.claude.category_temperatures,
            default_temperature=self.settings.claude.temperature,
            accuracy_context=accuracy_context,
        )

        # Call GPT-4o with retry logic
        start_time = time.monotonic()
        try:
            response = await self._call_gpt4o(prompt, temperature)
        except Exception as e:
            logger.warning(f"GPT-4o assessment failed for {market.ticker}: {e}")
            self._record_api_failure()
            return None

        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Parse response
        if not response.choices or not response.choices[0].message.content:
            logger.warning("GPT-4o returned empty content")
            self._record_api_failure()
            return None

        raw_text = response.choices[0].message.content

        # Track tokens
        usage = response.usage
        if usage:
            self._track_tokens(usage.prompt_tokens, usage.completion_tokens)

        # Parse using the same 5-strategy fallback chain as Claude
        forecast = _parse_response(raw_text)
        forecast.model_used = f"gpt-4o"
        forecast.tokens_used = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
        forecast.latency_ms = latency_ms
        forecast.raw_response = raw_text

        logger.info(
            f"GPT-4o assessed '{market.question[:50]}...' -> "
            f"{forecast.probability:.0%} (temp={temperature}, latency={latency_ms}ms)"
        )

        # Reset circuit breaker on success
        self._consecutive_failures = 0

        # Cache successful forecasts
        if not forecast.parse_failed:
            forecast._cached_market_price = market.yes_price  # type: ignore[attr-defined]
            self._forecast_cache.set(cache_key, forecast)

        return forecast

    async def _call_gpt4o(self, prompt: str, temperature: float):
        """Call GPT-4o API with retry logic for rate limits and connection errors.

        Retries up to 3 times with exponential backoff on rate limit and
        connection errors. Timeout: 60s.
        """
        from src.core.retry_helper import retry_with_backoff

        def _on_retry(attempt: int, exc: BaseException) -> None:
            logger.warning(
                f"GPT-4o API {type(exc).__name__}, retrying "
                f"(attempt {attempt + 1}/3)"
            )

        async def _make_request():
            return await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                ),
                timeout=self._timeout,
            )

        # Determine retryable exceptions dynamically (openai may not be imported)
        retryable = (ConnectionError, TimeoutError)
        try:
            retryable = (
                self._openai.RateLimitError,
                self._openai.APIConnectionError,
            )
        except AttributeError:
            pass

        response = await retry_with_backoff(
            _make_request,
            max_retries=3,
            base_delay=2.0,
            max_delay=10.0,
            retryable_exceptions=retryable,
            on_retry=_on_retry,
            abort_check=self.is_budget_exceeded,
        )
        return response

    async def health_check(self) -> bool:
        """Quick check that the OpenAI API key is valid."""
        if not self.is_available:
            return False
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=10,
            )
            return bool(response.choices)
        except Exception as e:
            logger.warning(f"GPT-4o health check failed: {e}")
            return False
