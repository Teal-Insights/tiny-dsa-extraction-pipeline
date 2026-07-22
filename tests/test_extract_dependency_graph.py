from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.extraction_pipeline import (
    count_provenance_edges,
    export_generated_package,
    extract_dependency_graph,
    main,
)
from src.pipeline_config import load_pipeline_config


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
    assert summary["elapsed_seconds"] >= sum(summary["stage_timings"].values()) - 0.01
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

    with patch("src.extraction_pipeline.load_pipeline_config", return_value=config):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                main(["--extract-graph"])

    assert (output_dir / "extraction-summary.json").is_file()


def test_main_without_extract_graph_flag_runs_full_pipeline(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch("src.extraction_pipeline.run_pipeline") as pipeline:
                    main([])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "document"


def test_load_pipeline_config_default_graph_output_dir() -> None:
    config = load_pipeline_config()
    assert config.graph_output_dir.name == "dependency-graph"
    assert config.graph_output_dir.parent.name == "artifacts"


def _export_generated_package_with_mocked_codegen(
    config,
    *,
    cluster_graph_formulas: MagicMock,
    refactor_internals_all_clusters: MagicMock | None = None,
) -> MagicMock:
    refactor = refactor_internals_all_clusters or MagicMock()
    with patch(
        "src.extraction_pipeline.build_pipeline_graph",
        return_value=MagicMock(
            graph=MagicMock(),
            series_bindings=MagicMock(),
            input_series=(),
            output_series=(),
            internal_series=(),
            graph_cache_key="cache-key",
        ),
    ):
        with patch(
            "src.extraction_pipeline.build_refactor_projection",
            return_value=MagicMock(),
        ):
            with patch(
                "src.extraction_pipeline.configure_docstring_callback",
                return_value="series_docs",
            ):
                with patch("src.extraction_pipeline.CodeGenerator") as generator_cls:
                    generator = generator_cls.return_value.__enter__.return_value
                    generator.generate_modules.return_value = {"internals.py": "pass\n"}
                    with patch("src.extraction_pipeline.seed_validation_harness"):
                        with patch(
                            "src.formula_clustering.cluster_graph_formulas",
                            cluster_graph_formulas,
                        ):
                            with patch(
                                "src.internals_refactor.refactor_internals_all_clusters",
                                refactor,
                            ):
                                with patch(
                                    "src.extraction_pipeline.run_post_refactor_differential"
                                ):
                                    with patch(
                                        "src.extraction_pipeline.export_reference_reports"
                                    ):
                                        export_generated_package(config)
    return refactor


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

    _export_generated_package_with_mocked_codegen(
        config,
        cluster_graph_formulas=MagicMock(return_value=()),
    )

    written = config.package_root / "internals.py"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "pass\n"
    assert written.resolve().is_relative_to(dist_root.resolve())
    if before is None:
        assert not repo_pollution.is_file()
    else:
        assert repo_pollution.read_bytes() == before


def test_export_generated_package_passes_variation_mode_to_cluster_graph_formulas(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    config = replace(
        synthetic_pipeline_config_fixture,
        dist_root=tmp_path / "dist",
        variation_mode="dominant_key_only",
    )
    cluster_graph_formulas = MagicMock(return_value=())
    _export_generated_package_with_mocked_codegen(
        config,
        cluster_graph_formulas=cluster_graph_formulas,
    )

    cluster_graph_formulas.assert_called_once()
    assert (
        cluster_graph_formulas.call_args.kwargs["variation_mode"] == "dominant_key_only"
    )
    assert cluster_graph_formulas.call_args.kwargs["clustering_mode"] == "series_ast"


def test_export_generated_package_passes_clustering_mode_to_cluster_graph_formulas(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    config = replace(
        synthetic_pipeline_config_fixture,
        dist_root=tmp_path / "dist",
        clustering_mode="ast",
    )
    cluster_graph_formulas = MagicMock(return_value=())
    _export_generated_package_with_mocked_codegen(
        config,
        cluster_graph_formulas=cluster_graph_formulas,
    )

    cluster_graph_formulas.assert_called_once()
    assert cluster_graph_formulas.call_args.kwargs["clustering_mode"] == "ast"


def test_export_generated_package_passes_bound_address_keys_to_refactor(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Avoid the second graph rebuild via ``_default_bound_address_keys``."""
    config = replace(
        synthetic_pipeline_config_fixture,
        dist_root=tmp_path / "dist",
    )
    expected_keys = {"Engine!B2": {"TIME_PERIOD": 1}}
    with patch(
        "src.refactor_bindings.build_bound_address_keys",
        return_value=expected_keys,
    ):
        refactor = _export_generated_package_with_mocked_codegen(
            config,
            cluster_graph_formulas=MagicMock(return_value=()),
        )

    refactor.assert_called_once()
    assert refactor.call_args.kwargs["bound_address_keys"] is expected_keys


def test_main_passes_cli_variation_mode_to_pipeline(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch("src.extraction_pipeline.run_pipeline") as pipeline:
                    main(["--variation-mode", "dominant_key_only"])

    pipeline.assert_called_once()
    assert pipeline.call_args.args[0].variation_mode == "dominant_key_only"


def test_main_passes_cli_clustering_mode_to_pipeline(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch("src.extraction_pipeline.run_pipeline") as pipeline:
                    main(["--clustering-mode", "ast"])

    pipeline.assert_called_once()
    assert pipeline.call_args.args[0].clustering_mode == "ast"
