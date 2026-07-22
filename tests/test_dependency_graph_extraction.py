from __future__ import annotations

import json
from typing import Any

from src.extraction_pipeline import count_provenance_edges


def test_provenance_edge_count_is_positive(
    synthetic_graph_extraction_artifacts,
) -> None:
    assert synthetic_graph_extraction_artifacts.summary["provenance_edge_count"] > 0


def test_graph_edges_carry_provenance_metadata(
    synthetic_graph_extraction_artifacts,
) -> None:
    graph = synthetic_graph_extraction_artifacts.extraction.graph
    assert count_provenance_edges(graph) > 0


def test_synthetic_graph_fixture_captures_dependency_provenance(
    synthetic_graph,
) -> None:
    assert count_provenance_edges(synthetic_graph) > 0


def test_extraction_writes_expected_artifact_files(
    synthetic_graph_extraction_artifacts,
) -> None:
    output_dir = synthetic_graph_extraction_artifacts.config.graph_output_dir
    assert (output_dir / "index.html").is_file()
    assert (output_dir / "dependency-graph.json").is_file()
    assert (output_dir / "extraction-summary.json").is_file()


def test_extraction_summary_includes_core_metrics(
    synthetic_graph_extraction_artifacts,
) -> None:
    summary = synthetic_graph_extraction_artifacts.summary
    for key in ("node_count", "edge_count", "leaf_count", "stage_timings"):
        assert key in summary
    assert summary["node_count"] > 0
    assert summary["edge_count"] > 0
    assert summary["stage_timings"]


def test_extraction_summary_on_disk_matches_fixture_summary(
    synthetic_graph_extraction_artifacts,
) -> None:
    summary_path = (
        synthetic_graph_extraction_artifacts.config.graph_output_dir
        / "extraction-summary.json"
    )
    on_disk: dict[str, Any] = json.loads(summary_path.read_text(encoding="utf-8"))
    assert on_disk == synthetic_graph_extraction_artifacts.summary


def test_output_paths_are_relative_to_config_graph_output_dir(
    synthetic_graph_extraction_artifacts,
) -> None:
    output_dir = synthetic_graph_extraction_artifacts.config.graph_output_dir
    paths = synthetic_graph_extraction_artifacts.summary["output_paths"]
    assert paths["index_html"].endswith(f"{output_dir.name}/index.html")
    assert paths["dependency_graph_json"].endswith(
        f"{output_dir.name}/dependency-graph.json"
    )
    assert paths["extraction_summary_json"].endswith(
        f"{output_dir.name}/extraction-summary.json"
    )
