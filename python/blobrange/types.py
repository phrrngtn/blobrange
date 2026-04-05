"""Excel value → PyArrow type inference.

Excel stores dates as floats (days since 1899-12-30), booleans as bool,
strings as str, and numbers as float. Error values are represented as
integers (e.g. -2146826281 for #N/A). This module infers Arrow types
from columns of Excel values.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Sequence

import pyarrow as pa

# Excel epoch: 1899-12-30 (Excel incorrectly treats 1900 as a leap year,
# so the effective epoch for dates >= 1900-03-01 is 1899-12-30).
EXCEL_EPOCH = datetime(1899, 12, 30)

# Excel error values (COM integer representations).
EXCEL_ERRORS = {
    -2146826281,  # #N/A
    -2146826246,  # #REF!
    -2146826259,  # #VALUE!
    -2146826252,  # #NAME?
    -2146826248,  # #NULL!
    -2146826245,  # #NUM!
    -2146826265,  # #DIV/0!
    -2146826244,  # #GETTING_DATA
}


def is_excel_error(value: Any) -> bool:
    """Return True if value is an Excel error sentinel."""
    return isinstance(value, int) and value in EXCEL_ERRORS


def excel_date_to_datetime(serial: float) -> datetime:
    """Convert an Excel date serial number to a Python datetime."""
    return EXCEL_EPOCH + timedelta(days=serial)


def excel_date_to_date(serial: float) -> date:
    """Convert an Excel date serial number to a Python date."""
    return excel_date_to_datetime(serial).date()


def is_integer_valued(value: float) -> bool:
    """Return True if a float represents an exact integer."""
    return value == int(value) and abs(value) < 2**53


def infer_column_type(
    values: Sequence[Any],
    number_format: str | None = None,
) -> pa.DataType:
    """Infer an Arrow type from a column of Excel values.

    Parameters
    ----------
    values:
        Column values as returned by Excel's Range.Value (tuple of scalars).
    number_format:
        The Excel NumberFormat string for the column, if available.
        Used to distinguish dates from plain numbers.

    Returns
    -------
    A PyArrow data type.
    """
    # Date format heuristics: Excel NumberFormat patterns containing
    # date/time tokens.
    if number_format and _looks_like_date_format(number_format):
        return pa.timestamp("us")

    first = _first_non_null(values)
    if first is None:
        return pa.string()  # all-null column defaults to string

    if isinstance(first, bool):
        return pa.bool_()
    if isinstance(first, str):
        return pa.string()
    if isinstance(first, (datetime, date)):
        return pa.timestamp("us")
    if isinstance(first, float):
        if all(
            v is None or is_excel_error(v) or (isinstance(v, float) and is_integer_valued(v))
            for v in values
        ):
            return pa.int64()
        return pa.float64()
    if isinstance(first, int):
        if is_excel_error(first):
            # Skip errors, look for a non-error value.
            rest = [v for v in values if not is_excel_error(v) and v is not None]
            return infer_column_type(rest, number_format) if rest else pa.string()
        return pa.int64()

    return pa.string()


def coerce_value(value: Any, target_type: pa.DataType) -> Any:
    """Coerce a single Excel value to match the target Arrow type.

    Returns None for null/error values.
    """
    if value is None or is_excel_error(value):
        return None

    if pa.types.is_timestamp(target_type) or pa.types.is_date(target_type):
        if isinstance(value, float):
            return excel_date_to_datetime(value)
        if isinstance(value, (datetime, date)):
            return value
        return None

    if pa.types.is_int64(target_type):
        if isinstance(value, float):
            return int(value)
        if isinstance(value, int):
            return value
        return None

    if pa.types.is_float64(target_type):
        if isinstance(value, (int, float)):
            return float(value)
        return None

    if pa.types.is_boolean(target_type):
        if isinstance(value, bool):
            return value
        return None

    # string or fallback
    return str(value) if value is not None else None


def _first_non_null(values: Sequence[Any]) -> Any | None:
    """Return the first value that is not None and not an Excel error."""
    for v in values:
        if v is not None and not is_excel_error(v):
            return v
    return None


_DATE_TOKENS = {"y", "m", "d", "h", "s", "AM/PM", "am/pm"}


def _looks_like_date_format(fmt: str) -> bool:
    """Heuristic: does an Excel NumberFormat string look like a date format?"""
    # Strip quoted literals.
    cleaned = ""
    in_quote = False
    for ch in fmt:
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            cleaned += ch
    # Check for date/time tokens.
    lower = cleaned.lower()
    return any(tok in lower for tok in ("yyyy", "yy", "mm", "dd", "hh", "ss", "am/pm"))
