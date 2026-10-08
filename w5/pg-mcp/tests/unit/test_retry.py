"""Unit tests for the retry helper with exponential backoff."""


import pytest

from pg_mcp.resilience.retry import async_retry


class SleepRecorder:
    """Async sleep stand-in that records requested delays."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class TestAsyncRetry:
    """Tests for async_retry backoff behavior."""

    @pytest.mark.asyncio
    async def test_returns_first_success_without_sleep(self) -> None:
        """Successful first attempt does not sleep or retry."""
        sleep = SleepRecorder()

        async def op() -> int:
            return 42

        result = await async_retry(op, retries=3, delay=1.0, sleep=sleep)
        assert result == 42
        assert sleep.calls == []

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failures(self) -> None:
        """Retries matching errors and succeeds before exhausting attempts."""
        sleep = SleepRecorder()
        attempts = 0

        async def op() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("transient")
            return "ok"

        result = await async_retry(
            op,
            retries=3,
            delay=1.0,
            backoff_factor=2.0,
            retry_on=(ConnectionError,),
            sleep=sleep,
        )
        assert result == "ok"
        assert attempts == 3
        # Exponential backoff: 1.0, then 2.0
        assert sleep.calls == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_raises_original_error_when_exhausted(self) -> None:
        """The original error propagates after all retries fail."""
        sleep = SleepRecorder()
        attempts = 0

        async def op() -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("still down")

        with pytest.raises(ConnectionError, match="still down"):
            await async_retry(
                op,
                retries=2,
                delay=0.5,
                backoff_factor=2.0,
                retry_on=(ConnectionError,),
                sleep=sleep,
            )
        assert attempts == 3  # initial + 2 retries
        assert sleep.calls == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_non_matching_error_raises_immediately(self) -> None:
        """Errors outside retry_on propagate without any retry."""
        sleep = SleepRecorder()
        attempts = 0

        async def op() -> None:
            nonlocal attempts
            attempts += 1
            raise ValueError("not transient")

        with pytest.raises(ValueError, match="not transient"):
            await async_retry(
                op,
                retries=5,
                delay=1.0,
                retry_on=(ConnectionError,),
                sleep=sleep,
            )
        assert attempts == 1
        assert sleep.calls == []

    @pytest.mark.asyncio
    async def test_zero_retries_single_attempt(self) -> None:
        """retries=0 performs exactly one attempt."""
        sleep = SleepRecorder()
        attempts = 0

        async def op() -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await async_retry(
                op,
                retries=0,
                delay=1.0,
                retry_on=(ConnectionError,),
                sleep=sleep,
            )
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_invalid_arguments_rejected(self) -> None:
        """Negative retries/delay are rejected upfront."""

        async def op() -> None:
            return None

        with pytest.raises(ValueError, match="retries"):
            await async_retry(op, retries=-1, delay=1.0)
        with pytest.raises(ValueError, match="delay"):
            await async_retry(op, retries=1, delay=-0.5)
