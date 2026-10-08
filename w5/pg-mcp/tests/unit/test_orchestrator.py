"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline,
including retry logic, error handling, integration with all components,
multi-database executor routing, rate limiting, and request tracing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMError,
    RateLimitExceededError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.services.orchestrator import QueryOrchestrator


def make_orchestrator(
    *,
    sql_generator: object | None = None,
    sql_validator: object | None = None,
    executors: dict | None = None,
    result_validator: object | None = None,
    schema_cache: object | None = None,
    pools: dict | None = None,
    resilience_config: ResilienceConfig | None = None,
    validation_config: ValidationConfig | None = None,
    rate_limiter: object | None = None,
) -> QueryOrchestrator:
    """Build a QueryOrchestrator with mock defaults (0008 factory pattern)."""
    pools = pools if pools is not None else {"test_db": MagicMock()}
    executors = executors if executors is not None else {
        name: MagicMock() for name in pools
    }
    return QueryOrchestrator(
        sql_generator=sql_generator if sql_generator is not None else MagicMock(),
        sql_validator=sql_validator if sql_validator is not None else MagicMock(),
        executors=executors,  # type: ignore[arg-type]
        result_validator=(
            result_validator if result_validator is not None else MagicMock()
        ),
        schema_cache=schema_cache if schema_cache is not None else MagicMock(),
        pools=pools,  # type: ignore[arg-type]
        resilience_config=(
            resilience_config if resilience_config is not None else ResilienceConfig()
        ),
        validation_config=(
            validation_config if validation_config is not None else ValidationConfig()
        ),
        rate_limiter=rate_limiter,  # type: ignore[arg-type]
    )


class TestDatabaseResolution:
    """Test database name resolution logic."""

    @pytest.fixture
    def orchestrator(self) -> QueryOrchestrator:
        """Create orchestrator with mocked components."""
        return make_orchestrator(pools={"db1": MagicMock(), "db2": MagicMock()})

    def test_resolve_database_specified_valid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified valid database."""
        result = orchestrator._resolve_database("db1")
        assert result == "db1"

    def test_resolve_database_specified_invalid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified but invalid database."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = make_orchestrator(pools={"only_db": MagicMock()})

        result = orchestrator._resolve_database(None)
        assert result == "only_db"

    def test_resolve_database_auto_select_multiple_fails(
        self, orchestrator: QueryOrchestrator
    ) -> None:
        """Test that auto-select fails when multiple databases available."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "multiple databases" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = make_orchestrator(pools={})

        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()


