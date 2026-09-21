"""Output compute helpers (series-bindings schema 1.10.0+).

When an internals helper covers a published output series' leaves, the series
declares ``output.compute.helper`` so generated ``compute_*`` calls the helper
from record dims instead of ``xl_cell(address)``. The codegen cache must prune
the ``_output_leaves.py`` module excel-grapher emits alongside helper-backed
computes.
"""

from __future__ import annotations

from pathlib import Path

from src.codegen_cache import OPTIONAL_GENERATED_MODULES, write_generated_modules


def test_optional_generated_modules_include_output_leaves(tmp_path: Path) -> None:
    assert "_output_leaves.py" in OPTIONAL_GENERATED_MODULES
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "_output_leaves.py").write_text("stale\n", encoding="utf-8")
    write_generated_modules(package_root, {"api.py": "def api():\n    return 1\n"})
    assert not (package_root / "_output_leaves.py").is_file()
