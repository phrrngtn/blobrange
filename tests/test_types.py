"""Tests for Excel value → Arrow type inference."""

import pyarrow as pa

from blobrange.types import (
    coerce_value,
    excel_date_to_date,
    excel_date_to_datetime,
    infer_column_type,
    is_excel_error,
    is_integer_valued,
)


class TestIsExcelError:
    def test_na_error(self):
        assert is_excel_error(-2146826281)

    def test_ref_error(self):
        assert is_excel_error(-2146826246)

    def test_not_error(self):
        assert not is_excel_error(42)
        assert not is_excel_error(0)
        assert not is_excel_error("hello")


class TestIsIntegerValued:
    def test_integer_float(self):
        assert is_integer_valued(42.0)
        assert is_integer_valued(0.0)
        assert is_integer_valued(-7.0)

    def test_fractional_float(self):
        assert not is_integer_valued(42.5)
        assert not is_integer_valued(0.1)


class TestExcelDateConversion:
    def test_known_date(self):
        # 2024-01-15 = Excel serial 45306
        dt = excel_date_to_datetime(45306.0)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_date_only(self):
        d = excel_date_to_date(45306.0)
        assert d.year == 2024
        assert d.month == 1
        assert d.day == 15


class TestInferColumnType:
    def test_all_strings(self):
        assert infer_column_type(["a", "b", "c"]) == pa.string()

    def test_integer_floats(self):
        assert infer_column_type([1.0, 2.0, 3.0]) == pa.int64()

    def test_mixed_floats(self):
        assert infer_column_type([1.5, 2.0, 3.7]) == pa.float64()

    def test_booleans(self):
        assert infer_column_type([True, False, True]) == pa.bool_()

    def test_all_none(self):
        assert infer_column_type([None, None, None]) == pa.string()

    def test_none_then_string(self):
        assert infer_column_type([None, "hello", None]) == pa.string()

    def test_none_then_int(self):
        assert infer_column_type([None, 5.0, 10.0]) == pa.int64()

    def test_error_then_string(self):
        assert infer_column_type([-2146826281, "hello"]) == pa.string()

    def test_all_errors(self):
        assert infer_column_type([-2146826281, -2146826246]) == pa.string()

    def test_date_format_hint(self):
        assert infer_column_type([45306.0], number_format="yyyy-mm-dd") == pa.timestamp("us")

    def test_no_date_format(self):
        # Without format hint, looks like integer.
        assert infer_column_type([45306.0]) == pa.int64()


class TestCoerceValue:
    def test_none_to_any(self):
        assert coerce_value(None, pa.int64()) is None

    def test_error_to_any(self):
        assert coerce_value(-2146826281, pa.string()) is None

    def test_float_to_int(self):
        assert coerce_value(42.0, pa.int64()) == 42

    def test_float_to_float(self):
        assert coerce_value(42.5, pa.float64()) == 42.5

    def test_float_to_timestamp(self):
        dt = coerce_value(45306.0, pa.timestamp("us"))
        assert dt.year == 2024
        assert dt.month == 1

    def test_string_to_string(self):
        assert coerce_value("hello", pa.string()) == "hello"

    def test_bool_to_bool(self):
        assert coerce_value(True, pa.bool_()) is True
