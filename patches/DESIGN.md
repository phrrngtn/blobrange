# duckdb-python patch: `add_replacement_scan`

## What this patch does

Adds `con.add_replacement_scan(callback)` to the DuckDB Python API, allowing
Python code to register a callable that DuckDB invokes when it encounters an
unresolved table name during query planning. The callback returns a scannable
Python object (pandas DataFrame, PyArrow Table, etc.) or `None` to decline.

```python
import duckdb, pandas as pd

def my_resolver(table_name: str):
    if table_name == "products":
        return pd.DataFrame({"id": [1, 2], "name": ["Widget", "Gadget"]})
    return None

con = duckdb.connect()
con.add_replacement_scan(my_resolver)
con.sql("SELECT * FROM products").show()
```

## Why this patch exists

DuckDB's replacement scan mechanism is the extension point that fires when
the query planner encounters a table name it cannot resolve in the catalog.
The C++ API (`DBConfig::replacement_scans`) and the C extension API
(`duckdb_add_replacement_scan`) both allow registering custom callbacks.
The Python API does not.

The existing Python replacement scan (`PythonReplacementScan::Replace`) is
hardcoded: it walks the caller's stack frames looking for Python variables
whose names match the unresolved table name. This works for interactive use
(notebook cells, REPL) but cannot support programmatic resolution — cases
where the data source is not a variable in scope but must be fetched on
demand from an external system.

The motivating use case is `blobrange`: a PyXLL extension that makes Excel
named ranges, ListObjects, and LAMBDAs queryable as DuckDB relations. The
resolver needs to intercept unresolved names, look them up in the Excel
object model via COM, read the data into a DataFrame, and return it. This
cannot work with frame inspection because the Excel objects are not Python
variables — they live in the Excel process and must be fetched through COM
calls.

## How the patch was conceived

### The problem space

The design session started with the question: how should DuckDB discover
Excel objects as tables? Several approaches were considered and rejected:

1. **sqlglot pre-parsing** — Parse the SQL with sqlglot to extract table
   names, resolve them against Excel, register via `con.register()`, then
   execute. Rejected because it duplicates DuckDB's SQL parser and can
   diverge on edge cases (CTEs, subqueries, dialect differences).

2. **`get_table_names` + `con.register`** — Use DuckDB's own
   `get_table_names(sql)` to extract references, filter against the catalog,
   register matches, then execute. Better (DuckDB parses the SQL), but still
   requires a two-pass approach and the caller must wrap every query.

3. **Lazy proxy objects in scope** — Create Python objects with
   `__arrow_c_stream__` for every known Excel name and inject them into the
   caller's namespace. The existing frame-inspection replacement scan would
   find them. This works (verified experimentally) but requires
   pre-enumerating all Excel objects and polluting the namespace.

4. **Callback-based replacement scan** — Register a Python callable that
   DuckDB calls when it needs to resolve a name. DuckDB does all the
   parsing; the callback is only invoked for names not in the catalog;
   no pre-enumeration needed. This is the right abstraction.

### The implementation path

Reading the duckdb-python source (`python_replacement_scan.cpp`) revealed
the internal architecture:

- `PythonReplacementScan::Replace` is emplaced into
  `config.replacement_scans` during `FetchOrCreateInstance`, before the
  database is opened.
- The replacement scan vector in `DBConfig` holds `ReplacementScan` objects,
  each containing a C++ function pointer and an optional
  `ReplacementScanData` payload.
- `ReplacementScanData` is a virtual base class designed to be subclassed.
- The binder iterates through all registered scans in order, calling each
  until one returns non-null.
- `PythonReplacementScan::TryReplacementObject` already handles conversion
  from any supported Python type (DataFrame, Arrow Table, Polars, NumPy,
  DuckDBPyRelation) to a DuckDB `TableRef`. This is the heavy lifting —
  and it's reusable.

The patch therefore:

1. Subclasses `ReplacementScanData` to hold a `py::function`.
2. Implements a new replacement scan function that acquires the GIL, calls
   the Python callback, and feeds the result through `TryReplacementObject`.
3. Exposes `add_replacement_scan(callback)` on `DuckDBPyConnection` via
   pybind11, which emplaces the new scan into `config.replacement_scans`.

### GIL safety

The `py::function` stored in `PythonCallbackReplacementScanData` is a
Python object that must be destroyed while holding the GIL. The destructor
runs when `config.replacement_scans` is cleared during database shutdown,
which happens outside any Python context. The destructor therefore acquires
the GIL before releasing the `py::function`:

```cpp
~PythonCallbackReplacementScanData() override {
    py::gil_scoped_acquire acquire;
    callback = py::function();
}
```

This was discovered via a crash (SIGABRT) during the initial test run —
pytest's process exit triggered database cleanup, which destroyed the
callback without the GIL held.

### The callback itself acquires the GIL

