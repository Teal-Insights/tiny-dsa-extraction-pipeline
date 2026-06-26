import importlib
import sys
from pathlib import Path

from excel_grapher.exporter import BaseProjectionManifest, CodeGenerator

from src.extraction_pipeline import graph, series_bindings, targets, workbook_path
from src.subgraph_projection import build_tiny_dsa_refactor_projection


def _base_manifest(manifest: object) -> BaseProjectionManifest:
    assert isinstance(manifest, BaseProjectionManifest)
    return manifest


def test_optimal_refactor_projection_compresses_with_provenance() -> None:
    projection = build_tiny_dsa_refactor_projection(graph)
    manifest = _base_manifest(projection.manifest)

    assert len(projection) == 59
    assert len(graph) - len(projection) == 22
    assert manifest.kind == "optimal_compression"
    assert len(manifest.collapsed_groups) == 11
    assert "Engine!B6" not in projection
    assert "Engine!C6" in projection
    assert "Engine!C10" in projection
    assert "Engine!C14" not in projection
    assert "Engine!C20" in projection


def test_projected_codegen_preserves_public_series_api(tmp_path: Path) -> None:
    projection = build_tiny_dsa_refactor_projection(graph)
    modules = CodeGenerator(projection).generate_modules(
        targets,
        series_bindings=series_bindings,
        bindings_workbook=workbook_path,
    )

    assert "def compute_output_baseline" in modules["api.py"]
    assert "def compute_output_shocked" in modules["api.py"]
    assert "def compute_output_delta" in modules["api.py"]
    assert "def cell_engine_c10" in modules["internals.py"]
    assert "def cell_engine_c14" not in modules["internals.py"]
    assert "def cell_engine_c20" in modules["internals.py"]
    assert "def cell_engine_b6" not in modules["internals.py"]

    package_dir = tmp_path / "tiny_dsa_projected"
    package_dir.mkdir()
    for filename, code in modules.items():
        (package_dir / filename).write_text(code, encoding="utf-8")

    sys.path.insert(0, str(tmp_path))
    try:
        api = importlib.import_module("tiny_dsa_projected.api")
        assert len(api.compute_output_baseline()) == 5
        assert len(api.compute_output_shocked()) == 5
        assert len(api.compute_output_delta()) == 5
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == "tiny_dsa_projected" or name.startswith("tiny_dsa_projected."):
                del sys.modules[name]
