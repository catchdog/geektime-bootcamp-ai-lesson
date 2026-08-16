"""Query result export service (CSV / JSON).

Implemented with the Python standard library only (csv + json), so no new
runtime dependencies are introduced. The exported content is generated as an
in-memory string which the API layer wraps into a downloadable response.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from urllib.parse import quote

from app.models.schemas import QueryColumn, QueryResult

# UTF-8 BOM makes Excel open CSV files with Chinese characters correctly
CSV_ENCODING = "utf-8-sig"
JSON_ENCODING = "utf-8"


class ExportFormat(str, Enum):
    """Supported export formats."""

    CSV = "csv"
    JSON = "json"


EXPORT_MEDIA_TYPES: dict[ExportFormat, str] = {
    ExportFormat.CSV: "text/csv; charset=utf-8",
    ExportFormat.JSON: "application/json; charset=utf-8",
}


def _sanitize_cell(value: Any) -> Any:
    """Convert a single cell value into a CSV/JSON friendly primitive.

    - None        -> "" for CSV, null for JSON (handled by the writers)
    - datetime    -> ISO-8601 string
    - Decimal     -> plain string (keeps precision)
    - bytes       -> lossless hex representation
    - other       -> returned untouched (str/int/float/bool)
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def export_to_csv(result: QueryResult) -> str:
    """Serialize a QueryResult to CSV text.

    Empty values are written as empty cells (NULL-safe), values containing
    commas / quotes / newlines are escaped by the csv module automatically.
    """
    headers = [col.name for col in result.columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in result.rows:
        # None cells are written as empty strings by the csv writer (NULL-safe)
        writer.writerow([_sanitize_cell(row.get(col)) for col in headers])
    return buffer.getvalue()


def export_to_json(result: QueryResult) -> str:
    """Serialize a QueryResult to a structured JSON document.

    The document keeps query metadata (SQL, row count, columns) alongside the
    rows so the export is self-describing and can be re-imported later.
    """
    document = {
        "columns": [
            {"name": col.name, "dataType": col.data_type} for col in result.columns
        ],
        "rowCount": result.row_count,
        "executionTimeMs": result.execution_time_ms,
        "sql": result.sql,
        "rows": [
            {key: _sanitize_cell(value) for key, value in row.items()}
            for row in result.rows
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2, default=str)


def build_export_filename(database_name: str, fmt: ExportFormat, now: datetime) -> str:
    """Build a filesystem-safe export filename.

    Example: query_test_db_20260816_103000.csv
    """
    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in database_name
    ).strip("_") or "db"
    return f"query_{safe_name}_{now.strftime('%Y%m%d_%H%M%S')}.{fmt.value}"


def build_content_disposition(filename: str) -> str:
    """Build a Content-Disposition header supporting non-ASCII filenames."""
    return f"attachment; filename=\"{quote(filename)}\"; filename*=UTF-8''{quote(filename)}"


def export_result(result: QueryResult, fmt: ExportFormat) -> str:
    """Dispatch to the format-specific exporter."""
    if fmt == ExportFormat.CSV:
        return export_to_csv(result)
    if fmt == ExportFormat.JSON:
        return export_to_json(result)
    raise ValueError(f"Unsupported export format: {fmt}")
