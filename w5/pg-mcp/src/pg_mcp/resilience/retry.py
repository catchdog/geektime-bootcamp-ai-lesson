"""Retry with exponential backoff for transient failures.

This module provides a generic async retry helper used by the SQL executor
(transient database errors) and available to any caller that needs
exponential backoff between attempts.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

# Clock/sleep injection point for tests
SleepFunc = Callable[[float], Awaitable[None]]


async def async_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: SleepFunc = asyncio.sleep,
) -> T:
    """Run an async operation with exponential backoff retries.

    The operation is invoked once plus up to `retries` additional times.
    Only exceptions matching `retry_on` trigger a retry; anything else
    propagates immediately. The wait before the n-th retry is
    ``delay * backoff_factor ** (n - 1)``.

    Args:
        operation: Zero-argument callable returning an awaitable.
        retries: Number of retries after the initial attempt (0 = no retry).
        delay: Base delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after each failure.
        retry_on: Exception types that should be retried.
        sleep: Sleep function (injectable for tests).

    Returns:
        T: The operation's result.

    Raises:
        Exception: The last observed exception when all attempts fail, or
            any non-matching exception immediately.

    Example:
        >>> result = await async_retry(
        ...     lambda: fetch_rows(sql),
        ...     retries=3,
        ...     delay=1.0,
        ...     backoff_factor=2.0,
        ...     retry_on=(ConnectionFailureError,),
        ... )
    """
    if retries < 0:
        raise ValueError("retries must be >= 0")
    if delay < 0:
        raise ValueError("delay must be >= 0")

    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await operation()
        except retry_on as e:
            last_error = e
            if attempt >= retries:
                raise
            wait = delay * (backoff_factor**attempt)
            await sleep(wait)
    # Unreachable: the loop either returns or raises
    raise AssertionError(f"retry loop exited unexpectedly: {last_error!r}")
