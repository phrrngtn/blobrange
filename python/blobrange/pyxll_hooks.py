"""PyXLL entry points — xl_func and xl_macro registrations.

This module is loaded by PyXLL when the workbook opens. It registers
Excel-callable functions that expose blobrange's query capabilities.

Requires PyXLL to be installed and configured.
"""

from __future__ import annotations

from typing import Any

try:
    from pyxll import xl_app, xl_func, xl_macro
    HAS_PYXLL = True
except ImportError:
    HAS_PYXLL = False

if HAS_PYXLL:
    import duckdb

    from blobrange.connection import get_connection, close_connection
    from blobrange.resolver import resolve_and_execute

    @xl_func("str sql: object", macro=True)
    def blobrange_query(sql: str) -> list[list[Any]]:
        """Execute a SQL query with Excel object resolution.

        Table names in the query that aren't in the DuckDB catalog are
        resolved against ListObjects, named ranges, and LAMBDAs in the
        active workbook.

        Returns a 2D array suitable for spilling into the worksheet.
        """
        app = xl_app()
        wb_path = str(app.ActiveWorkbook.FullName) if app.ActiveWorkbook else None
        con = get_connection(wb_path)

        result = resolve_and_execute(sql, con, app)

        # Convert to 2D list for Excel output.
        columns = result.columns
        rows = result.fetchall()

        output = [columns]  # header row
        output.extend(list(row) for row in rows)
        return output

    @xl_func("str sql, int n: object", macro=True)
    def blobrange_preview(sql: str, n: int = 100) -> list[list[Any]]:
        """Preview the first n rows of a query result."""
        app = xl_app()
        wb_path = str(app.ActiveWorkbook.FullName) if app.ActiveWorkbook else None
        con = get_connection(wb_path)

        preview_sql = f"SELECT * FROM ({sql}) AS _preview LIMIT {int(n)}"
        result = resolve_and_execute(preview_sql, con, app)

        columns = result.columns
        rows = result.fetchall()

        output = [columns]
        output.extend(list(row) for row in rows)
        return output

    @xl_func("str sql: str", macro=True)
    def blobrange_explain(sql: str) -> str:
        """Return the resolved SQL string without executing."""
        # Phase 5: will translate via sqlglot and lambda_registry.
        # For now, returns the SQL as-is.
        return sql

    @xl_macro
    def blobrange_close() -> None:
        """Close the DuckDB connection for the active workbook."""
        app = xl_app()
        wb_path = str(app.ActiveWorkbook.FullName) if app.ActiveWorkbook else None
        close_connection(wb_path)
