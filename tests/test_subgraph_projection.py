from __future__ import annotations

import importlib
import sys
from pathlib import Path

from excel_grapher.exporter import BaseProjectionManifest, CodeGenerator

from src.subgraph_projection import build_refactor_projection


def _base_manifest(manifest: object) -> BaseProjectionManifest:
    assert isinstance(manifest, BaseProjectionManifest)
    return manifest


def test_refactor_projection_preserves_targets_and_parallel_members(
    synthetic_graph,
) -> None:
    projection = build_refactor_projection(synthetic_graph)
    manifest = _base_manifest(projection.manifest)

    assert manifest.kind == "optimal_compression"
    assert "Engine!B2" in projection
    assert "Engine!C2" in projection
    assert "Outputs!B1" in projection
    assert "Outputs!C1" in projection
    assert "Inputs!A1" in projection


def test_projected_codegen_preserves_public_series_api(
    tmp_path: Path,
    synthetic_graph,
    synthetic_series_bindings,
    synthetic_workbook_path,
) -> None:
    projection = build_refactor_projection(synthetic_graph)
    modules = CodeGenerator(projection).generate_modules(
        list(synthetic_graph.target_keys()),
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_workbook_path,
    )

    assert "def compute_result_a" in modules["api.py"]
    assert "def compute_result_b" in modules["api.py"]
    assert "def set_input_rate" in modules["api.py"]
    assert (
        "def cell_engine_b2" in modules["internals.py"]
        or "Engine!B2" in modules["internals.py"]
    )

    package_dir = tmp_path / "synthetic_model_projected"
    package_dir.mkdir()
    for filename, code in modules.items():
        (package_dir / filename).write_text(code, encoding="utf-8")

    sys.path.insert(0, str(tmp_path))
    try:
        api = importlib.import_module("synthetic_model_projected.api")
        ctx = api.make_context()
        api.set_input_rate(ctx, 4.0)
        result_a = api.compute_result_a(ctx=ctx)
        result_b = api.compute_result_b(ctx=ctx)
        assert len(result_a) == 1
        assert len(result_b) == 1
        assert result_a[0]["OBS_VALUE"] == 5.0
        assert result_b[0]["OBS_VALUE"] == 5.0
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == "synthetic_model_projected" or name.startswith(
                "synthetic_model_projected."
            ):
                del sys.modules[name]
