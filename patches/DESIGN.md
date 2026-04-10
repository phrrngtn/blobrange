# duckdb-python patch: `add_replacement_scan`

## What this patch does

Adds `con.add_replacement_scan(callback)` to the DuckDB Python API, allowing
Python code to register a callable that DuckDB invokes when it encounters an
unresolved table name during query planning.

The callback receives keyword arguments `(table_name, schema_name,
catalog_name)` and can return:

- `None` — decline (DuckDB tries the next replacement scan)
- A dict `{"type": "table", ...}` — name redirect to another catalog entry
  (full optimizer pushdown, zero data copying)
- A dict `{"type": "query", "sql": "..."}` — SQL rewrite as a subquery
  (parsed lock-free, full pushdown)
- A scannable Python object (DataFrame, Arrow Table, etc.) — scanned
  directly via the existing `TryReplacementObject` machinery

```python
import duckdb

con = duckdb.connect()
con.execute("ATTACH 'cache.duckdb' AS cache")

def resolver(table_name, schema_name="", catalog_name=""):
    if schema_name == "socrata":
        ensure_cached(table_name)  # refresh via separate connection
        return {"type": "table", "catalog": "cache", "schema": "main", "table": table_name}
    return None

con.add_replacement_scan(resolver)
con.sql("SELECT * FROM socrata.parking_tickets WHERE zipcode = '11231'")
# Predicate pushes all the way to the Parquet cache
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

A broader use case is external catalog integration (Socrata, Rule4) where
hundreds of thousands of datasets should be addressable by name with
transparent local caching and full predicate pushdown.

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
   pre-enumerating all Excel objects and polluting the namespace. Does not
   scale to catalogs with hundreds of thousands of entries.

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

### The deadlock problem and the dict-dispatch solution

The initial prototype simply called `TryReplacementObject` on whatever the
Python callback returned. This works for DataFrames and Arrow Tables, but
the real value of the replacement scan is the ability to redirect names
to other catalog entries with full optimizer pushdown — and that requires
returning a `DuckDBPyRelation` or constructing a `TableRef` AST node.

The problem: creating a `DuckDBPyRelation` requires calling `con.sql()`
or `con.table()`, which acquires the `ClientContext` lock. But the binder
already holds that lock when the replacement scan fires. Result: deadlock.

This was traced through the duckdb-python and DuckDB core source:

```
con.sql("SELECT * FROM ...")
  → RunQuery
    → GetStatements
      → connection.ExtractStatements
        → context->ParseStatements
          → LockContext()    ← DEADLOCK: binder already holds this lock
```

Even `con.table("name")` deadlocks because it calls `TableInfo()` which
runs inside `RunFunctionInTransaction`, which also acquires the lock.

The solution: **bypass the connection entirely and construct AST nodes
directly in C++**. The Python callback returns a dict describing what it
wants, and the C++ code builds the corresponding `TableRef` without
touching any connection locks:

- `{"type": "table", ...}` → constructs a `BaseTableRef` (pure data:
  three strings) wrapped in a `SubqueryRef`
- `{"type": "query", "sql": "..."}` → parses the SQL using `Parser`
  directly (stateless, no lock) and wraps the result in a `SubqueryRef`

Both paths produce unbound AST nodes that the binder on the active
connection resolves against its own catalog. The optimizer sees through
the `SubqueryRef` boundary, enabling full predicate pushdown, projection
pushdown, partition pruning, and join reordering.

The `SubqueryRef` wrapping for the `"table"` path is necessary because
the binder's replacement scan code wraps non-`SubqueryRef`/non-
`TableFunctionRef` results in a `SELECT * FROM ...` subquery and drops
the user's alias in the process. By returning a `SubqueryRef` with the
alias pre-set, we avoid this.

### Lock-free SQL parsing

The `"query"` path uses DuckDB's `Parser` class directly:

```cpp
ParserOptions options;
options.preserve_identifier_case = true;
Parser parser(options);
parser.ParseQuery(sql);
```

`Parser` is stateless — it needs only `ParserOptions`, which are simple
config flags. The normal path (`ClientContext::ParseStatements`) wraps this
in a `LockContext()` call, but the lock is for protecting `ClientContext`
state during parsing (pragma handling, error processing), not for the
parser itself. By constructing a `Parser` directly, we avoid the lock.

**Known limitation:** the `ParserOptions` are not read from the active
session's settings (e.g., `preserve_identifier_case`). We hardcode sensible
defaults. This could cause subtle differences in how names are handled if
the session has non-default parser settings.

### GIL safety

The `py::function` stored in `PythonCallbackReplacementScanData` is a
Python object that must be destroyed while holding the GIL. pybind11's
`py::object` types are RAII wrappers around `PyObject*` — their
destructors call `Py_DECREF`, which is a Python C API call that requires
the GIL.

The destructor runs when `config.replacement_scans` is cleared during
database shutdown, which happens on a C++ thread without the GIL. The
destructor therefore acquires the GIL before releasing the `py::function`:

```cpp
~PythonCallbackReplacementScanData() override {
    py::gil_scoped_acquire acquire;
    callback = py::function();
}
```

This was discovered via a crash (SIGABRT) during the initial test run —
pytest's process exit triggered database cleanup, which destroyed the
callback without the GIL held. The `Py_DECREF` corrupted the interpreter
state.

### The callback itself acquires the GIL

The replacement scan callback is invoked from the DuckDB binder, which runs
without the GIL. The callback function uses `py::gil_scoped_acquire` before
calling into Python:

```cpp
py::gil_scoped_acquire acquire;
result = scan_data.callback("table_name"_a = py::str(input.table_name),
                            "schema_name"_a = py::str(input.schema_name),
                            "catalog_name"_a = py::str(input.catalog_name));
