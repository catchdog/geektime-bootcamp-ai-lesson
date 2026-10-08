"""Unit tests verifying observability wiring in the request path.

These tests exercise the orchestrator with mocked services and assert that
Prometheus metrics actually change, proving the metrics are wired into the
request flow (not merely defined).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.models.query import QueryRequest, ResultValidationResult, ReturnType
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_executor import SQLExecutor
from tests.unit.test_orchestrator import make_orchestrator


def sample(name: str, **labels: str) -> float | None:
    """Read a sample value from the default Prometheus registry."""
    return REGISTRY.get_sample_value(name, labels)


def mock_schema() -> DatabaseSchema:
    """Build a minimal schema for orchestrator mocks."""
    return DatabaseSchema(database_name="test_db", tables=[], version="15.0")


@pytest.fixture
def schema() -> DatabaseSchema:
    """Fresh schema per test."""
    return mock_schema()


def wired_orchestrator(
    schema: DatabaseSchema,
    *,
    validator: SecurityViolationError | None = None,
    rate_limiter: MultiRateLimiter | None = None,
) -> QueryOrchestrator:
    """Orchestrator with mock services and cache age reporting enabled."""
    cache = MagicMock()
    cache.get.return_value = schema
    cache.get_cache_age.return_value = 12.0

    generator = AsyncMock()
    if validator is not None:
        generator.generate.return_value = ("DELETE FROM users;", None)
        generator_validator = MagicMock()
        generator_validator.validate_or_raise.side_effect = validator
    else:
        generator.generate.return_value = ("SELECT 1;", 7)
        generator_validator = MagicMock()
        generator_validator.validate_or_raise.return_value = None

    result_validator = AsyncMock()
    result_validator.validate.return_value = ResultValidationResult(
        confidence=95, explanation="ok", suggestion=None, is_acceptable=True,
        tokens_used=5,
    )

    return make_orchestrator(
        sql_generator=generator,
        sql_validator=generator_validator,
        result_validator=result_validator,
        schema_cache=cache,
        resilience_config=ResilienceConfig(max_retries=1),
        validation_config=ValidationConfig(enabled=True),
        rate_limiter=rate_limiter,
    )


class TestRequestMetrics:
    """Query request counter and duration histogram wiring."""

    @pytest.mark.asyncio
    async def test_success_increments_request_counter(self, schema: DatabaseSchema) -> None:
        """Successful requests bump pg_mcp_query_requests_total{status=success}."""
        before = sample(
            "pg_mcp_query_requests_total", status="success", database="test_db"
        ) or 0.0

        orchestrator = wired_orchestrator(schema)
        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.SQL
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        after = sample(
            "pg_mcp_query_requests_total", status="success", database="test_db"
        ) or 0.0
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_security_rejection_recorded(self, schema: DatabaseSchema) -> None:
        """Rejected SQL bumps both the request and sql_rejected counters."""
        status_before = sample(
            "pg_mcp_query_requests_total",
            status="security_violation",
            database="test_db",
        ) or 0.0
        rejected_before = sample(
            "pg_mcp_sql_rejected_total", reason="security_violation"
        ) or 0.0

        orchestrator = wired_orchestrator(
            schema, validator=SecurityViolationError("DELETE not allowed")
        )
        request = QueryRequest(question="test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        status_after = sample(
            "pg_mcp_query_requests_total",
            status="security_violation",
            database="test_db",
        ) or 0.0
        rejected_after = sample(
            "pg_mcp_sql_rejected_total", reason="security_violation"
        ) or 0.0
        assert status_after == status_before + 1
        assert rejected_after == rejected_before + 1

    @pytest.mark.asyncio
    async def test_unknown_database_label_on_early_failure(self) -> None:
        """Failures during database resolution use the 'unknown' label."""
        orchestrator = make_orchestrator(pools={})  # no databases configured
        request = QueryRequest(question="test", database="some_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        after = sample(
            "pg_mcp_query_requests_total",
            status="database_error",
            database="unknown",
        ) or 0.0
        assert after >= 1

    @pytest.mark.asyncio
    async def test_schema_cache_age_gauge_set(self, schema: DatabaseSchema) -> None:
        """The cache age gauge reflects the value reported by the cache."""
        orchestrator = wired_orchestrator(schema)
        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.SQL
        )
        await orchestrator.execute_query(request)

        age = sample("pg_mcp_schema_cache_age_seconds", database="test_db")
        assert age == 12.0


class TestRateLimiterMetricsInteraction:
    """Rate limiting integration produces request-level metrics."""

    @pytest.mark.asyncio
    async def test_rate_limited_requests_use_real_limiter(self, schema: DatabaseSchema) -> None:
        """A MultiRateLimiter with zero free LLM slots yields RATE_LIMITED."""
        limiter = MultiRateLimiter(query_limit=5, llm_limit=1)
        # Exhaust the single LLM slot
        async with limiter.for_llm():
            orchestrator = wired_orchestrator(schema, rate_limiter=limiter)
            request = QueryRequest(question="test", database="test_db")
            response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        stats = limiter.get_all_stats()
        assert stats["llm"]["total_rejections"] >= 1


class TestExecutorMetrics:
    """SQL executor duration wiring."""

    @pytest.mark.asyncio
    async def test_db_query_duration_observed(self) -> None:
        """Executing through the executor records a duration sample."""

        class _AsyncCM:
            async def __aenter__(self) -> object:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        class _ConnCtx:
            def __init__(self, conn: object) -> None:
                self._conn = conn

            async def __aenter__(self) -> object:
                return self._conn

            async def __aexit__(self, *args: object) -> None:
                return None

        connection = MagicMock()
        connection.fetch = AsyncMock(return_value=[{"id": 1}])
        connection.execute = AsyncMock(return_value="SET")
        connection.transaction.return_value = _AsyncCM()

        pool = MagicMock()
        pool.acquire.return_value = _ConnCtx(connection)

        security = MagicMock(
            max_execution_time=5.0,
            max_rows=10,
            safe_search_path="public",
            readonly_role=None,
        )
        executor = SQLExecutor(
            pool=pool,
            security_config=security,  # type: ignore[arg-type]
            db_config=MagicMock(),  # type: ignore[arg-type]
        )

        before = REGISTRY.get_sample_value("pg_mcp_db_query_duration_seconds_count") or 0.0
        results, total = await executor.execute("SELECT 1")
        assert total == 1
        assert results == [{"id": 1}]

        after = REGISTRY.get_sample_value("pg_mcp_db_query_duration_seconds_count") or 0.0
        assert after == before + 1
