"""Unit tests for dynamic runtime symbol discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime_symbols import (
    discover_allowed_formula_symbols,
    discover_allowed_reader_symbols,
    discover_allowed_runtime_symbols,
)

_SAMPLE_RUNTIME = """\
from enum import StrEnum


class XlError(StrEnum):
    VALUE = "#VALUE!"


def to_bool(value: object) -> bool | XlError:
    return True


def to_int(value: object) -> int | XlError:
    return 0


def to_number(value: object) -> float | XlError:
    return 0.0


def compare_scalars(op: str, left: object, right: object) -> bool | XlError:
    return True


def xl_add(left: float, right: float) -> float:
    return left + right


def xl_cell(ctx: object, address: str) -> object:
    return address
"""


@pytest.fixture
def sample_runtime(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.py"
    path.write_text(_SAMPLE_RUNTIME, encoding="utf-8")
    return path


def test_discovers_public_functions_and_xlerror(sample_runtime: Path) -> None:
    symbols = discover_allowed_runtime_symbols(sample_runtime)
    assert symbols == ("XlError", "xl_add", "xl_cell")


def test_excludes_sentinel_returning_helpers(sample_runtime: Path) -> None:
    symbols = discover_allowed_runtime_symbols(sample_runtime)
    assert "to_bool" not in symbols
    assert "to_int" not in symbols
    assert "to_number" not in symbols
    assert "compare_scalars" not in symbols


def test_verifies_symbols_are_importable(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "class XlError(Exception):\n    pass\n\ndef xl_eval():\n    return 1\n",
        encoding="utf-8",
    )
    assert discover_allowed_runtime_symbols(runtime_path) == ("XlError", "xl_eval")


def test_missing_runtime_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        discover_allowed_runtime_symbols(Path("/nonexistent/runtime.py"))


def test_runtime_module_load_failure_propagates(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text("raise ImportError('boom')\n", encoding="utf-8")
    with pytest.raises(ImportError, match="boom"):
        discover_allowed_runtime_symbols(runtime_path)


def test_discovers_public_reader_functions(tmp_path: Path) -> None:
    readers_path = tmp_path / "_readers.py"
    readers_path.write_text(
        "from .runtime import xl_cell\n\n_LEAF_INDEX = {(): 'Inputs!B1'}\n\ndef read_shock_type(ctx):\n    return xl_cell(ctx, 'Inputs!B1')\n\ndef read_country(ctx):\n    return xl_cell(ctx, 'Dashboard!C12')\n",
        encoding="utf-8",
    )
    assert discover_allowed_reader_symbols(readers_path) == (
        "read_country",
        "read_shock_type",
    )


def test_missing_readers_file_yields_empty_tuple(tmp_path: Path) -> None:
    assert discover_allowed_reader_symbols(tmp_path / "_readers.py") == ()


def test_formula_symbols_union_runtime_and_readers(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "class XlError(Exception):\n    pass\n\ndef xl_cell(ctx, address):\n    return address\n",
        encoding="utf-8",
    )
    readers_path = tmp_path / "_readers.py"
    readers_path.write_text(
        "def read_shock_type(ctx):\n    return 'level'\n",
        encoding="utf-8",
    )
    assert discover_allowed_formula_symbols(runtime_path, readers_path) == (
        "XlError",
        "read_shock_type",
        "xl_cell",
    )
