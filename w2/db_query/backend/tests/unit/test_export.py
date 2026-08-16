"""Unit tests for query result export (service + API endpoint)."""

import csv
import io
import json
import re
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.database import get_session
from app.models.database import DatabaseConnection, ConnectionStatus
from app.models.schemas import QueryColumn, QueryResult
from app.services.export import (
    ExportFormat,
    build_content_disposition,
    build_export_filename,
    export_result,
    export_to_csv,
    export_to_json,
)


def make_result() -> QueryResult:
    """Build a sample QueryResult covering tricky value types."""
    return QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="varchar"),
            QueryColumn(name="score", dataType="numeric"),
        ],
        rows=[
            {"id": 1, "name": "Alice", "score": Decimal("95.5")},
            {"id": 2, "name": None, "score": None},
        ],
        rowCount=2,
        executionTimeMs=12,
        sql="SELECT id, name, score FROM students",
    )


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

class TestExportToCsv:
    def test_basic_content(self):
        text = export_to_csv(make_result())
        lines = text.splitlines()
        assert lines[0] == "id,name,score"
        assert lines[1] == "1,Alice,95.5"
        # None cells become empty fields
        assert lines[2] == "2,,"

    def test_special_characters_are_escaped(self):
        result = QueryResult(
            columns=[QueryColumn(name="note", dataType="text")],
            rows=[
                {"note": "comma, quote\" and\nnewline"},
                {"note": "中文内容"},
            ],
            rowCount=2,
            executionTimeMs=1,
            sql="SELECT note FROM logs",
        )
        rows = list(csv.reader(io.StringIO(export_to_csv(result))))
        assert rows[0] == ["note"]
        assert rows[1] == ['comma, quote" and\nnewline']
        assert rows[2] == ["中文内容"]


class TestExportToJson:
    def test_document_structure(self):
        document = json.loads(export_to_json(make_result()))
        assert document["rowCount"] == 2
        assert document["sql"] == "SELECT id, name, score FROM students"
        assert document["columns"][0] == {"name": "id", "dataType": "integer"}
        assert document["rows"][0] == {"id": 1, "name": "Alice", "score": "95.5"}
        # nulls survive instead of becoming empty strings
        assert document["rows"][1] == {"id": 2, "name": None, "score": None}

    def test_chinese_not_escaped_and_datetime_serialized(self):
        result = QueryResult(
            columns=[QueryColumn(name="城市", dataType="varchar")],
            rows=[{"城市": "北京", "created": datetime(2026, 8, 16, 10, 30, 0)}],
            rowCount=1,
            executionTimeMs=1,
            sql="SELECT 1",
        )
        text = export_to_json(result)
        assert "北京" in text  # ensure_ascii=False
        document = json.loads(text)
        assert document["rows"][0]["created"] == "2026-08-16T10:30:00"


class TestHelpers:
    def test_build_export_filename(self):
        name = build_export_filename("test db!", ExportFormat.CSV, datetime(2026, 8, 16, 10, 30, 0))
        # unsafe chars -> '_', trailing ones are stripped
        assert name == "query_test_db_20260816_103000.csv"

    def test_build_content_disposition_ascii_and_non_ascii(self):
        assert build_content_disposition("result.csv") == (
            'attachment; filename="result.csv"; filename*=UTF-8\'\'result.csv'
        )
        header = build_content_disposition("结果.csv")
        assert "filename*=UTF-8''%E7%BB%93%E6%9E%9C.csv" in header

    def test_dispatch(self):
        assert export_result(make_result(), ExportFormat.CSV).startswith("id,name")
        assert json.loads(export_result(make_result(), ExportFormat.JSON))["rowCount"] == 2

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError):
            export_result(make_result(), "xlsx")


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

@pytest.fixture
def test_session():
    """Create an in-memory SQLite session for testing."""
    engine = create_engine(
        "sqlite:///file:test_export_db?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    """Create TestClient with test database session."""

    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    """Create a sample database connection."""
    conn = DatabaseConnection(
        name="test_db",
        url="postgresql://user:pass@localhost/testdb",
        description="Test database",
        status=ConnectionStatus.ACTIVE,
    )
    test_session.add(conn)
    test_session.commit()
    test_session.refresh(conn)
    return conn


def mock_query_result():
    """AsyncMock return value shaped like the real execution pipeline."""
    return make_result()


class TestExportEndpoint:
    def test_export_csv(self, client, sample_connection):
        with patch(
            "app.api.v1.queries.execute_query_with_service",
            new=AsyncMock(return_value=mock_query_result()),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export?format=csv",
                json={"sql": "SELECT id, name, score FROM students"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        # utf-8-sig BOM so Excel renders Chinese correctly
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert response.text.lstrip("﻿").splitlines()[0] == "id,name,score"

    def test_export_json(self, client, sample_connection):
        with patch(
            "app.api.v1.queries.execute_query_with_service",
            new=AsyncMock(return_value=mock_query_result()),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export?format=json",
                json={"sql": "SELECT id, name, score FROM students"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        document = response.json()
        assert document["rowCount"] == 2
        assert document["rows"][0]["name"] == "Alice"

    def test_default_format_is_csv(self, client, sample_connection):
        with patch(
            "app.api.v1.queries.execute_query_with_service",
            new=AsyncMock(return_value=mock_query_result()),
        ):
            response = client.post(
                "/api/v1/dbs/test_db/query/export",
                json={"sql": "SELECT 1"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    def test_invalid_format_rejected(self, client, sample_connection):
        response = client.post(
            "/api/v1/dbs/test_db/query/export?format=xlsx",
            json={"sql": "SELECT 1"},
        )
        assert response.status_code == 422

    def test_unknown_database_returns_404(self, client):
        response = client.post(
            "/api/v1/dbs/no_such_db/query/export?format=csv",
            json={"sql": "SELECT 1"},
        )
        assert response.status_code == 404
