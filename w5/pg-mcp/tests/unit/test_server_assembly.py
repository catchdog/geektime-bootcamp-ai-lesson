"""Unit tests for server assembly (lifespan) and the /health endpoint.

The lifespan is exercised with patched pool creation and schema loading so
no real PostgreSQL or OpenAI service is needed.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pg_mcp.server as server_module
from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.server import (
    health_check,
    lifespan,
    mcp,
)


@pytest.fixture
def reset_server_globals():
    """Snapshot and restore server module globals around each test."""
    snapshot = (
        server_module._settings,
        server_module._pools,
        server_module._schema_cache,
        server_module._orchestrator,
        server_module._rate_limiter,
        server_module._started_at,
    )
    yield
    (
        server_module._settings,
        server_module._pools,
        server_module._schema_cache,
        server_module._orchestrator,
        server_module._rate_limiter,
        server_module._started_at,
    ) = snapshot


@pytest.mark.asyncio
async def test_lifespan_wires_multi_database_executors(reset_server_globals) -> None:
    """Lifespan creates one pool and one executor per configured database."""
    fake_pools = {"db_a": MagicMock(), "db_b": MagicMock()}
    for p in fake_pools.values():
        p.get_size.return_value = 1

    schema = DatabaseSchema(database_name="db_a", tables=[], version="15.0")

    with (
        patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "sk-test-assembly",
                "DATABASES": '[{"name":"db_a","host":"h"},{"name":"db_b","host":"h"}]',
            },
        ),
        patch.object(
            server_module,
            "create_pools",
            new=AsyncMock(return_value=fake_pools),
        ) as mock_create_pools,
        patch.object(
            server_module.SchemaCache, "load", new=AsyncMock(return_value=schema)
        ),
    ):
        async with lifespan(mcp):
            # Pools created for every configured database
            mock_create_pools.assert_awaited_once()
            configs = mock_create_pools.await_args.args[0]
            assert isinstance(configs, list)
            assert [c.name for c in configs] == ["db_a", "db_b"]
            assert all(isinstance(c, DatabaseConfig) for c in configs)

            assert server_module._pools == fake_pools
            assert server_module._orchestrator is not None
            # Executors exist for every database
            for db_name in fake_pools:
                assert db_name in server_module._orchestrator.executors
            # Rate limiter uses configured defaults
            assert server_module._rate_limiter is not None
            assert (
                server_module._rate_limiter.query_limiter.max_concurrent
                == server_module._settings.resilience.max_concurrent_queries
            )

    # Shutdown closed the pools (they were MagicMock pools; close_pools
    # tolerates their close() being MagicMock attributes)


@pytest.mark.asyncio
async def test_health_endpoint_reports_components(reset_server_globals) -> None:
    """The /health handler reports pools, cache age, breaker state, uptime."""
    fake_pool = MagicMock()
    fake_pool.get_size.return_value = 3
    server_module._pools = {"db_a": fake_pool}

    cache = MagicMock()
    cache.get_cache_age.return_value = 42.5
    server_module._schema_cache = cache

    orchestrator = MagicMock()
    orchestrator.circuit_breaker = CircuitBreaker()
    server_module._orchestrator = orchestrator
    server_module._started_at = time.monotonic() - 10

    response = await health_check(None)  # type: ignore[arg-type]
    payload = json.loads(response.body)

    assert payload["status"] == "ok"
    assert payload["databases"]["db_a"]["pool_size"] == 3
    assert payload["databases"]["db_a"]["cache_age_seconds"] == 42.5
    assert payload["circuit_breaker"] == "closed"
    assert payload["uptime_seconds"] >= 10


@pytest.mark.asyncio
async def test_health_endpoint_before_initialization(reset_server_globals) -> None:
    """Health reports 'initializing' before the lifespan has run."""
    server_module._pools = None
    server_module._orchestrator = None
    server_module._started_at = None

    response = await health_check(None)  # type: ignore[arg-type]
    payload = json.loads(response.body)

    assert payload["status"] == "initializing"
    assert payload["databases"] == {}
    assert payload["circuit_breaker"] is None
    assert payload["uptime_seconds"] is None


def test_mcp_server_has_health_route() -> None:
    """The custom /health route is registered on the FastMCP app."""
    # custom_route registers Starlette routes in _custom_starlette_routes
    routes = [getattr(r, "path", None) for r in mcp._custom_starlette_routes]
    assert "/health" in routes
