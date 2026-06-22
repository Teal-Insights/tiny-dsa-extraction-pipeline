import importlib
import sys
from pathlib import Path

from excel_grapher.exporter import BaseProjectionManifest, CodeGenerator

from src.extraction_pipeline import graph, series_bindings, targets, workbook_path
from src.subgraph_projection import (
    TINY_DSA_HYPOTHESIS_GROUPS,
    SubgraphCollapse,
    build_tiny_dsa_refactor_projection,
)


def _base_manifest(manifest: object) -> BaseProjectionManifest:
    assert isinstance(manifest, BaseProjectionManifest)
    return manifest


def test_subgraph_collapse_projection_preserves_canonical_graph() -> None:
    projection = SubgraphCollapse(TINY_DSA_HYPOTHESIS_GROUPS).project(graph)

    assert "Engine!C10" in graph
    assert "Engine!C10" not in projection
    assert "Engine!C20" in projection
    assert _base_manifest(projection.manifest).forwarding_map == {}


def test_subgraph_collapse_records_group_lineage() -> None:
    labeled_graph = graph.copy()
    labeled_graph.set_node_metadata(
        "Engine!C10",
        {"row_labels": [{"label": "Shock active"}]},
    )
    projection = SubgraphCollapse(TINY_DSA_HYPOTHESIS_GROUPS).project(labeled_graph)
    manifest = _base_manifest(projection.manifest)

    assert manifest.retained_to_collapsed_sources["Engine!C20"] == (
        "Engine!C10",
        "Engine!C14",
        "Engine!C15",
        "Engine!C16",
    )
    assert manifest.removed_node_snapshots["Engine!C10"].metadata == {
        "row_labels": [{"label": "Shock active"}]
    }

    group = next(
        group for group in manifest.collapsed_groups if group.retained == "Engine!C20"
    )
    assert group.statement_order == (
        "Engine!C10",
        "Engine!C14",
        "Engine!C15",
        "Engine!C16",
        "Engine!C20",
    )
    assert group.external_dependencies == (
        "Engine!B20",
        "Engine!B9",
        "Engine!C5",
        "Inputs!B21",
        "Inputs!B22",
        "Inputs!C16",
        "Inputs!C17",
        "Inputs!C18",
    )

    round_tripped = BaseProjectionManifest.from_dict(manifest.to_dict())
    assert round_tripped.to_dict() == manifest.to_dict()


def test_subgraph_collapse_updates_root_formula_and_dependencies() -> None:
    projection = SubgraphCollapse(TINY_DSA_HYPOTHESIS_GROUPS).project(graph)
    node = projection.get_node("Engine!C20")

    assert node is not None
    assert node.normalized_formula is not None
    assert "Engine!C14" not in node.normalized_formula
    assert "Engine!C15" not in node.normalized_formula
    assert "Engine!C16" not in node.normalized_formula
    assert "IF(Engine!C5>=Inputs!B21,1,0)" in node.normalized_formula
    assert set(projection.get_dependencies("Engine!C20")) == {
        "Engine!B20",
        "Engine!B9",
        "Engine!C5",
        "Inputs!B21",
        "Inputs!B22",
        "Inputs!C16",
        "Inputs!C17",
        "Inputs!C18",
    }


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


def test_optimal_refactor_projection_matches_or_beats_human_hypothesis_size() -> None:
    optimal = build_tiny_dsa_refactor_projection(graph)
    hypothesis = SubgraphCollapse(TINY_DSA_HYPOTHESIS_GROUPS).project(graph)

    hypothesis_removed = set(graph) - set(hypothesis)
    optimal_removed = set(graph) - set(optimal)

    assert len(optimal) <= len(hypothesis)
    assert len(hypothesis_removed) == 21
    assert len(optimal_removed) == 22
    assert {
        "Engine!C10",
        "Engine!C16",
        "Engine!D10",
        "Engine!D16",
        "Engine!E10",
        "Engine!E16",
        "Engine!F10",
        "Engine!F16",
        "Engine!G10",
        "Engine!G16",
    } <= hypothesis_removed - optimal_removed
    assert {"Engine!B20", "Outputs!B13"} <= optimal_removed - hypothesis_removed


def test_projected_codegen_preserves_public_series_api(tmp_path: Path) -> None:
    projection = build_tiny_dsa_refactor_projection(graph)
    modules = CodeGenerator(projection).generate_modules(
        targets,
        series_bindings=series_bindings,
        bindings_workbook=workbook_path,
    )

    assert "def compute_output_baseline" in modules["api.py"]
    assert "def compute_output_shocked" in modules["api.py"]
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
