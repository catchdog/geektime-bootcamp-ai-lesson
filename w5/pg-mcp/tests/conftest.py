"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics for tests to avoid port conflicts."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]


@pytest.fixture
def mock_openai_response():
    """Factory for mocked OpenAI chat completions returning SQL."""
    from unittest.mock import MagicMock

    def _make(sql: str, tokens: int | None = None):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=f"```sql\n{sql}\n```"))]
        if tokens is None:
            response.usage = None
        else:
            response.usage = MagicMock(total_tokens=tokens)
        return response

    return _make


@pytest.fixture
def mock_asyncpg_pool():
    """A mocked asyncpg pool yielding a connection with canned fetch results."""
    from unittest.mock import AsyncMock, MagicMock

    pool = MagicMock()
    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"id": 1, "name": "alice"}])
    connection.execute = AsyncMock(return_value="SET")
    pool.acquire = AsyncMock(return_value=connection)
    pool.get_size.return_value = 1
    return pool