```

Exceptions from the Python callback are caught and result in the scan
returning `nullptr` (decline), allowing the next scan in the chain to try.
This means there is no way for the callback to surface a meaningful error
message to the user — the query will fail with a generic "table not found"
error.

## Callback dispatch logic

The C++ callback (`PythonCallbackReplacementScan`) dispatches on the return
type from the Python callable:

1. **`None`** → return `nullptr` (decline)
2. **`dict`** → dispatch to `HandleDictDirective`:
   - `{"type": "table", "catalog": ..., "schema": ..., "table": ...}` →
     construct `BaseTableRef` wrapped in `SubqueryRef` (name redirect)
   - `{"type": "query", "sql": "SELECT ..."}` → parse SQL lock-free,
     wrap in `SubqueryRef` (SQL rewrite)
   - Any other `"type"` → raise `InvalidInputException`
   - Missing `"type"` key → raise `InvalidInputException`
3. **Anything else** → pass to `TryReplacementObject` (DataFrame, Arrow
   Table, etc.)

Each path is an affirmative decision by the resolver. There is no
fallthrough or default behavior — the resolver either declines (`None`),
redirects (dict), or provides data (object).

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

The choice of return type determines how much of the outer query's
predicates and projections can be pushed down into the scan.

### Dict-based returns (zero-copy, full pushdown)

| Dict type | Internal mechanism | Pushdown |
|---|---|---|
| `{"type": "table", ...}` | `BaseTableRef` in `SubqueryRef` — pure AST, no data | Full: optimizer resolves the redirected name against the catalog and applies all optimizations (predicate pushdown, projection, partition pruning, zone maps) |
| `{"type": "query", "sql": ...}` | Parsed `SelectStatement` in `SubqueryRef` — pure AST | Full: optimizer sees through the subquery boundary |

These paths construct unbound AST nodes without acquiring any locks or
copying any data. The binder on the active connection resolves all table
references in the AST against its own catalog.

### Object returns (data materialized in Python)

| Return type | Internal mechanism | Pushdown |
|---|---|---|
| **pandas DataFrame** | `pandas_scan` table function | Full DuckDB-side pushdown (data is in memory, DuckDB reads only needed columns/rows) |
| **Arrow Table / Dataset** | `arrow_scan` with PyArrow compute | Projection + filter pushdown via PyArrow expressions |
| **Arrow RecordBatchReader** | `arrow_scan` | Projection pushdown; filter pushdown if PyArrow dataset is available |
| **Polars DataFrame** | `.to_arrow()` then `arrow_scan` | Same as Arrow Table |
| **Polars LazyFrame** | Filter expressions translated into Polars plan | Polars-native pushdown before materialization |
| **bare `__arrow_c_stream__`** | `arrow_scan_dumb` | No pushdown (single-use stream) |
| **`DuckDBPyRelation`** | Query plan spliced as `SubqueryRef` | Full pushdown, but requires same connection and cannot be created inside the callback (deadlock) |

### The ATTACH + redirect pattern

The most powerful pattern for external catalogs: attach a cache database
and use the `"table"` redirect to point at cached tables. The optimizer
pushes predicates all the way to the storage layer.

```python
con = duckdb.connect()
con.execute("ATTACH 'cache.duckdb' AS cache (READ_ONLY)")

def resolver(table_name, schema_name="", **kw):
    if schema_name == "store":
        return {"type": "table", "catalog": "cache", "schema": "main", "table": table_name}
    return None

