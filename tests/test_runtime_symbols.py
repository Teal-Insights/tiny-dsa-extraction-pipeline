"""Unit tests for dynamic runtime symbol discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime_symbols import (
    discover_allowed_formula_symbols,
    discover_allowed_reader_symbols,
    discover_allowed_runtime_symbols,
)

_FIXTURE_RUNTIME = Path(__file__).resolve().parent / "fixtures" / "sample_runtime.py"


def test_discovers_public_functions_and_xlerror() -> None:
    symbols = discover_allowed_runtime_symbols(_FIXTURE_RUNTIME)
    assert symbols == ("XlError", "xl_add", "xl_cell")


def test_excludes_sentinel_returning_helpers() -> None:
    symbols = discover_allowed_runtime_symbols(_FIXTURE_RUNTIME)
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
