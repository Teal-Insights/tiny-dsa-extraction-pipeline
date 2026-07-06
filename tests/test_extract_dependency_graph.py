from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.extraction_pipeline import (
    count_provenance_edges,
    extract_dependency_graph,
    main,
)
from src.pipeline_config import load_pipeline_config
from src.semantic_labeling import SemanticLabelingSummary


def test_count_provenance_edges_on_synthetic_graph(synthetic_graph) -> None:
    assert count_provenance_edges(synthetic_graph) >= 0


def test_extract_dependency_graph_writes_artifacts(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "dependency-graph"
    config = replace(
        synthetic_pipeline_config_fixture,
        graph_output_dir=output_dir,
    )
    stub_summary = SemanticLabelingSummary(
        labeled_cell_count=0,
        sheet_count=0,
        candidate_cells_by_sheet={},
    )

    with patch(
        "src.extraction_pipeline.label_internal_graph_cells",
        return_value=stub_summary,
    ):
        summary = extract_dependency_graph(config)

    assert (output_dir / "index.html").is_file()
    assert (output_dir / "dependency-graph.json").is_file()
    assert (output_dir / "dependencies.dot").is_file()
    assert (output_dir / "graph-topology.json").is_file()
    summary_path = output_dir / "extraction-summary.json"
    assert summary_path.is_file()

    on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
    assert on_disk == summary
    assert summary["schema_version"] == "1.0.0"
    assert summary["node_count"] == 6
    assert summary["edge_count"] == 6
    assert summary["leaf_count"] == 2
    assert "provenance_edge_count" in summary
    assert summary["elapsed_seconds"] >= 0
    assert summary["stage_timings"]
    assert summary["output_paths"]["index_html"].endswith("dependency-graph/index.html")


def test_extract_graph_cli_exits_zero_on_synthetic_workbook(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "artifacts" / "dependency-graph"
    config = replace(
        synthetic_pipeline_config_fixture,
        graph_output_dir=output_dir,
    )
    stub_summary = SemanticLabelingSummary(
        labeled_cell_count=0,
        sheet_count=0,
        candidate_cells_by_sheet={},
    )

    with patch("src.extraction_pipeline.load_pipeline_config", return_value=config):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch(
                    "src.extraction_pipeline.label_internal_graph_cells",
                    return_value=stub_summary,
                ):
                    main(["--extract-graph"])

    assert (output_dir / "extraction-summary.json").is_file()


def test_main_without_extract_graph_flag_runs_export(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch(
                    "src.extraction_pipeline.export_generated_package"
                ) as export:
                    with patch(
                        "src.documentation_pipeline.run_documentation_pipeline"
                    ) as document:
                        main([])

    export.assert_called_once()
    document.assert_called_once()


def test_load_pipeline_config_default_graph_output_dir() -> None:
    config = load_pipeline_config()
    assert config.graph_output_dir.name == "dependency-graph"
    assert config.graph_output_dir.parent.name == "artifacts"
