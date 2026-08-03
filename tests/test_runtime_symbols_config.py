"""Integration tests for path-scoped runtime symbol discovery."""

from __future__ import annotations

from pathlib import Path

from src.runtime_symbols import (
    allowed_runtime_symbols,
    clear_runtime_symbol_caches,
    discover_allowed_formula_symbols,
)


def _write_runtime(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "class XlError(Exception):",
                "    pass",
                "",
                "def to_bool(value):",
                "    return True",
                "",
                "def xl_eval():",
                "    return 1",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_allowed_runtime_symbols_uses_package_root(tmp_path: Path) -> None:
    package_root = tmp_path / "dist" / "my_model"
    _write_runtime(package_root / "runtime.py")
    (package_root / "_readers.py").write_text(
        "def read_shock_type(ctx):\n    return 'level'\n",
        encoding="utf-8",
    )

    clear_runtime_symbol_caches()
    symbols = allowed_runtime_symbols(package_root)

    assert symbols == discover_allowed_formula_symbols(
        package_root / "runtime.py",
        package_root / "_readers.py",
    )
    assert "to_bool" not in symbols
    assert symbols == ("XlError", "read_shock_type", "xl_eval")


def test_allowed_runtime_symbols_cache_is_keyed_by_package_root(
    tmp_path: Path,
) -> None:
    first = tmp_path / "pkg_a"
    second = tmp_path / "pkg_b"
    _write_runtime(first / "runtime.py")
    _write_runtime(second / "runtime.py")
    (second / "runtime.py").write_text(
        "class XlError(Exception):\n    pass\n\ndef xl_other():\n    return 2\n",
        encoding="utf-8",
    )

    clear_runtime_symbol_caches()
    assert "xl_eval" in allowed_runtime_symbols(first)
    assert "xl_other" in allowed_runtime_symbols(second)
    assert "xl_other" not in allowed_runtime_symbols(first)
