from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import cast

import pytest
from excel_grapher.exporter import BaseProjectionManifest, CodeGenerator
from excel_grapher.exporter.codegen import GraphLike

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


def test_build_refactor_projection_forwards_cache_dir(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_graph,
) -> None:
    """Opt-in smokes need to pin cache_dir past pytest's redirected default."""
    captured: dict[str, object] = {}
    sentinel_dir = Path("/tmp/repo-projection-cache")

    def fake_get_or_build(
        graph: object,
        *,
        graph_cache_key: str,
        cache_dir: Path | None = None,
        no_cache: bool = False,
        force_rebuild: bool = False,
    ) -> object:
        del graph, no_cache, force_rebuild
        captured["graph_cache_key"] = graph_cache_key
        captured["cache_dir"] = cache_dir

        class _Result:
            projection = object()

        return _Result()

    monkeypatch.setattr(
        "src.subgraph_projection.get_or_build_refactor_projection",
        fake_get_or_build,
    )

    result = build_refactor_projection(
        synthetic_graph,
        graph_cache_key="test-graph-cache-key",
        cache_dir=sentinel_dir,
    )
    assert result is not None
    assert captured["graph_cache_key"] == "test-graph-cache-key"
    assert captured["cache_dir"] == sentinel_dir


def test_projected_codegen_preserves_public_series_api(
    tmp_path: Path,
    synthetic_graph,
    synthetic_series_bindings,
    synthetic_workbook_path,
) -> None:
    projection = build_refactor_projection(synthetic_graph)
    modules = CodeGenerator(cast(GraphLike, projection)).generate_modules(
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
