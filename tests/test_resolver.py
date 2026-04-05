"""Tests for the ExcelResolver replacement scan callback."""

import pandas as pd

from blobrange.resolver import ExcelResolver


class MockListObject:
    """Fake Excel ListObject for testing."""

    def __init__(self, name: str, columns: list[str], data: tuple):
        self._name = name
        self._columns = columns
        self._data = data
        self.Parent = MockParent(name)

    @property
    def Name(self):
        return self._name

    @property
    def HeaderRowRange(self):
        return MockRange((tuple(self._columns),))

    @property
    def DataBodyRange(self):
        if not self._data:
            return None
        return MockRange(self._data)

    @property
    def ListColumns(self):
        return [type("Col", (), {"Name": c})() for c in self._columns]


class MockRange:
    def __init__(self, values):
        self._values = values

    @property
    def Value(self):
        return self._values

    def Rows(self, n):
        return self

    def Cells(self, row, col):
        return type("Cell", (), {"NumberFormat": None})()


class MockParent:
    """Fake worksheet/workbook parent chain."""

    def __init__(self, name):
        self.Name = "Sheet1"
        self.Parent = type("WB", (), {"FullName": "test.xlsx"})()


class MockExcelApp:
    """Fake Excel Application with controllable ListObjects."""

    def __init__(self, list_objects: dict[str, MockListObject]):
        self._list_objects = list_objects

    @property
    def Workbooks(self):
        return [self]

    @property
    def Worksheets(self):
        return [self]

    def ListObjects(self, name):
        if name in self._list_objects:
            return self._list_objects[name]
        raise Exception(f"ListObject '{name}' not found")


class TestExcelResolver:
    def test_resolves_listobject(self):
        products = MockListObject(
            "products",
            ["id", "name", "price"],
            ((1.0, "Widget", 9.99), (2.0, "Gadget", 19.50)),
        )
        app = MockExcelApp({"products": products})
        resolver = ExcelResolver(app)

        result = resolver("products")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2
        assert list(result.columns) == ["id", "name", "price"]

    def test_returns_none_for_unknown(self):
        app = MockExcelApp({})
        resolver = ExcelResolver(app)

        result = resolver("nonexistent")
        assert result is None

    def test_caches_resolved_object(self):
        products = MockListObject(
            "products",
            ["id", "name"],
            ((1.0, "Widget"),),
        )
        app = MockExcelApp({"products": products})
        resolver = ExcelResolver(app)

        resolver("products")
        assert "products" in resolver.cached_names

    def test_invalidate_single(self):
        products = MockListObject("products", ["id"], ((1.0,),))
        app = MockExcelApp({"products": products})
        resolver = ExcelResolver(app)

        resolver("products")
        resolver.invalidate("products")
        assert "products" not in resolver.cached_names

    def test_invalidate_all(self):
        products = MockListObject("products", ["id"], ((1.0,),))
        orders = MockListObject("orders", ["id"], ((1.0,),))
        app = MockExcelApp({"products": products, "orders": orders})
        resolver = ExcelResolver(app)

        resolver("products")
        resolver("orders")
        resolver.invalidate()
        assert resolver.cached_names == []

    def test_cache_hit_rereads_from_excel(self):
        """On cache hit, the resolver should still re-read from Excel
        to pick up changes (read-through behavior)."""
        products = MockListObject(
            "products",
            ["id", "name"],
            ((1.0, "Widget"),),
        )
        app = MockExcelApp({"products": products})
        resolver = ExcelResolver(app)

        # First call populates cache
        df1 = resolver("products")
        assert len(df1) == 1

        # Mutate the underlying data
        products._data = ((1.0, "Widget"), (2.0, "Gadget"))

        # Second call should return updated data
        df2 = resolver("products")
        assert len(df2) == 2
