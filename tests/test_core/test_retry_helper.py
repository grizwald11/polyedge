"""Tests for shared retry helper with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.retry_helper import retry_with_backoff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Transient(Exception):
    """Simulates a transient, retryable error."""


class _Fatal(Exception):
    """Simulates a non-retryable error."""


def _make_flaky(fail_times: int, exc: BaseException = _Transient("boom")) -> AsyncMock:
    """Return an async callable that raises *exc* for the first *fail_times* calls."""
    call_count = 0

    async def _inner(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        if call_count <= fail_times:
            raise exc
        return "ok"

    mock = AsyncMock(side_effect=_inner)
    return mock


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        """Function that never fails should be called exactly once."""
        func = AsyncMock(return_value="result")
        result = await retry_with_backoff(func, max_retries=3, base_delay=0)
        assert result == "result"
        func.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_func_return_value(self):
        """Return value of func is propagated correctly."""
        func = AsyncMock(return_value={"data": 42})
        result = await retry_with_backoff(func, max_retries=0, base_delay=0)
        assert result == {"data": 42}

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        """Positional and keyword arguments are forwarded to func."""
        func = AsyncMock(return_value=None)
        await retry_with_backoff(func, "a", "b", max_retries=0, base_delay=0, key="v")
        func.assert_awaited_once_with("a", "b", key="v")

    @pytest.mark.asyncio
    async def test_succeeds_on_last_allowed_attempt(self):
        """Func that fails twice then succeeds should succeed when max_retries >= 2."""
        func = _make_flaky(fail_times=2)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(
                func,
                max_retries=3,
                base_delay=1.0,
                retryable_exceptions=(_Transient,),
            )
        assert result == "ok"
        assert func.await_count == 3  # 2 failures + 1 success


# ---------------------------------------------------------------------------
# Retry / exhaustion
# ---------------------------------------------------------------------------

class TestRetryExhaustion:
    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self):
        """Should raise the last exception when all retries fail."""
        func = AsyncMock(side_effect=_Transient("persistent"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_Transient, match="persistent"):
                await retry_with_backoff(
                    func,
                    max_retries=2,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                )

    @pytest.mark.asyncio
    async def test_total_call_count_is_max_retries_plus_one(self):
        """Total invocations should be max_retries + 1."""
        func = AsyncMock(side_effect=_Transient("x"))
        max_retries = 4
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=max_retries,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                )
        assert func.await_count == max_retries + 1

    @pytest.mark.asyncio
    async def test_zero_retries_calls_func_once_then_raises(self):
        """max_retries=0 means a single attempt with no retries."""
        func = AsyncMock(side_effect=_Transient("nope"))
        with pytest.raises(_Transient):
            await retry_with_backoff(
                func,
                max_retries=0,
                base_delay=0,
                retryable_exceptions=(_Transient,),
            )
        func.assert_awaited_once()


# ---------------------------------------------------------------------------
# Non-retryable exceptions
# ---------------------------------------------------------------------------

class TestNonRetryableExceptions:
    @pytest.mark.asyncio
    async def test_non_retryable_exception_propagates_immediately(self):
        """Exceptions not in retryable_exceptions must propagate without retry."""
        func = AsyncMock(side_effect=_Fatal("fatal"))
        with pytest.raises(_Fatal):
            await retry_with_backoff(
                func,
                max_retries=5,
                base_delay=0,
                retryable_exceptions=(_Transient,),
            )
        # Must NOT retry; called exactly once.
        func.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_specified_exception_types_are_retried(self):
        """Only listed exception types trigger retries; others bubble up immediately."""
        func = AsyncMock(side_effect=ValueError("bad value"))
        with pytest.raises(ValueError):
            await retry_with_backoff(
                func,
                max_retries=3,
                base_delay=0,
                retryable_exceptions=(TypeError,),
            )
        func.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multiple_retryable_types_accepted(self):
        """A tuple with multiple exception types should retry on any of them."""
        func = _make_flaky(fail_times=1, exc=ConnectionError("conn"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(
                func,
                max_retries=2,
                base_delay=1.0,
                retryable_exceptions=(ConnectionError, TimeoutError),
            )
        assert result == "ok"


# ---------------------------------------------------------------------------
# Default retryable_exceptions (Exception catches everything)
# ---------------------------------------------------------------------------

class TestDefaultRetryableExceptions:
    @pytest.mark.asyncio
    async def test_default_retries_on_any_exception(self):
        """Default retryable_exceptions=(Exception,) retries on any Exception subclass."""
        func = _make_flaky(fail_times=1, exc=RuntimeError("oops"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(func, max_retries=2, base_delay=1.0)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Delay / backoff behaviour
# ---------------------------------------------------------------------------

class TestBackoffBehaviour:
    @pytest.mark.asyncio
    async def test_sleep_is_called_between_retries(self):
        """asyncio.sleep should be called once per retry."""
        func = AsyncMock(side_effect=_Transient("x"))
        max_retries = 3
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=max_retries,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                )
        assert mock_sleep.await_count == max_retries

    @pytest.mark.asyncio
    async def test_sleep_not_called_when_no_retries(self):
        """If max_retries=0, asyncio.sleep must never be called."""
        func = AsyncMock(side_effect=_Transient("x"))
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=0,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                )
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delay_never_exceeds_max_delay(self):
        """Every sleep call should receive a value <= max_delay + 1 (jitter)."""
        func = AsyncMock(side_effect=_Transient("x"))
        max_delay = 2.0
        sleep_args: list[float] = []

        async def _capture_sleep(seconds: float) -> None:
            sleep_args.append(seconds)

        with patch("asyncio.sleep", side_effect=_capture_sleep):
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=5,
                    base_delay=1.0,
                    max_delay=max_delay,
                    retryable_exceptions=(_Transient,),
                )

        for s in sleep_args:
            # max_delay + 1.0 accounts for the uniform(0, 1) jitter
            assert s <= max_delay + 1.0, f"sleep({s}) exceeded max_delay cap"

    @pytest.mark.asyncio
    async def test_exponential_growth_before_cap(self):
        """First retry delay should be based on base_delay * 2^0 (before cap)."""
        func = AsyncMock(side_effect=_Transient("x"))
        sleep_args: list[float] = []

        async def _capture_sleep(seconds: float) -> None:
            sleep_args.append(seconds)

        base_delay = 1.0
        with patch("asyncio.sleep", side_effect=_capture_sleep):
            with patch("random.uniform", return_value=0.0):  # remove jitter
                with pytest.raises(_Transient):
                    await retry_with_backoff(
                        func,
                        max_retries=3,
                        base_delay=base_delay,
                        max_delay=100.0,
                        retryable_exceptions=(_Transient,),
                    )

        # attempt 0: delay = base_delay * 2^0 = 1.0
        # attempt 1: delay = base_delay * 2^1 = 2.0
        # attempt 2: delay = base_delay * 2^2 = 4.0
        assert sleep_args[0] == pytest.approx(1.0)
        assert sleep_args[1] == pytest.approx(2.0)
        assert sleep_args[2] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# on_retry callback
# ---------------------------------------------------------------------------

class TestOnRetryCallback:
    @pytest.mark.asyncio
    async def test_on_retry_called_with_attempt_and_exception(self):
        """on_retry callback receives (attempt_index, exception)."""
        exc = _Transient("boom")
        func = AsyncMock(side_effect=exc)
        on_retry = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=2,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                    on_retry=on_retry,
                )

        assert on_retry.call_count == 2
        # First retry: attempt=0
        first_call_args = on_retry.call_args_list[0][0]
        assert first_call_args[0] == 0
        assert first_call_args[1] is exc
        # Second retry: attempt=1
        second_call_args = on_retry.call_args_list[1][0]
        assert second_call_args[0] == 1

    @pytest.mark.asyncio
    async def test_on_retry_not_called_on_final_failure(self):
        """on_retry must NOT be called after the last failed attempt."""
        func = AsyncMock(side_effect=_Transient("x"))
        on_retry = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_Transient):
                await retry_with_backoff(
                    func,
                    max_retries=2,
                    base_delay=1.0,
                    retryable_exceptions=(_Transient,),
                    on_retry=on_retry,
                )

        # max_retries=2 → 3 total calls → 2 retries → on_retry called twice
        assert on_retry.call_count == 2

    @pytest.mark.asyncio
    async def test_logger_debug_used_when_no_on_retry(self, caplog):
        """When on_retry is None, a DEBUG log line should be emitted per retry."""
        func = AsyncMock(side_effect=_Transient("logged"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with caplog.at_level(logging.DEBUG, logger="src.core.retry_helper"):
                with pytest.raises(_Transient):
                    await retry_with_backoff(
                        func,
                        max_retries=1,
                        base_delay=1.0,
                        retryable_exceptions=(_Transient,),
                        on_retry=None,
                    )

        assert any("Retry" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_on_retry_suppresses_default_logging(self, caplog):
        """Providing on_retry should skip the default logger.debug call."""
        func = AsyncMock(side_effect=_Transient("silent"))
        on_retry = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with caplog.at_level(logging.DEBUG, logger="src.core.retry_helper"):
                with pytest.raises(_Transient):
                    await retry_with_backoff(
                        func,
                        max_retries=1,
                        base_delay=1.0,
                        retryable_exceptions=(_Transient,),
                        on_retry=on_retry,
                    )

        assert not any("Retry" in r.message for r in caplog.records)
