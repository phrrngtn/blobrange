# blobrange

**Uniform relation addressing for Excel objects in DuckDB.**

Part of the [blob extension family](https://github.com/phrrngtn/rule4/blob/main/BLOB_EXTENSIONS.md).

> *Note on authorship:* This design document was developed collaboratively
> between Paul Harrington and Claude (Anthropic), March 2026, as part of a
> broader design session on Excel/DuckDB integration and staged computation.

---

## One-line summary

`blobrange` makes Excel named ranges, ListObjects, and dynamic array LAMBDAs
into first-class DuckDB relations, queryable by name in SQL alongside any
other data source.

---

## Motivation: everything is just a table

The blob extension family's central architectural principle is that DuckDB can
serve as a universal query layer over heterogeneous sources — relational
databases via `blobodbc`, documents via `blobboxes`, web APIs via
`duckdb-http-enterprise`, domain fingerprints via `blobfilters`. The missing
piece is the Excel workbook itself.

A business workbook contains product catalogs, regional sales targets,
pricing rules, and calibrated parameters — authoritative data that domain
experts maintain in Excel because Excel is the right tool for that work. At
present, that data is stranded: it cannot participate in SQL joins, it cannot
be queried against a DuckDB catalog, and it cannot be fed directly into a
Vega-Lite rendering pipeline without application-layer plumbing.

`blobrange` closes this gap. After loading the extension, SQL written against
Excel objects looks identical to SQL written against any other DuckDB relation:

```sql
SELECT p.product_id, SUM(o.quantity * o.unit_price) AS total_revenue
FROM products AS p
JOIN orders AS o ON p.product_id = o.product_id
WHERE p.category = 'Electronics'
GROUP BY p.product_id
```

Where `products` is a ListObject on Sheet2 and `orders` is a Parquet
file on MinIO — the query planner does not distinguish them.

This is **uniform relation addressing**: every named tabular thing is reachable
by name in SQL, regardless of where it lives. The data source is an
implementation detail the query layer does not see.

---

## Relationship to the blob family

| Extension | Wraps / exposes |
|---|---|
| `blobodbc` | ODBC-accessible databases → JSON scalars |
| `blobtemplates` | Inja templates + JMESPath → SQL/text codegen |
| `blobfilters` | Roaring bitmap fingerprints → domain classification |
| `blobboxes` | PDF/XLSX/DOCX → normalised relational schema |
| `blobrange` | Excel named ranges, ListObjects, LAMBDAs → DuckDB relations |

`blobrange` is the Excel-side counterpart to `blobodbc`. Where `blobodbc`
bridges external relational databases into DuckDB via ODBC, `blobrange` bridges
the Excel workbook's own data structures into DuckDB via PyXLL.

The two extensions compose naturally: a query can join a ListObject (via
`blobrange`) against a SQL Server catalog table (via `blobodbc`) against a
Parquet file (DuckDB built-in) in a single SQL statement.

---

## Implementation host: PyXLL

`blobrange` is implemented as a PyXLL extension rather than a standalone
C/C++ DuckDB extension. This is a deliberate departure from the pattern of
the other blob extensions, which are C/C++ core libraries with thin wrappers.

The reason is access: Excel's object model — named ranges, ListObjects,
worksheet LAMBDAs — is only addressable from within the Excel process. PyXLL
provides exactly that boundary: Python running inside Excel, with full access
to the Excel COM/API surface and the ability to register DuckDB connections
and Python callables as SQL functions.

This means `blobrange` is not a loadable `.duckdb_extension` file but a set
of PyXLL-registered hooks that configure an in-process DuckDB connection.
The user experience is the same: load the workbook, get a DuckDB connection
that can see Excel objects as relations.

---

## The mechanism: DuckDB replacement scan

DuckDB's replacement scan is an extension point that fires when the query
planner encounters a table name it cannot resolve in the catalog. Instead of
raising an error, it calls a registered Python callback with the unresolved
name. The callback can return a DuckDB relation — or `None` to fall through
to the next resolver or to a genuine error.

`blobrange` registers a replacement scan callback that implements the
following resolution hierarchy:

```
SQL table reference "foo"
    → DuckDB catalog?               → use it (pass through)
    → ListObject named "foo"?       → replacement scan → structured table read
    → Named range "foo"?            → replacement scan → range read + shape inference
    → Zero-arg LAMBDA named "foo"?  → replacement scan → evaluate → spilled array
    → None                          → DuckDB raises unresolved name error
```

Later stages are only reached if earlier ones return `None`. The DuckDB
catalog always wins — `blobrange` never shadows an existing catalog entry.

---

## Excel object types

### 1. ListObjects (Excel Tables)

ListObjects are the best-behaved case. An Excel Table (`Ctrl+T`) has an
explicit header row, a defined body range, and a name visible in the Name Box.
The schema is unambiguous.

```
products (ListObject, Sheet2)
┌─────────────┬─────────────┬───────────┬────────────┐
│ product_id  │ category    │ unit_price│ in_stock   │
├─────────────┼─────────────┼───────────┼────────────┤
│ PRD-001     │ Electronics │ 299.99    │ 150        │
│ PRD-002     │ Furniture   │ 549.00    │ 42         │
└─────────────┴─────────────┴───────────┴────────────┘
```

Resolution: `worksheet.ListObjects['products']` gives column names and
body data directly. No shape inference required. Column types are inferred
from the body values.

ListObjects are the primary target for the first implementation milestone.
They represent the most common pattern in structured business workbooks —
product catalogs, pricing tables, parameter grids — and require the least
inference.

### 2. Named ranges

Named ranges are rectangular regions of cells with a workbook- or
worksheet-scoped name. They may or may not have a header row. The schema must
be inferred.

Shape inference rules (applied in order):

1. If the first row contains only string values and no subsequent row does —
   treat as header row; remaining rows are data.
2. If the range is a single row — treat as a single data row with positional
   column names (`col_0`, `col_1`, ...).
3. Otherwise — treat first row as header if all cells are non-empty strings;
   use positional names as fallback.

Column types are inferred from the first non-null value in each column.
Cells containing Excel error values (`#N/A`, `#REF!`, `#VALUE!`, etc.) are
mapped to NULL.

Named ranges also cover the **scalar parameter** case — a single-cell named
range is a relation with one row and one column, but more usefully it is a
query parameter. The expected pattern is:

```sql
-- 'report_date' is a single-cell named range containing a date value
SELECT *
FROM orders
WHERE order_date <= (SELECT col_0 FROM report_date)
```

A convenience macro or function for scalar extraction may be warranted.

### 3. Zero-argument LAMBDAs returning dynamic arrays

A worksheet-scoped LAMBDA with no parameters that returns a dynamic array is
semantically a table-valued function — a named, parameterless computation that
produces a relation. Examples:

```
=ACTIVE_PRODUCTS    defined as =FILTER(products, products[in_stock]>0)
=RECENT_ORDERS      defined as =FILTER(orders, orders[order_date]>TODAY()-30)
```

These are the Excel user's natural abstraction mechanism — they encapsulate
domain logic under a meaningful name, using Excel's formula vocabulary.

Resolution requires evaluating the LAMBDA from within the PyXLL context and
reading the resulting spilled range. The result is then treated as a named
range (shape inference applies).

This is the most complex case due to Excel's asynchronous calculation model.
Evaluation must be performed in a context where calculation is complete and
the result range is available. Implementation is deferred to a later milestone
pending investigation of PyXLL's synchronous evaluation options.

---

## The `blobrange` LAMBDA registry

For the zero-argument LAMBDA case to participate in the staged computation
model (see below), LAMBDAs must be registered. Registration records the
LAMBDA's name, its Excel formula (the Stage 1 / sample-side implementation),
and optionally a SQL equivalent (the Stage 2 / production-side implementation).

Registration is stored in the rule4 catalog:

```sql
CREATE TABLE blobrange.lambda_registry (
    lambda_name         TEXT PRIMARY KEY,
    scope               TEXT,         -- 'workbook' | 'worksheet:<name>'
    excel_formula       TEXT,         -- the LAMBDA body as a formula string
    sql_template        TEXT,         -- SQLGlot template for Stage 2 (nullable)
    description         TEXT,
    registered_at       TIMESTAMPTZ,
    tags                JSON
);
```

An unregistered zero-arg LAMBDA can still be resolved (evaluated and read as
a range) but cannot participate in the Stage 2 promotion path.

---

## Staged computation and the promotion path

`blobrange` is designed as the Excel side of a two-stage computation model.

**Stage 1 — prototype / whipupitude**

Excel LAMBDAs and named ranges operate over sample or full data as the user
works. DuckDB joins them against other sources via the replacement scan.
Vega-Lite renders the results in a PyXLL-hosted Qt WebEngine panel. The user
works entirely within Excel's familiar idiom — formula authoring, named ranges,
table references — without thinking about scale or deployment.

**Stage 2 — production**

When the user is satisfied with the computation, they promote it. The
replacement scan resolves which relations came from Excel and which from
other sources. Registered LAMBDAs are translated to their SQL equivalents
via the `lambda_registry`. The full query is emitted as SQL (via SQLGlot
for dialect targeting) and submitted against production datasets, returning
a handle — a UUID stored in a cell — that can be used to rendezvous with the
result later.

The Excel formula that produced the prototype is the *specification*. The
submitted SQL is its *lifted* equivalent. They have the same denotation over
the same data; they differ only in execution environment and scale.

The forcing operations exposed as PyXLL xl_funcs:

| Function | Behaviour |
|---|---|
| `=BLOBRANGE.FORCE(query_cell)` | Force locally via DuckDB, return array to sheet |
| `=BLOBRANGE.SUBMIT(query_cell)` | Force remotely, return handle UUID |
| `=BLOBRANGE.PREVIEW(query_cell, n)` | Force against first n rows, return array |
| `=BLOBRANGE.EXPLAIN(query_cell)` | Return the translated SQL string, do not execute |
| `=BLOBRANGE.STATUS(handle_cell)` | Poll job status from the handle table |

`EXPLAIN` is particularly important for transparency — the user can inspect
the SQL that would be submitted before committing.

---

## Lazy ASTs: LAMBDAs as computation descriptions

The deepest formulation of `blobrange`'s role in the staged model: a
registered LAMBDA need not return a *value* — it can return a *description*
of the computation, a data structure (an AST node) that can be composed,
inspected, translated, and forced at a chosen time and location.

```
=ACTIVE_PRODUCTS(A1)  →  { "op": "filter",
                            "relation": "products",
                            "predicate": {"field": "in_stock", "gt": 0},
                            "as_of": <value of A1> }
```

Composition over descriptions builds a query AST in Excel cells, without
touching any data. Forcing is an explicit, separate act.

This is equivalent in structure to SQLAlchemy's query objects: method chains
build an AST; `.all()` forces it. The difference is that the host language is
Excel, the AST lives in cells, and the forcing operations are formula
functions visible to the workbook author.

This capability is aspirational for the initial implementation. It requires
the `lambda_registry` to record AST constructor functions alongside SQL
templates, and the forcing operations to walk the AST rather than executing
Excel formulas directly. It is documented here as design intent so that
implementation decisions do not foreclose it.

---

## Relationship to the WebEngine rendering pipeline

`blobrange` is the data-side complement to the PyXLL-hosted Vega-Lite
rendering pipeline:

```
Excel ListObject / named range / LAMBDA
    → blobrange replacement scan
        → DuckDB assembly CTE
            → Vega-Lite spec JSON (via blobtemplates + json_object)
                → vega-embed in Qt WebEngine CTP
                    → rendered chart updates in the task pane
```

Named ranges bound to formula cells serve as query parameters. A user editing
a cell value (a date, a product category, a price threshold) causes the DuckDB query
to re-execute and the chart to update. The Excel cell is the control; the
WebEngine panel is the view.

QWebChannel provides the bidirectional bridge: Python pushes updated spec JSON
to the JS side; Vega-Lite selection events (brushed ranges, clicked bars) flow
back to Python, which can write selected values into named ranges, closing the
loop.

---

## Metadata and rule4 integration

The replacement scan callback, when it resolves a name, records what it found:
the object type, the workbook and worksheet path, the inferred schema, and the
resolution timestamp. This metadata is written into the rule4 catalog:

```sql
CREATE TABLE blobrange.resolved_objects (
    object_name         TEXT,
    object_type         TEXT,    -- 'list_object' | 'named_range' | 'lambda'
    workbook_path       TEXT,
    worksheet_name      TEXT,
    inferred_schema     JSON,    -- column names and inferred types
    resolved_at         TIMESTAMPTZ,
    row_count           INTEGER
);
```

This gives rule4 visibility into the Excel side of the data landscape — the
same catalog machinery that describes SQL Server schemas and Parquet files
can describe the workbook's named objects. The domain detection and
fingerprinting machinery in `blobfilters` applies without modification:
a ListObject column with high containment against a known domain fingerprint
is classified the same way as any other column source.

---

## Open items

- [ ] PyXLL replacement scan registration — verify the DuckDB Python API
      surface for `register_replacement_scan` and confirm it is accessible
      from a PyXLL-hosted DuckDB connection
- [ ] ListObject resolution — implement and test the primary path; verify
      column type inference against Excel's type system (dates in particular
      need care — Excel stores dates as floats)
