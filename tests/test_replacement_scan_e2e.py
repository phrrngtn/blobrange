"""End-to-end test for the replacement scan callback API.

Requires the patched duckdb-python with add_replacement_scan support.
Skip gracefully if the method doesn't exist (stock duckdb).
"""

import pandas as pd
import pytest

import duckdb


requires_patched_duckdb = pytest.mark.skipif(
    not hasattr(duckdb.DuckDBPyConnection, "add_replacement_scan"),
    reason="Requires patched duckdb-python with add_replacement_scan",
)


def make_resolver(tables: dict[str, pd.DataFrame]):
    """Create a simple resolver callback from a dict of name → DataFrame."""

    def resolver(table_name: str) -> pd.DataFrame | None:
        return tables.get(table_name)

    return resolver


@requires_patched_duckdb
class TestReplacementScanCallback:
    def test_simple_select(self):
        products = pd.DataFrame(
            {"id": [1, 2, 3], "name": ["Widget", "Gadget", "Doohickey"], "price": [9.99, 19.50, 4.99]}
        )
        resolver = make_resolver({"products": products})

        con = duckdb.connect()
        con.add_replacement_scan(resolver)
        result = con.sql("SELECT * FROM products WHERE price > 10").fetchdf()

        assert len(result) == 1
        assert result["name"].iloc[0] == "Gadget"

    def test_join_two_resolved_tables(self):
        products = pd.DataFrame({"id": [1, 2], "name": ["Widget", "Gadget"]})
        orders = pd.DataFrame({"order_id": [10, 20], "product_id": [1, 2], "qty": [5, 3]})
        resolver = make_resolver({"products": products, "orders": orders})

        con = duckdb.connect()
        con.add_replacement_scan(resolver)
        result = con.sql("""
            SELECT p.name, o.qty
            FROM products AS p
            JOIN orders AS o ON p.id = o.product_id
            ORDER BY p.name
        """).fetchdf()

        assert len(result) == 2
        assert result["name"].tolist() == ["Gadget", "Widget"]
        assert result["qty"].tolist() == [3, 5]

    def test_mixed_resolved_and_native(self):
        """A query joining a replacement-scanned table with a native DuckDB table."""
        products = pd.DataFrame({"id": [1, 2], "name": ["Widget", "Gadget"]})
        resolver = make_resolver({"products": products})

        con = duckdb.connect()
        con.add_replacement_scan(resolver)
        con.execute("CREATE TABLE prices (product_id INT, price DECIMAL(10,2))")
        con.execute("INSERT INTO prices VALUES (1, 9.99), (2, 19.50)")

        result = con.sql("""
            SELECT p.name, pr.price
            FROM products AS p
            JOIN prices AS pr ON p.id = pr.product_id
            ORDER BY p.name
        """).fetchdf()

        assert len(result) == 2
        assert result["name"].tolist() == ["Gadget", "Widget"]

    def test_returns_none_falls_through(self):
        """Resolver returning None should let DuckDB raise the normal error."""
        resolver = make_resolver({})  # empty — always returns None

        con = duckdb.connect()
        con.add_replacement_scan(resolver)

        with pytest.raises(duckdb.CatalogException):
            con.sql("SELECT * FROM nonexistent")

    def test_catalog_wins_over_resolver(self):
        """A table already in the DuckDB catalog should NOT go through the resolver."""
        call_log = []

        def logging_resolver(table_name: str):
            call_log.append(table_name)
            return pd.DataFrame({"x": [999]})

        con = duckdb.connect()
        con.add_replacement_scan(logging_resolver)
        con.execute("CREATE TABLE products (x INT)")
        con.execute("INSERT INTO products VALUES (1)")

        result = con.sql("SELECT * FROM products").fetchdf()
        # Should get the catalog version, not the resolver version
        assert result["x"].iloc[0] == 1
        # Resolver should not have been called for 'products'
        assert "products" not in call_log

    def test_resolver_called_per_query(self):
        """The resolver should be called each time the name is encountered."""
        call_count = 0

        def counting_resolver(table_name: str):
            nonlocal call_count
            call_count += 1
            return pd.DataFrame({"val": [call_count]})

        con = duckdb.connect()
        con.add_replacement_scan(counting_resolver)

        r1 = con.sql("SELECT * FROM dynamic_data").fetchdf()
        r2 = con.sql("SELECT * FROM dynamic_data").fetchdf()

        # Each query should trigger a fresh call
        assert call_count >= 2
