"""Shared retry logic for external API calls (M-3 audit fix).

Provides a reusable async retry wrapper with exponential backoff, jitter,
and configurable exception handling. Replaces duplicated retry patterns
across kalshi_client.py, claude_forecaster.py, and news_researcher.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Coroutine, Optional, Type

logger = logging.getLogger(__name__)


async def retry_with_backoff(
    func: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_exceptions: tuple[Type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
    **kwargs: Any,
) -> Any:
    """Execute an async function with exponential backoff retry.

    Args:
        func: Async callable to execute.
        max_retries: Maximum number of retry attempts (total calls = max_retries + 1).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Maximum delay cap in seconds.
        retryable_exceptions: Tuple of exception types that trigger a retry.
        on_retry: Optional callback(attempt, exception) called before each retry sleep.

    Returns:
        The return value of func.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exception: Optional[BaseException] = None

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as e:
            last_exception = e
            if attempt >= max_retries:
                break
            delay = min(max_delay, base_delay * (2 ** attempt) + random.uniform(0, 1))
            if on_retry:
                on_retry(attempt, e)
            else:
                logger.debug(
                    f"Retry {attempt + 1}/{max_retries} after {type(e).__name__}: {e} "
                    f"(waiting {delay:.1f}s)"
                )
            await asyncio.sleep(delay)

    raise last_exception  # type: ignore[misc]
