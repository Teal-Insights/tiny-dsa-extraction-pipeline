from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.extraction_pipeline import (
    build_pipeline_graph,
    count_provenance_edges,
    export_generated_package,
    extract_dependency_graph,
    main,
)
from src.pipeline_config import load_pipeline_config


def _write_empty_placeholder_bindings(bindings_dir: Path) -> None:
    """Bootstrap shards with empty series lists and divergent concept schemes."""
    bindings_dir.mkdir(parents=True, exist_ok=True)
    for name, scheme_id in (
        ("inputs.bindings.yaml", "inputs_placeholder"),
        ("outputs.bindings.yaml", "outputs_placeholder"),
        ("internals.bindings.yaml", "internals_placeholder"),
    ):
        (bindings_dir / name).write_text(
            (
                "schema_version: 1.13.0\n"
                "workbook: workbook.xlsx\n"
                "concept_scheme:\n"
                f"  id: {scheme_id}\n"
                "  concepts: []\n"
                "series: []\n"
            ),
            encoding="utf-8",
        )


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

    result = extract_dependency_graph(config)
    summary = result.summary

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
    assert summary["elapsed_seconds"] >= sum(summary["stage_timings"].values()) - 0.01
    assert summary["output_paths"]["index_html"].endswith("dependency-graph/index.html")
    assert result.graph_cache_key
    assert len(result.graph) == 6


def test_extract_dependency_graph_succeeds_with_empty_binding_shards(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Extract is graph-first: empty placeholder shards must not block the graph."""
    bindings = tmp_path / "bindings"
    _write_empty_placeholder_bindings(bindings)
    output_dir = tmp_path / "dependency-graph"
    config = replace(
        synthetic_pipeline_config_fixture,
        bindings_path=bindings,
        graph_output_dir=output_dir,
    )

    result = extract_dependency_graph(config)

    assert (output_dir / "index.html").is_file()
    assert (output_dir / "dependency-graph.json").is_file()
    assert (output_dir / "extraction-summary.json").is_file()
    assert result.graph_cache_key
    assert len(result.graph) == 6


def test_build_pipeline_graph_accepts_empty_binding_shards(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """excel-grapher 5.1.4+ loads empty ``series: []`` placeholders (unioned schemes)."""
    bindings = tmp_path / "bindings"
    _write_empty_placeholder_bindings(bindings)
    config = replace(
        synthetic_pipeline_config_fixture,
        bindings_path=bindings,
        graph_output_dir=tmp_path / "dependency-graph",
    )

    result = build_pipeline_graph(config)

    assert result.series_bindings["series"] == []
    assert result.input_series == []
    assert result.output_series == []
    assert result.internal_series == []
    assert result.graph_cache_key
    assert len(result.graph) == 6


def test_extract_dependency_graph_succeeds_without_binding_yaml_files(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    bindings = tmp_path / "bindings"
    bindings.mkdir()
    output_dir = tmp_path / "dependency-graph"
    config = replace(
        synthetic_pipeline_config_fixture,
        bindings_path=bindings,
        graph_output_dir=output_dir,
    )

    result = extract_dependency_graph(config)

    assert (output_dir / "index.html").is_file()
    assert result.graph_cache_key
    assert len(result.graph) == 6


def test_extract_graph_cli_exits_zero_on_synthetic_workbook(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "artifacts" / "dependency-graph"
    config = replace(
        synthetic_pipeline_config_fixture,
        graph_output_dir=output_dir,
    )

    with (
        patch("src.extraction_pipeline.load_pipeline_config", return_value=config),
        patch("src.extraction_pipeline.validate_pipeline_config"),
    ):
        main(["--extract-graph"])

    assert (output_dir / "extraction-summary.json").is_file()


def test_main_without_extract_graph_flag_runs_full_pipeline(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch(
            "src.extraction_pipeline.load_pipeline_config",
            return_value=synthetic_pipeline_config_fixture,
        ),
        patch("src.extraction_pipeline.validate_pipeline_config"),
        patch("src.extraction_pipeline.run_pipeline") as pipeline,
    ):
        main([])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "document"


def test_load_pipeline_config_default_graph_output_dir() -> None:
    config = load_pipeline_config()
    assert config.graph_output_dir.name == "dependency-graph"
    assert config.graph_output_dir.parent.name == "artifacts"


def test_export_generated_package_writes_under_isolated_dist_root(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    config = replace(synthetic_pipeline_config_fixture, dist_root=dist_root)
    repo_pollution = (
        config.repo_root / "dist" / config.dist_metadata.package_name / "internals.py"
    )
    before = repo_pollution.read_bytes() if repo_pollution.is_file() else None

    def _write_package(*_args: object, **_kwargs: object) -> None:
        config.package_root.mkdir(parents=True, exist_ok=True)
        (config.package_root / "internals.py").write_text("pass\n", encoding="utf-8")

    with patch("src.extraction_pipeline.run_pipeline", side_effect=_write_package):
        export_generated_package(config)

    written = config.package_root / "internals.py"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "pass\n"
    assert written.resolve().is_relative_to(dist_root.resolve())
    if before is None:
        assert not repo_pollution.is_file()
    else:
        assert repo_pollution.read_bytes() == before