The replacement scan callback is invoked from the DuckDB binder, which runs
on DuckDB's internal threads without the GIL. The callback function uses
`py::gil_scoped_acquire` before calling into Python:

```cpp
py::gil_scoped_acquire acquire;
result = scan_data.callback(py::str(input.table_name));
```

Exceptions from the Python callback are caught and result in the scan
returning `nullptr` (decline), allowing the next scan in the chain to try.

## Patch details

**Base version:** duckdb-python v1.5.1 (tag `v1.5.1`, commit `1fc6421`)

**Files modified:**

| File | Change |
|---|---|
| `src/duckdb_py/include/duckdb_python/python_replacement_scan.hpp` | Add `PythonCallbackReplacementScanData` struct and `PythonCallbackReplacementScan` function declaration |
| `src/duckdb_py/python_replacement_scan.cpp` | Implement `PythonCallbackReplacementScan` (~25 lines) |
| `src/duckdb_py/pyconnection.cpp` | Add `AddReplacementScanMethod`, forward declaration, and pybind11 binding (~20 lines) |

**Total: ~65 lines of C++ across 3 files.**

## Applying the patch

```bash
cd /path/to/duckdb-python   # checked out at v1.5.1
git apply /path/to/blobrange/patches/duckdb-python-add-replacement-scan.patch
```

## Building

```bash
# Install build dependencies
pip install "scikit-build-core>=0.11.4" "pybind11[global]>=2.6.0" "setuptools_scm>=8.0"

# Build (editable install)
OVERRIDE_GIT_DESCRIBE=v1.5.1 pip install --no-build-isolation -e .
```

## Testing

The blobrange repo includes end-to-end tests in
`tests/test_replacement_scan_e2e.py` that exercise:

- Simple SELECT through a replacement scan callback
- JOINs between two replacement-scanned tables
- JOINs between replacement-scanned and native DuckDB tables
- Callback returning None (falls through to error)
- Catalog tables taking priority over the callback
- Callback invoked per query (no stale caching by DuckDB)

## Upstream consideration

This patch is minimal and self-contained. It does not modify the existing
`PythonReplacementScan::Replace` behavior (frame inspection continues to
work as before). The new scan is appended to the replacement scan vector
after the existing one, so user-registered callbacks are tried after the
built-in frame inspection.

A reasonable upstream API might differ in naming or placement (e.g.,
`register_replacement_scan`, or a parameter on `duckdb.connect()`), but the
underlying mechanism — subclassing `ReplacementScanData` to hold a
`py::function` and emplacing into `config.replacement_scans` — is the
natural fit given the existing architecture.

## SQL quoting and Excel range addresses

DuckDB uses double-quoted identifiers (`"..."`) for names that contain
special characters. The replacement scan callback receives the **unquoted**
name — DuckDB strips the quotes before invoking the callback.

This interacts well with Excel's reference syntax because Excel uses `!`,
`$`, `:`, `[]`, and `'` (single quotes) — never double quotes. Any valid
Excel reference can be placed directly inside SQL double quotes with no
escaping:

```sql
-- Named range (plain identifier, no quoting needed)
SELECT * FROM products

-- Explicit sheet + absolute range
SELECT * FROM "Sheet1!$A$1:$D$100"

-- Sheet name with spaces
SELECT * FROM "My Sheet!$A$1:$B$10"

-- External workbook reference
SELECT * FROM "[Pricing.xlsx]Sheet1!prices"

-- Join a named range against an explicit range address
SELECT p.name, o.qty
FROM products AS p
JOIN "Orders!$A$1:$C$500" AS o ON p.id = o.product_id
```

The callback receives the literal string (`Sheet1!$A$1:$D$100`,
`[Pricing.xlsx]Sheet1!prices`, etc.) and can parse the Excel address
components as needed.

Experimental findings on how DuckDB handles various forms:

| SQL syntax | Callback receives | Notes |
|---|---|---|
| `FROM products` | `products` | Plain identifier |
| `FROM "products"` | `products` | Quotes stripped |
| `FROM "Sheet1!A1:B10"` | `Sheet1!A1:B10` | Excel range syntax preserved |
| `FROM "Sheet1!$A$1:$B$10"` | `Sheet1!$A$1:$B$10` | Absolute refs preserved |
| `FROM "My Sheet!A1:B10"` | `My Sheet!A1:B10` | Spaces work |
| `FROM "[Book1.xlsx]Sheet1!rng"` | `[Book1.xlsx]Sheet1!rng` | Full external ref |
| `FROM Sheet1.products` | `products` | Dot parsed as schema.table — only table part reaches callback |
| `FROM "Sheet1.products"` | `Sheet1.products` | Quoting preserves the dot as part of the name |
| `FROM Products` | `Products` | Case preserved (not folded) |

Note: `FROM [products]` is a syntax error — square brackets are not valid
SQL quoting in DuckDB. They only survive as literal characters inside
double quotes (e.g., `"[Book1.xlsx]Sheet1!A1"`).

