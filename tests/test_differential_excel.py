"""Unit tests for Excel golden-master read helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from excel_grapher import XlError

from tests.differential.differential_excel import coerce_excel_error, read_cell_value


def test_read_cell_value_uses_err_to_str() -> None:
    range_obj = MagicMock()
    range_obj.options.return_value = range_obj
    range_obj.value = "#N/A"

    sheet = MagicMock()
    sheet.range.return_value = range_obj
    sheets = {"Outputs": sheet}

    assert read_cell_value(sheets, "Outputs!B1") == "#N/A"
    sheet.range.assert_called_once_with("B1")
    range_obj.options.assert_called_once_with(err_to_str=True)


def test_matched_error_values() -> None:
    from excel_grapher import XlError

    from tests.differential.differential_excel import matched_error_values

    assert matched_error_values("#VALUE!", XlError.VALUE) is True
    assert matched_error_values("#VALUE!", "#N/A") is False


def test_coerce_excel_error_normalizes_error_strings() -> None:
    assert coerce_excel_error("#VALUE!") is XlError.VALUE
    assert coerce_excel_error(1.0) == 1.0