class TestExecutorRouting:
    """Test per-database executor selection."""

    def test_resolve_database_uses_pools_not_executors(self) -> None:
        """Resolution is based on the pool map, not the executor map."""
        orchestrator = make_orchestrator(
            pools={"db1": MagicMock()},
            executors={},  # intentionally empty
        )
        assert orchestrator._resolve_database("db1") == "db1"


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(self, mock_schema: DatabaseSchema) -> None:
        """Test successful SQL generation on first attempt."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", 150)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None  # No exception = valid

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            resilience_config=ResilienceConfig(max_retries=3),
        )

        # Execute
        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert tokens == 150
        assert validation_result.is_valid is True
        assert validation_result.is_select is True
        mock_generator.generate.assert_called_once()
        mock_validator.validate_or_raise.assert_called_once_with("SELECT * FROM users;")

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test retry logic when validation fails."""
        # Setup mocks - first attempt fails validation, second succeeds
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = [
            ("SELECT * FROM user;", 100),  # First attempt (wrong table name)
            ("SELECT * FROM users;", 120),  # Second attempt (correct)
        ]

        mock_validator = MagicMock()
        # First call raises error, second call succeeds
        mock_validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            None,  # Success on second attempt
        ]

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            resilience_config=ResilienceConfig(max_retries=3),
        )

        # Execute
        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        # Tokens accumulate across attempts
        assert tokens == 220
        assert mock_generator.generate.call_count == 2
        assert mock_validator.validate_or_raise.call_count == 2

        # Verify retry included error feedback
        second_call = mock_generator.generate.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(self, mock_schema: DatabaseSchema) -> None:
        """Test failure after exhausting all retries."""
        # Setup mocks - all attempts fail validation
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", None)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            resilience_config=ResilienceConfig(max_retries=2),
        )

        # Execute and verify exception
        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # Should attempt max_retries + 1 times (initial + retries)
        assert mock_generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(self, mock_schema: DatabaseSchema) -> None:
        """Test that open circuit breaker prevents SQL generation."""
        orchestrator = make_orchestrator(
            sql_generator=AsyncMock(),
            resilience_config=ResilienceConfig(circuit_breaker_threshold=1),
        )

        # Manually open the circuit breaker
        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        # Attempt should fail immediately
        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_sql_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors during generation."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            resilience_config=ResilienceConfig(max_retries=1),
        )

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "unexpectedly" in str(exc_info.value).lower()
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_rate_limited(self, mock_schema: DatabaseSchema) -> None:
        """Test that an exhausted LLM slot raises RateLimitExceededError."""

        class _FullLimiter:
            """Rate limiter whose LLM slots are all taken."""

            @asynccontextmanager
            async def for_llm(self, *, timeout: float | None = None) -> AsyncIterator[None]:  # noqa: ASYNC109
                raise TimeoutError("Rate limiter timeout exceeded")
                yield  # pragma: no cover

        mock_generator = AsyncMock()
        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            rate_limiter=_FullLimiter(),
        )

        with pytest.raises(RateLimitExceededError):
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
            )
        mock_generator.generate.assert_not_called()


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        mock_validator = AsyncMock()
        mock_validator.validate.return_value = ResultValidationResult(
            confidence=85,
            explanation="Results match the question well",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            result_validator=mock_validator,
            validation_config=ValidationConfig(enabled=True),
        )

        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation.confidence == 85
        assert validation.is_acceptable is True
        mock_validator.validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        mock_validator = AsyncMock()

        orchestrator = make_orchestrator(
            result_validator=mock_validator,
            validation_config=ValidationConfig(enabled=False),
        )

        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation.confidence == 100
        assert validation.is_acceptable is True
        mock_validator.validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        mock_validator = AsyncMock()
        mock_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = make_orchestrator(
            result_validator=mock_validator,
            validation_config=ValidationConfig(enabled=True),
        )

        # Should not raise, returns default confidence
        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation.confidence == 100
        assert validation.is_acceptable is True


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=SQL."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", 42)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            schema_cache=mock_cache,
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None  # No execution for SQL-only
        assert response.error is None
        assert response.tokens_used == 42
        assert response.request_id is not None

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=RESULT."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT id, name FROM users;", 50)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.pool = MagicMock()
        mock_executor.pool.get_size.return_value = 1
        mock_executor.execute.return_value = (
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            2,  # total count
        )

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="Good results",
            suggestion=None,
            is_acceptable=True,
            tokens_used=30,
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            validation_config=ValidationConfig(enabled=True),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert len(response.data.rows) == 2
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.error is None
        # Tokens accumulate: generation (50) + result validation (30)
        assert response.tokens_used == 80

    @pytest.mark.asyncio
    async def test_execute_query_routes_to_requested_executor(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Requests naming a database run on that database's executor."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        executor_a = AsyncMock()
        executor_a.execute.return_value = ([{"db": "a"}], 1)
        executor_b = AsyncMock()
        executor_b.pool = MagicMock()
        executor_b.pool.get_size.return_value = 1
        executor_b.execute.return_value = ([{"db": "b"}], 1)

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None
        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=95, explanation="ok", suggestion=None, is_acceptable=True
        )

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"db_a": executor_a, "db_b": executor_b},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            pools={"db_a": MagicMock(), "db_b": MagicMock()},
            validation_config=ValidationConfig(enabled=True),
        )

        request = QueryRequest(
            question="test", database="db_b", return_type=ReturnType.RESULT
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        executor_b.execute.assert_awaited_once()
        executor_a.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_query_missing_executor_returns_connection_error(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """A resolved database without an executor yields a connection error."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={},  # no executor bound
            schema_cache=mock_cache,
            pools={"orphan_db": MagicMock()},
        )

        request = QueryRequest(
            question="test", database="orphan_db", return_type=ReturnType.RESULT
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_connection_error"

    @pytest.mark.asyncio
    async def test_execute_query_rate_limited(self, mock_schema: DatabaseSchema) -> None:
        """LLM rate limiting surfaces as a rate_limit_exceeded error response."""

        class _FullLimiter:
            @asynccontextmanager
            async def for_llm(self, *, timeout: float | None = None) -> AsyncIterator[None]:  # noqa: ASYNC109
                raise TimeoutError("Rate limiter timeout exceeded")
                yield  # pragma: no cover

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=AsyncMock(),
            schema_cache=mock_cache,
            rate_limiter=_FullLimiter(),
        )

        request = QueryRequest(question="test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"

    @pytest.mark.asyncio
    async def test_execute_query_low_confidence_rejected(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Results the validator marks unacceptable produce LOW_CONFIDENCE."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.pool = MagicMock()
        mock_executor.pool.get_size.return_value = 1
        mock_executor.execute.return_value = ([{"count": 1}], 1)

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=20,
            explanation="Results do not answer the question",
            suggestion="Refine the question",
            is_acceptable=False,
        )

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            validation_config=ValidationConfig(enabled=True),
        )

        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.RESULT
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "low_confidence"

    @pytest.mark.asyncio
    async def test_execute_query_low_confidence_warning(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Confidence below min_confidence_score attaches a warning, not an error."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.pool = MagicMock()
        mock_executor.pool.get_size.return_value = 1
        mock_executor.execute.return_value = ([{"count": 1}], 1)

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=75,
            explanation="Mostly matching results",
            suggestion="Check the filter",
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            validation_config=ValidationConfig(
                enabled=True,
                confidence_threshold=70,  # validator accepts 75
                min_confidence_score=80,  # orchestrator warns below 80
            ),
        )

        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.RESULT
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.confidence == 75
        assert response.warning is not None
        assert "75%" in response.warning
        assert "Check the filter" in response.warning

    @pytest.mark.asyncio
    async def test_execute_query_no_warning_above_threshold(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Confidence at/above min_confidence_score produces no warning."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.pool = MagicMock()
        mock_executor.pool.get_size.return_value = 1
        mock_executor.execute.return_value = ([{"count": 1}], 1)

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=95, explanation="great", suggestion=None, is_acceptable=True
        )

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            validation_config=ValidationConfig(enabled=True),
        )

        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.RESULT
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.warning is None

    @pytest.mark.asyncio
    async def test_execute_query_propagates_request_id(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """request_id is bound to the tracing context during the pipeline."""
        captured: dict[str, str | None] = {"context_id": None}

        async def generating(*args: object, **kwargs: object) -> tuple[str, int | None]:
            captured["context_id"] = get_request_id()
            return ("SELECT 1;", None)

        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = generating
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            schema_cache=mock_cache,
        )

        request = QueryRequest(
            question="test", database="test_db", return_type=ReturnType.SQL
        )
        response = await orchestrator.execute_query(request)

        assert response.request_id is not None
        assert captured["context_id"] == response.request_id
        # Context is unbound after the request completes
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # Not in cache
        mock_cache.load = AsyncMock(return_value=mock_schema)
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_pool = MagicMock()

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            schema_cache=mock_cache,
            pools={"test_db": mock_pool},
        )

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify schema was loaded
        mock_cache.load.assert_called_once_with("test_db", mock_pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = make_orchestrator(schema_cache=mock_cache)

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self) -> None:
        """Test handling of SQL validation errors."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", None)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            schema_cache=mock_cache,
            resilience_config=ResilienceConfig(max_retries=1),
        )

        # Execute
        request = QueryRequest(
            question="Delete all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of SQL execution errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", None)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.pool = MagicMock()
        mock_executor.pool.get_size.return_value = 1
        mock_executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            schema_cache=mock_cache,
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(schema_cache=mock_cache)

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self, mock_schema: DatabaseSchema) -> None:
        """Test auto-selecting database when only one available."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema
        mock_cache.get_cache_age.return_value = None

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", None)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            schema_cache=mock_cache,
            pools={"only_db": MagicMock()},  # Only one database
        )

        # Execute without specifying database
        request = QueryRequest(
            question="Test query",
            database=None,  # No database specified
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        # Verify schema was fetched for auto-selected database
        mock_cache.get.assert_called_once_with("only_db")
