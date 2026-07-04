"""Excel golden-master read helpers for differential harnesses."""

from __future__ import annotations

from typing import Any

from excel_grapher import XlError


def read_cell_value(sheets: Any, address: str) -> Any:
    """Read one live Excel cell, preserving error codes as strings.

    xlwings converts ``#VALUE!``, ``#N/A``, and other error cells to ``None`` by
    default. ``err_to_str=True`` returns the literal error text so the golden
    oracle can be compared against typed ``XlError`` values from the SUT.
    """
    sheet, cell = address.split("!", 1)
    return sheets[sheet].range(cell).options(err_to_str=True).value


def coerce_excel_error(value: Any) -> Any:
    """Normalize xlwings error strings to typed ``XlError`` values."""
    if isinstance(value, str):
        err = XlError.from_text(value)
        if err is not None:
            return err
    return value


def matched_error_values(golden: Any, mvp: Any) -> bool:
    """Return whether both sides are the same typed Excel error."""
    golden = coerce_excel_error(golden)
    mvp = coerce_excel_error(mvp)
    return isinstance(golden, XlError) and isinstance(mvp, XlError) and golden == mvp
