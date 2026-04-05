"""ListObject (Excel Table) → DataFrame / PyArrow Table conversion.

A ListObject has an explicit header row and a defined body range.
This is the best-behaved case: schema is unambiguous.
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd
import pyarrow as pa

from blobrange.types import (
    coerce_value,
    excel_date_to_datetime,
    infer_column_type,
    is_excel_error,
)


class ExcelListObject(Protocol):
    """Minimal interface for an Excel ListObject COM object."""

    @property
    def Name(self) -> str: ...

    @property
    def HeaderRowRange(self) -> Any: ...

    @property
    def DataBodyRange(self) -> Any: ...

    @property
    def ListColumns(self) -> Any: ...

    @property
    def Parent(self) -> Any: ...


def read_listobject(list_object: ExcelListObject) -> pa.Table:
    """Convert an Excel ListObject to a PyArrow Table.

    Parameters
    ----------
    list_object:
        A COM ListObject with HeaderRowRange and DataBodyRange.

    Returns
    -------
    A PyArrow Table with inferred column types.
    """
    column_names = _get_column_names(list_object)
    body = list_object.DataBodyRange
    if body is None:
        # Empty table — return schema with no rows.
        schema = pa.schema([(name, pa.string()) for name in column_names])
        return pa.table({name: pa.array([], type=pa.string()) for name in column_names}, schema=schema)

    raw_data = body.Value
    if raw_data is None:
        schema = pa.schema([(name, pa.string()) for name in column_names])
        return pa.table({name: pa.array([], type=pa.string()) for name in column_names}, schema=schema)

    return _raw_to_table(column_names, raw_data, list_object)


def read_listobject_from_raw(
    column_names: list[str],
    raw_data: tuple[tuple[Any, ...], ...],
    number_formats: list[str | None] | None = None,
) -> pa.Table:
    """Convert pre-extracted column names and raw data to a PyArrow Table.

    This is the testable core — no COM dependency.

    Parameters
    ----------
    column_names:
        List of column name strings.
    raw_data:
        Tuple of row tuples as returned by Range.Value.
    number_formats:
        Optional per-column NumberFormat strings for date detection.
    """
    num_cols = len(column_names)
    # Transpose: rows → columns.
    columns: list[list[Any]] = [[] for _ in range(num_cols)]
    for row in raw_data:
        for col_idx in range(num_cols):
            columns[col_idx].append(row[col_idx] if col_idx < len(row) else None)

    if number_formats is None:
        number_formats = [None] * num_cols

    arrow_columns: dict[str, pa.Array] = {}
    for col_idx, name in enumerate(column_names):
        col_values = columns[col_idx]
        fmt = number_formats[col_idx] if col_idx < len(number_formats) else None
        arrow_type = infer_column_type(col_values, fmt)
        coerced = [coerce_value(v, arrow_type) for v in col_values]
        arrow_columns[name] = pa.array(coerced, type=arrow_type)

    return pa.table(arrow_columns)


def _get_column_names(list_object: ExcelListObject) -> list[str]:
    """Extract column names from a ListObject's header row."""
    header = list_object.HeaderRowRange
    if header is not None:
        values = header.Value
        if values is not None:
            # Range.Value returns a tuple of tuples; header is one row.
            row = values[0] if isinstance(values[0], tuple) else values
            return [str(v) if v is not None else f"col_{i}" for i, v in enumerate(row)]

    # Fallback: use ListColumns collection.
    return [str(col.Name) for col in list_object.ListColumns]


def _raw_to_table(
    column_names: list[str],
    raw_data: Any,
    list_object: ExcelListObject,
) -> pa.Table:
    """Convert Range.Value output to a PyArrow Table."""
    # Range.Value for a single row returns a flat tuple, not a tuple of tuples.
    if raw_data and not isinstance(raw_data[0], tuple):
        raw_data = (raw_data,)

    # Try to get number formats for date detection.
    number_formats = _get_number_formats(list_object, len(column_names))

    return read_listobject_from_raw(column_names, raw_data, number_formats)


def _get_number_formats(list_object: ExcelListObject, num_cols: int) -> list[str | None]:
    """Try to extract NumberFormat for each column from the first data row."""
    try:
        body = list_object.DataBodyRange
        if body is None:
            return [None] * num_cols
        # Read NumberFormat from first row of body.
        first_row = body.Rows(1)
        formats = []
        for col_idx in range(1, num_cols + 1):
            try:
                cell = first_row.Cells(1, col_idx)
                fmt = cell.NumberFormat
                formats.append(str(fmt) if fmt else None)
            except Exception:
                formats.append(None)
        return formats
    except Exception:
        return [None] * num_cols


# ---------------------------------------------------------------------------
# Pandas path — simpler, used by the replacement scan resolver.
# DuckDB's existing replacement scan machinery already knows how to scan
# a pandas DataFrame, so we just hand it one.
# ---------------------------------------------------------------------------


def read_listobject_to_pandas(list_object: ExcelListObject) -> pd.DataFrame:
    """Convert an Excel ListObject to a pandas DataFrame.

    Parameters
    ----------
    list_object:
        A COM ListObject with HeaderRowRange and DataBodyRange.
    """
    column_names = _get_column_names(list_object)
    body = list_object.DataBodyRange
    if body is None or body.Value is None:
        return pd.DataFrame(columns=column_names)

    raw_data = body.Value
    # Range.Value for a single row returns a flat tuple, not a tuple of tuples.
    if raw_data and not isinstance(raw_data[0], tuple):
        raw_data = (raw_data,)

    return raw_to_pandas(column_names, raw_data)


def raw_to_pandas(
    column_names: list[str],
    raw_data: tuple[tuple[Any, ...], ...],
) -> pd.DataFrame:
    """Convert pre-extracted column names and raw data to a pandas DataFrame.

    Testable core — no COM dependency.
    Excel error values are mapped to None (becomes NaN in pandas).
    """
    num_cols = len(column_names)
    columns: dict[str, list[Any]] = {name: [] for name in column_names}

    for row in raw_data:
        for col_idx, name in enumerate(column_names):
            val = row[col_idx] if col_idx < len(row) else None
            if val is not None and is_excel_error(val):
                val = None
            columns[name].append(val)

    return pd.DataFrame(columns)