## Schema-qualified names

With the named-kwargs callback signature `(table_name, schema_name,
catalog_name)`, DuckDB's dot-separated qualified names map naturally:

| SQL | `table_name` | `schema_name` | `catalog_name` |
|---|---|---|---|
| `FROM products` | `products` | `""` | `""` |
| `FROM Sheet1.products` | `products` | `Sheet1` | `""` |
| `FROM "Sheet1"."products"` | `products` | `Sheet1` | `""` |
| `FROM "WB"."Sheet1"."products"` | `products` | `Sheet1` | `WB` |

This means `schema.table` syntax can represent `worksheet.object` and
three-part syntax can represent `workbook.worksheet.object` — matching
Excel's own hierarchy without any custom parsing.

## Return types and optimizer pushdown

The replacement scan callback can return any Python object that DuckDB's
existing `TryReplacementObject` knows how to scan. The choice of return
type has significant performance implications because it determines how
much of the outer query's predicates and projections can be pushed down
into the scan.

### What each return type does

| Return type | Internal mechanism | Pushdown |
|---|---|---|
| **`DuckDBPyRelation`** | Query plan spliced as `SubqueryRef` — no data copied | Full: predicates, projections, joins all optimized through the subquery boundary. Pushdown reaches whatever the underlying relation's scan supports. |
| **pandas DataFrame** | `pandas_scan` table function | Full DuckDB-side pushdown (data is in memory, DuckDB reads only needed columns/rows) |
| **Arrow Table / Dataset** | `arrow_scan` with PyArrow compute | Projection + filter pushdown via PyArrow expressions |
| **Arrow RecordBatchReader** | `arrow_scan` | Projection pushdown; filter pushdown if PyArrow dataset is available |
| **Polars DataFrame** | `.to_arrow()` then `arrow_scan` | Same as Arrow Table |
| **Polars LazyFrame** | Filter expressions translated into Polars plan | Polars-native pushdown before materialization |
| **bare `__arrow_c_stream__`** | `arrow_scan_dumb` | No pushdown (single-use stream) |

### DuckDBPyRelation: the zero-copy path

A `DuckDBPyRelation` is a lazy query plan — a wrapper around DuckDB's
internal `Relation` object. When the replacement scan encounters one, it
does not materialize any data. It extracts the relation's query node and
splices it directly into the outer query as a subquery:

```cpp
auto select = make_uniq<SelectStatement>();
select->node = pyrel->GetRel().GetQueryNode();
auto subquery = make_uniq<SubqueryRef>(std::move(select));
```

The DuckDB optimizer then sees through the subquery boundary and applies
predicate pushdown, projection pushdown, and join reordering across the
whole plan. If the underlying relation is backed by Parquet (via DuckLake,
for example), this means partition pruning, min/max statistics, and zone
maps all apply — even though the user's query refers to a name that was
resolved dynamically by the replacement scan callback.

**Constraint:** the `DuckDBPyRelation` must have been created by the same
`DuckDBPyConnection` that is executing the query. DuckDB checks this and
rejects cross-connection relations.

### Design implication: the resolver as a policy layer

Because the resolver can return a `DuckDBPyRelation`, it can act as a
**transparent caching and routing layer** rather than a data materializer.
The resolver decides *where* to read from; DuckDB's optimizer decides
*how* to read efficiently.

Example: a Socrata open data resolver backed by DuckLake for local caching:

```python
def resolver(table_name, schema_name="", catalog_name=""):
    if schema_name != "Socrata":
        return None

    # Check local DuckLake cache
    if is_cached(table_name) and not is_stale(table_name):
        return con.sql(f'SELECT * FROM ducklake."{table_name}"')

    # Refresh cache from remote, then return local relation
    refresh_from_socrata(table_name)  # idempotent
    return con.sql(f'SELECT * FROM ducklake."{table_name}"')
```

The user writes:

```sql
SELECT *
FROM Socrata."nyc.data.gov.Parking Tickets"
WHERE zipcode = '11231'
```

DuckDB resolves the name, gets back a relation against DuckLake-managed
Parquet, and pushes `WHERE zipcode = '11231'` all the way down to the
storage layer — partition pruning, predicate pushdown, the full optimizer
stack. The user never knows whether the data came from the network or a
local cache.

The resolver runs during query planning (the binder phase). DuckDB caches
the replacement scan result as a CTE internally, so even if the same name
appears multiple times in a query (e.g., self-join), the resolver is called
once and the result is reused. This means any refresh/cache operations in
the resolver execute once per query, not once per reference.

**Idempotency matters:** because the replacement scan can fire more than
once for the same name during planning (observed in some join patterns),
any side effects in the resolver (cache refresh, logging, etc.) should be
idempotent. "Ensure the local table reflects the remote state" is safe.
"Append the latest delta" is not.
