"""DuckDB connection lifecycle — singleton per workbook."""

from __future__ import annotations

import duckdb

_connections: dict[str, duckdb.DuckDBPyConnection] = {}


def get_connection(workbook_path: str | None = None) -> duckdb.DuckDBPyConnection:
    """Return (or create) the DuckDB connection for a workbook.

    Parameters
    ----------
    workbook_path:
        Full path to the Excel workbook. If None, uses a default
        in-memory connection keyed as "__default__".
    """
    key = workbook_path or "__default__"
    if key not in _connections:
        con = duckdb.connect()
        _init_catalog(con)
        _connections[key] = con
    return _connections[key]


def close_connection(workbook_path: str | None = None) -> None:
    """Close and remove the connection for a workbook."""
    key = workbook_path or "__default__"
    con = _connections.pop(key, None)
    if con is not None:
        con.close()


def close_all() -> None:
    """Close all managed connections."""
    for con in _connections.values():
        con.close()
    _connections.clear()


def _init_catalog(con: duckdb.DuckDBPyConnection) -> None:
    """Create blobrange catalog tables on a fresh connection."""
    con.execute("CREATE SCHEMA IF NOT EXISTS blobrange")
    con.execute("""
        CREATE TABLE IF NOT EXISTS blobrange.resolved_objects (
            object_name     TEXT,
            object_type     TEXT,
            workbook_path   TEXT,
            worksheet_name  TEXT,
            inferred_schema JSON,
            resolved_at     TIMESTAMPTZ DEFAULT now(),
            row_count       INTEGER
        )
    """)
