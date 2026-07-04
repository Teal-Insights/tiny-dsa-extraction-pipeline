"""Unit tests for dynamic runtime symbol discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime_symbols import discover_allowed_runtime_symbols

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
        "\n".join(
            [
                "class XlError(Exception):",
                "    pass",
                "",
                "def xl_eval():",
                "    return 1",
                "",
            ]
        ),
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