- [ ] Named range resolution — implement shape inference; handle single-cell
      scalar case; handle error cell values → NULL mapping
- [ ] Schema caching — replacement scans fire per query planning; cache
      the resolved schema (keyed by object name + workbook calculation version)
      to avoid redundant range reads
- [ ] Zero-arg LAMBDA evaluation — investigate PyXLL synchronous evaluation
      options; understand interaction with Excel's calculation state
- [ ] `blobrange.resolved_objects` catalog table — implement write-back from
      resolution callback
- [ ] `lambda_registry` schema and write path — define registration UX
      (manual SQL INSERT vs. a PyXLL ribbon button vs. auto-registration on
      first resolution)
- [ ] Forcing operations (`BLOBRANGE.FORCE`, `BLOBRANGE.SUBMIT`, etc.) —
      implement as PyXLL xl_funcs; define the handle table schema
- [ ] Vega-Lite pipeline integration — wire the WebEngine CTP to a named
      range containing a spec JSON string; test QWebChannel parameter
      push/pull
- [ ] `blobfilters` integration — fingerprint ListObject columns on
      registration; write domain classification results to rule4 catalog
- [ ] Lazy AST design — design the AST node schema (must be representable
      as a DuckDB STRUCT and inspectable as a spilled Excel array); design
      the AST constructor protocol for registered LAMBDAs
