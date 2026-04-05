## Summary

Add `con.add_replacement_scan(callback)` to the Python API.

The callback is invoked with keyword arguments `(table_name, schema_name,
catalog_name)` when DuckDB encounters an unresolved name during query
planning. It returns a scannable Python object (DataFrame, Arrow Table,
DuckDBPyRelation, etc.) or `None` to decline. The result is fed through
the existing `TryReplacementObject` machinery, so all currently supported
Python object types work automatically.

```python
con = duckdb.connect()
con.add_replacement_scan(
    lambda table_name, schema_name="", catalog_name="": my_lookup(table_name)
)
con.sql("SELECT * FROM some_external_table WHERE x > 10")
```

This enables programmatic table resolution — cases where the data source
is not a Python variable in scope but must be fetched on demand (e.g.,
Excel COM objects via PyXLL, remote catalog lookups, plugin-managed data).

Returning a `DuckDBPyRelation` is the zero-copy path: DuckDB splices the
relation's query plan directly into the outer query as a subquery,
enabling full predicate and projection pushdown through the scan boundary.

Schema-qualified names are decomposed into the three keyword arguments,
so `FROM myschema.mytable` passes `schema_name="myschema"`,
`table_name="mytable"`.

## Implementation

- `PythonCallbackReplacementScanData` — subclasses `ReplacementScanData`
  to hold a `py::function`. Destructor acquires GIL before releasing the
  Python object.
- `PythonCallbackReplacementScan` — acquires GIL, calls callback with
  keyword arguments, passes result to `TryReplacementObject`.
- `add_replacement_scan` — pybind11 binding that emplaces into
  `config.replacement_scans`.

~65 lines of C++ across 3 existing files. No changes to the existing
frame-inspection replacement scan behavior.

## Test plan

- [x] Basic resolution (DataFrame, Arrow Table)
- [x] Callback returning `None` falls through to catalog error
- [x] Catalog tables take priority over callback
- [x] Joins between two resolved tables
- [x] Joins between resolved and native tables
- [x] Callback invoked per query (no stale caching)
- [x] Exception in callback treated as decline
- [x] Quoted identifiers passed through unquoted
- [x] `enable_external_access=False` disables callback
- [x] Method returns connection (chaining)
- [x] Two-part qualified name passes `schema_name`
- [x] Three-part qualified name passes `schema_name` + `catalog_name`
