## Summary

Add `con.add_replacement_scan(callback)` to the Python API.

The callback receives a table name (`str`) when DuckDB encounters an
unresolved name during query planning. It returns a scannable Python object
(DataFrame, Arrow Table, etc.) or `None` to decline. The result is fed
through the existing `TryReplacementObject` machinery.

```python
con = duckdb.connect()
con.add_replacement_scan(lambda name: my_external_lookup(name))
con.sql("SELECT * FROM some_external_table")
```

This enables programmatic table resolution — cases where the data source
is not a Python variable in scope but must be fetched on demand (e.g.,
Excel COM objects, remote catalogs, plugin-managed data).

## Implementation

- `PythonCallbackReplacementScanData` — subclasses `ReplacementScanData`
  to hold a `py::function`. Destructor acquires GIL before releasing the
  Python object.
- `PythonCallbackReplacementScan` — acquires GIL, calls callback, passes
  result to `TryReplacementObject`.
- `add_replacement_scan` — pybind11 binding that emplaces into
  `config.replacement_scans`.

~65 lines of C++ across 3 existing files. No changes to the existing
frame-inspection replacement scan behavior.

## Test plan

- [x] Basic resolution (DataFrame, Arrow Table)
- [x] Callback returning `None` falls through to catalog error
- [x] Catalog tables take priority over callback
- [x] Joins between resolved and native tables
- [x] Callback invoked per query (no stale caching)
- [x] Exception in callback treated as decline
- [x] Quoted identifiers passed through unquoted
- [x] `enable_external_access=False` disables callback
- [x] Method returns connection (chaining)