con.add_replacement_scan(resolver)
con.sql("SELECT name FROM store.products WHERE price < 15")
# EXPLAIN shows: SEQ_SCAN on cache.main.products with Filters: price<15
```

This was verified via EXPLAIN output — the predicate appears on the scan
node, confirming pushdown through the replacement scan boundary.

## Same-connection re-entrancy: a known deadlock

The replacement scan callback fires during the binder phase while DuckDB
holds the `ClientContext` lock. If the callback attempts any operation on
the **same** connection — even a read-only one — it will deadlock.

This was confirmed via stress testing. Every operation on the same
connection deadlocks inside the callback:

| Operation | Result |
|---|---|
| `con.sql("...")` (lazy relation) | DEADLOCK |
| `con.table("...")` | DEADLOCK |
| `con.sql("...").fetchdf()` | DEADLOCK |
| `con.execute("...")` | DEADLOCK |
| `con.cursor().sql("...")` | DEADLOCK |

The lock is the `ClientContext` lock, acquired by `ParseStatements` (for
`sql`/`execute`) and `TableInfo` (for `table`). Even creating a cursor
doesn't help — cursors share the same `ClientContext`.

**Safe patterns:**

1. Return a dict (`"table"` or `"query"`) — no connection interaction.
2. Return pre-materialized data (DataFrame, Arrow Table).
3. Query a **different** connection for cache refresh or data retrieval.
4. Return a `DuckDBPyRelation` created *before* the query (outside the
   callback). Relations are lazy plans, not snapshots — they see current
   data when executed.

**The dict-dispatch paths were designed specifically to avoid this
deadlock.** They construct AST nodes in C++ without touching the
connection, which is why `"table"` redirects and `"query"` rewrites are
the preferred return types for production use.

## Recursive resolution

If the callback returns a dict that references a name the callback itself
handles, the replacement scan fires again for that name. This is by design
— DuckDB's binder resolves all names in the returned AST, and unresolved
ones trigger replacement scans as usual.

This enables chained resolution (A → B → C) but also creates a footgun:
a resolver that redirects A → B → A will loop until DuckDB hits an
internal limit. There is no depth guard in the replacement scan protocol.

**Recommendation:** resolvers should not emit ASTs that reference names
the resolver itself handles. If the resolver manages a namespace (e.g.,
`schema_name == "socrata"`), the redirected targets should always be in
a different namespace (e.g., `catalog: "cache"`) that resolves through
normal catalog lookup without triggering the replacement scan again.

## Patch details

**Base version:** duckdb-python v1.5.1 (tag `v1.5.1`)

**Files modified:**

| File | Change |
|---|---|
| `python_replacement_scan.hpp` | `PythonCallbackReplacementScanData` struct (GIL-safe destructor), `PythonCallbackReplacementScan` declaration, callback protocol documentation |
| `python_replacement_scan.cpp` | `HandleDictDirective` (table redirect + SQL rewrite), `PythonCallbackReplacementScan` (GIL acquire, callback dispatch), lock-free `Parser` usage |
| `pyconnection.cpp` | `AddReplacementScanMethod`, forward declaration, pybind11 binding |
| `test_add_replacement_scan.py` | 41 tests across 5 test classes |

**Total: ~150 lines of C++, ~500 lines of tests.**

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

## Test results

41 tests across 5 classes, all passing:

| Test class | Tests | Coverage |
|---|---|---|
| `TestAddReplacementScanDataFrame` | 13 | DataFrame/Arrow returns, None, catalog priority, schema-qualified, exceptions, chaining |
| `TestAddReplacementScanTableRedirect` | 10 | Name redirects with aliases, joins, self-joins, schema, views, CTAS, error on nonexistent |
| `TestAddReplacementScanQueryRewrite` | 15 | SQL rewrites with aliases, filter composition, aggregation, table functions, joins, CTEs, subqueries, windows, prepared stmts, views, error cases |
| `TestAddReplacementScanDictErrors` | 2 | Missing type key, unknown type |
| `TestAddReplacementScanMixed` | 1 | All four return types in one resolver |

Additional stress testing (50 tests, separate suite) covered threading
(12 concurrent threads), scale (1M rows), recursive CTEs, LATERAL joins,
DESCRIBE, EXPLAIN pushdown verification, and the ATTACH + redirect pattern.

## Open design questions

1. **The dict return is stringly-typed.** `{"type": "table"}` and
   `{"type": "query"}` are runtime dispatch on magic strings. Typed Python
   objects (e.g., `duckdb.ReplacementTableRef(catalog, schema, table)`) would
   be more principled.

2. **Polymorphic return.** The callback can return `None`, a dict, or a
   scannable object — three different protocols. Separate registration
   methods or a single wrapper type might be cleaner.

3. **The SQL rewrite path uses default `ParserOptions`** rather than reading
   from the active session's settings. This avoids touching the locked
   `ClientContext` but could cause inconsistencies with non-default parser
   configurations.

4. **No way to signal errors from the callback** other than raising an
   exception, which is silently treated as a decline. The query fails with
   a generic "table not found" error regardless of what went wrong in the
   callback.

5. **No depth guard for recursive resolution.** A resolver that redirects
   A → B → A will loop. This is a footgun that should at minimum be
   documented, and ideally guarded against.
