"""Tests for ListObject → PyArrow / pandas conversion."""

import pandas as pd
import pyarrow as pa

from blobrange.listobject import raw_to_pandas, read_listobject_from_raw


class TestReadListObjectFromRaw:
    def test_basic_table(self):
        columns = ["product_id", "category", "unit_price", "in_stock"]
        data = (
            ("PRD-001", "Electronics", 299.99, 150.0),
            ("PRD-002", "Furniture", 549.00, 42.0),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.num_rows == 2
        assert table.num_columns == 4
        assert table.column_names == columns
        assert table.column("product_id").to_pylist() == ["PRD-001", "PRD-002"]
        assert table.column("category").to_pylist() == ["Electronics", "Furniture"]

    def test_integer_column(self):
        columns = ["id", "count"]
        data = (
            (1.0, 10.0),
            (2.0, 20.0),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.column("id").type == pa.int64()
        assert table.column("count").type == pa.int64()
        assert table.column("id").to_pylist() == [1, 2]

    def test_mixed_numeric(self):
        columns = ["name", "price"]
        data = (
            ("Widget", 9.99),
            ("Gadget", 19.50),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.column("name").type == pa.string()
        assert table.column("price").type == pa.float64()

    def test_null_values(self):
        columns = ["a", "b"]
        data = (
            ("x", None),
            (None, 5.0),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.column("a").to_pylist() == ["x", None]
        assert table.column("b").to_pylist() == [None, 5]

    def test_error_values_become_null(self):
        columns = ["val"]
        data = (
            (42.0,),
            (-2146826281,),  # #N/A
            (10.0,),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.column("val").to_pylist() == [42, None, 10]

    def test_boolean_column(self):
        columns = ["flag"]
        data = (
            (True,),
            (False,),
            (True,),
        )
        table = read_listobject_from_raw(columns, data)

        assert table.column("flag").type == pa.bool_()
        assert table.column("flag").to_pylist() == [True, False, True]

    def test_date_column_with_format(self):
        columns = ["order_date"]
        data = (
            (45306.0,),  # 2024-01-15
            (45307.0,),  # 2024-01-16
        )
        table = read_listobject_from_raw(columns, data, number_formats=["yyyy-mm-dd"])

        assert pa.types.is_timestamp(table.column("order_date").type)
        dates = table.column("order_date").to_pylist()
        assert dates[0].year == 2024
        assert dates[0].month == 1
        assert dates[0].day == 15

    def test_single_row(self):
        columns = ["x"]
        data = (("only",),)
        table = read_listobject_from_raw(columns, data)

        assert table.num_rows == 1
        assert table.column("x").to_pylist() == ["only"]

    def test_empty_data(self):
        columns = ["a", "b"]
        data = ()
        table = read_listobject_from_raw(columns, data)

        assert table.num_rows == 0
        assert table.column_names == ["a", "b"]


class TestRawToPandas:
    def test_basic_table(self):
        columns = ["id", "name", "price"]
        data = (
            (1.0, "Widget", 9.99),
            (2.0, "Gadget", 19.50),
        )
        df = raw_to_pandas(columns, data)

        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == columns
        assert len(df) == 2
        assert df["name"].tolist() == ["Widget", "Gadget"]

    def test_error_values_become_nan(self):
        columns = ["val"]
        data = (
            (42.0,),
            (-2146826281,),  # #N/A
            (10.0,),
        )
        df = raw_to_pandas(columns, data)

        assert df["val"].tolist()[0] == 42.0
        assert pd.isna(df["val"].tolist()[1])
        assert df["val"].tolist()[2] == 10.0

    def test_empty_data(self):
        columns = ["a", "b"]
        data = ()
        df = raw_to_pandas(columns, data)

        assert len(df) == 0
        assert list(df.columns) == ["a", "b"]

    def test_single_row(self):
        columns = ["x"]
        data = (("only",),)
        df = raw_to_pandas(columns, data)

        assert len(df) == 1
        assert df["x"].tolist() == ["only"]

    def test_mixed_types(self):
        columns = ["name", "count", "active"]
        data = (
            ("A", 1.0, True),
            ("B", 2.0, False),
        )
        df = raw_to_pandas(columns, data)

        assert df["name"].tolist() == ["A", "B"]
        assert df["count"].tolist() == [1.0, 2.0]
        assert df["active"].tolist() == [True, False]
