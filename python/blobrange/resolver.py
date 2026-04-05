"""Excel replacement scan resolver — read-through cache of Excel objects.

Provides a callable that DuckDB invokes via the replacement scan mechanism
when it encounters an unresolved table name. The resolver maintains a cache
of known Excel objects (ListObjects, named ranges, LAMBDAs) keyed by name,
with read-through to Excel to verify the cache is current before returning
a pandas DataFrame for DuckDB to scan.

Usage (requires patched duckdb-python with add_replacement_scan):

    from blobrange.resolver import ExcelResolver

    resolver = ExcelResolver(xl_app)
    con = duckdb.connect()
    con.add_replacement_scan(resolver)
    con.sql("SELECT * FROM products")  # 'products' resolved from Excel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from blobrange.listobject import read_listobject_to_pandas
from blobrange.types import EXCEL_ERRORS

logger = logging.getLogger(__name__)


@dataclass
class CachedObject:
    """A cached Excel object with its last-known data."""

    name: str
    object_type: str  # 'list_object' | 'named_range' | 'lambda'
    worksheet_name: str
    workbook_path: str
    df: pd.DataFrame
    cached_at: datetime = field(default_factory=datetime.now)
    excel_obj: Any = None  # COM reference for cache validation


class ExcelResolver:
    """Replacement scan callback for DuckDB.

    Callable with signature (table_name: str) -> Optional[pd.DataFrame].
    DuckDB calls this when it can't find a table name in its catalog.
    """

    def __init__(self, xl_app: Any) -> None:
        self._xl_app = xl_app
        self._cache: dict[str, CachedObject] = {}
        self._object_index: dict[str, Any] | None = None

    def __call__(self, table_name: str) -> pd.DataFrame | None:
        """DuckDB replacement scan entry point.

        Returns a pandas DataFrame if the name resolves to an Excel object,
        or None to let DuckDB continue to the next replacement scan.
        """
        # Read-through: refresh the index if stale, then check cache
        cached = self._cache.get(table_name)
        if cached is not None:
            # Validate: re-read from Excel and update cache
            df = self._read_object(cached.excel_obj, cached.object_type)
            if df is not None:
                cached.df = df
                cached.cached_at = datetime.now()
                return df
            else:
                # Object disappeared from Excel — evict
                del self._cache[table_name]
                self._object_index = None

        # Cache miss: scan Excel for the name
        excel_obj, object_type = self._find_excel_object(table_name)
        if excel_obj is None:
            return None

        df = self._read_object(excel_obj, object_type)
        if df is None:
            return None

        # Cache it
        try:
            wb_path = str(excel_obj.Parent.Parent.FullName)
            ws_name = str(excel_obj.Parent.Name)
        except Exception:
            wb_path = ""
            ws_name = ""

        self._cache[table_name] = CachedObject(
            name=table_name,
            object_type=object_type,
            worksheet_name=ws_name,
            workbook_path=wb_path,
            df=df,
            excel_obj=excel_obj,
        )

        logger.info(
            "Resolved '%s' as %s (%d rows, %d cols) from %s!%s",
            table_name,
            object_type,
            len(df),
            len(df.columns),
            wb_path,
            ws_name,
        )
        return df

    def invalidate(self, name: str | None = None) -> None:
        """Evict a named entry or the entire cache."""
        if name is None:
            self._cache.clear()
            self._object_index = None
        else:
            self._cache.pop(name, None)

    @property
    def cached_names(self) -> list[str]:
        """Names currently in the cache."""
        return list(self._cache.keys())

    def _find_excel_object(self, name: str) -> tuple[Any, str] | tuple[None, str]:
        """Search all open workbooks for an object matching name.

        Resolution order:
        1. ListObject
        2. Named range (Phase 2)
        3. Zero-arg LAMBDA (Phase 3)
        """
        # 1. ListObject
        lo = self._find_listobject(name)
        if lo is not None:
            return lo, "list_object"

        # 2. Named range — Phase 2
        # 3. Zero-arg LAMBDA — Phase 3

        return None, ""

    def _find_listobject(self, name: str) -> Any | None:
        """Search all open workbooks for a ListObject with the given name."""
        try:
            for wb in self._xl_app.Workbooks:
                for ws in wb.Worksheets:
                    try:
                        lo = ws.ListObjects(name)
                        if lo is not None:
                            return lo
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _read_object(self, excel_obj: Any, object_type: str) -> pd.DataFrame | None:
        """Read an Excel object into a pandas DataFrame."""
        if object_type == "list_object":
            return self._read_listobject(excel_obj)
        # Phase 2: named_range
        # Phase 3: lambda
        return None

    def _read_listobject(self, list_object: Any) -> pd.DataFrame | None:
        """Read a ListObject into a pandas DataFrame."""
        try:
            return read_listobject_to_pandas(list_object)
        except Exception:
            logger.exception("Failed to read ListObject '%s'", list_object.Name)
            return None
