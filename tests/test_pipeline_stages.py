from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.codegen_cache import DEFAULT_CODEGEN_CACHE_DIR, save_codegen_payload
from src.extraction_pipeline import (
    PIPELINE_STAGES,
    ExportStageState,
    RefactorLabOptions,
    RefactorStageState,
    main,
    run_export_stage,
    run_pipeline,
    run_refactor_stage,
    run_validate_stage,
)
from src.formula_clustering import FormulaCluster
from src.package_materialize import (
    PackageCacheKeys,
    current_internals_inputs,
    materialize_package,
    read_package_cache_keys,
    write_package_cache_keys,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.stage_timings import PipelineTimings, stage_timings_path

_COLD_CLONE_MODULES = {
    "__init__.py": "# init\n",
    "api.py": "from .runtime import EvalContext\n\ndef api():\n    return 1\n",
    "data.py": "DATA = {}\n",
    "runtime.py": "def run():\n    pass\n",
    "internals.py": "def cell_a1(ctx):\n    return 1.0\n",
}
_COLD_CLONE_REFACTORED = "def cell_a1(ctx):\n    return 42.0\n"


@pytest.fixture(autouse=True)
def _stage_timings_in_tmp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep pipeline runs from writing ``artifacts/`` in the working repo."""
    monkeypatch.setattr(
        "src.extraction_pipeline.stage_timings_path",
        lambda _repo_root: tmp_path / "stage-timings.json",
    )
    monkeypatch.setattr(
        "src.stage_manifest.stage_manifest_path",
        lambda _repo_root, stage: tmp_path / "stages" / f"{stage}.json",
    )


def _sample_config(repo_root: Path) -> PipelineConfig:
    workbook_path = repo_root / "data" / "workbook.xlsx"
    guide_path = repo_root / "data" / "guide.md"
    bindings_path = repo_root / "bindings"
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    if not workbook_path.is_file():
        workbook_path.write_bytes(b"workbook")
    if not guide_path.is_file():
        guide_path.write_text("guide\n", encoding="utf-8")
    bindings_path.mkdir(parents=True, exist_ok=True)
    binding_file = bindings_path / "inputs.bindings.yaml"
    if not binding_file.is_file():
        binding_file.write_text("series: []\n", encoding="utf-8")
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=workbook_path,
        guide_path=guide_path,
        bindings_path=bindings_path,
        dist_root=repo_root / "dist",
        targets=("Sheet!A1",),
        constraints={},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
        docstring_callback_name="series_docs",
        canonical_api_example_path=repo_root / "templates" / "canonical-api-usage.md",
        binding_authoring_prompt_path=repo_root
        / "templates"
        / "binding-authoring-prompt.txt",
        section_rewrite_introduction_focus_path=(
            repo_root / "templates" / "section-rewrite-introduction-focus.txt"
        ),
        section_rewrite_functional_overview_focus_path=(
            repo_root / "templates" / "section-rewrite-functional-overview-focus.txt"
        ),
        section_rewrite_illustrative_example_focus_path=(
            repo_root / "templates" / "section-rewrite-illustrative-example-focus.txt"
        ),
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=repo_root / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )


def test_run_export_stage_prints_codegen_stage_boundary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _sample_config(tmp_path)
    (tmp_path / "dist" / "my_model").mkdir(parents=True)
    config.guide_path.parent.mkdir(parents=True, exist_ok=True)
    config.guide_path.write_text("guide\n", encoding="utf-8")

    with (
        patch(
            "src.extraction_pipeline.build_pipeline_graph",
            return_value=MagicMock(
                graph=MagicMock(),
                series_bindings=MagicMock(),
                input_series=(),
                output_series=(),
                internal_series=(),
                constant_series=(),
                graph_cache_key="cache-key",
            ),
        ),
        patch(
            "src.extraction_pipeline.build_refactor_projection",
            return_value=MagicMock(),
        ),
        patch(
            "src.extraction_pipeline.configure_docstring_callback",
            return_value="series_docs",
        ),
        patch("src.extraction_pipeline.CodeGenerator") as generator_cls,
        patch("src.package_materialize.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_export_stage(config)

    captured = capsys.readouterr().out
    assert "codegen: cache miss" in captured or "codegen: cache bypassed" in captured
    assert "codegen: 1 modules (" in captured


def test_run_export_stage_forwards_blank_ranges_to_generate_modules(
    tmp_path: Path,
) -> None:
    blank_ranges = ("'Chart Data'!D46:X46",)
    config = replace(_sample_config(tmp_path), blank_ranges=blank_ranges)
    (tmp_path / "dist" / "my_model").mkdir(parents=True)
    config.guide_path.write_text("guide\n", encoding="utf-8")

    with (
        patch(
            "src.extraction_pipeline.build_pipeline_graph",
            return_value=MagicMock(
                graph=MagicMock(),
                series_bindings=MagicMock(),
                input_series=(),
                output_series=(),
                internal_series=(),
                constant_series=(),
                graph_cache_key="cache-key",
            ),
        ),
        patch(
            "src.extraction_pipeline.build_refactor_projection",
            return_value=MagicMock(),
        ),
        patch(
            "src.extraction_pipeline.configure_docstring_callback",
            return_value="series_docs",
        ),
        patch("src.extraction_pipeline.CodeGenerator") as generator_cls,
        patch("src.extraction_pipeline.materialize_package"),
        patch("src.package_materialize.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_export_stage(config, no_cache=True)

    generator.generate_modules.assert_called()
    assert generator.generate_modules.call_args.kwargs["blank_ranges"] == blank_ranges


def test_run_refactor_stage_prints_clustering_and_refactor_boundaries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _sample_config(tmp_path)
    package_root = tmp_path / "dist" / "my_model"
    package_root.mkdir(parents=True)
    (package_root / "internals.py").write_text("pass\n", encoding="utf-8")
    clusters = (
        FormulaCluster(
            cluster_id=0,
            members=("Sheet!A1", "Sheet!B1"),
            canonical_template="=1",
            row=1,
        ),
        FormulaCluster(
            cluster_id=1,
            members=("Sheet!C1",),
            canonical_template="=2",
            row=1,
        ),
    )
    state = ExportStageState(
        config=config,
        graph_cache_key="cache-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key="codegen-key",
        package_root=package_root,
    )
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import ExportStageArtifacts

    cluster_result = ClusterCacheResult(
        clusters=clusters,
        schedule=(),
        cache_key="cluster-key",
        cache_hit=False,
        elapsed_seconds=0.01,
    )
    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch("src.internals_refactor.refactor_internals_all_clusters"),
    ):
        run_refactor_stage(state)

    captured = capsys.readouterr().out
    assert "clustering: partitioning formulas..." in captured
    assert "clustering: 3 formulas -> 2 clusters" in captured
    assert "internals_refactor: rewriting 2 clusters..." in captured
    assert "internals_refactor: done (" in captured


def test_pipeline_stages_order() -> None:
    assert PIPELINE_STAGES == (
        "extract",
        "export",
        "refactor",
        "validate",
        "document",
    )


def test_run_pipeline_stop_after_extract_skips_later_stages(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch("src.extraction_pipeline.extract_dependency_graph") as extract,
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch("src.extraction_pipeline.run_refactor_stage") as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="extract",
        )

    extract.assert_called_once()
    export.assert_not_called()
    refactor.assert_not_called()
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_stop_after_export_runs_through_export(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    graph = object()
    graph_cache_key = "g" * 64
    extract_result = MagicMock(graph=graph, graph_cache_key=graph_cache_key)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ) as extract,
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ) as export,
        patch("src.extraction_pipeline.run_refactor_stage") as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="export",
            no_cache=True,
        )

    extract.assert_called_once_with(
        synthetic_pipeline_config_fixture,
        no_cache=True,
        force_rebuild=False,
        timings=ANY,
    )
    export.assert_called_once_with(
        synthetic_pipeline_config_fixture,
        no_cache=True,
        force_rebuild=False,
        timings=ANY,
        graph=graph,
        graph_cache_key=graph_cache_key,
    )
    refactor.assert_not_called()
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_full_run_records_extract_then_export(
    synthetic_pipeline_config_fixture,
) -> None:
    """A full run includes extract before export and hands off the live graph."""
    export_state = object()
    graph = object()
    graph_cache_key = "g" * 64
    extract_result = MagicMock(graph=graph, graph_cache_key=graph_cache_key)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ) as extract,
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ) as export,
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch("src.extraction_pipeline.run_validate_stage", return_value=0),
        patch("src.extraction_pipeline.run_document_stage"),
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    assert extract.call_count == 1
    assert export.call_count == 1
    assert extract.call_args.kwargs["timings"] is export.call_args.kwargs["timings"]
    assert export.call_args.kwargs["graph"] is graph
    assert export.call_args.kwargs["graph_cache_key"] is graph_cache_key


def _mock_refactor_state(config: PipelineConfig) -> RefactorStageState:
    return RefactorStageState(
        config=config,
        codegen_cache_key="c" * 64,
        internals_cache_key="i" * 64,
    )


def test_run_pipeline_stop_after_refactor_skips_validate_and_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    graph = object()
    graph_cache_key = "g" * 64
    extract_result = MagicMock(graph=graph, graph_cache_key=graph_cache_key)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ) as export,
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ) as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="refactor",
        )

    export.assert_called_once()
    assert export.call_args.kwargs["graph"] is graph
    assert export.call_args.kwargs["graph_cache_key"] is graph_cache_key
    refactor.assert_called_once_with(
        export_state,
        no_cache=False,
        force_rebuild=False,
        timings=ANY,
        lab_options=None,
    )
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_stop_after_validate_skips_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ) as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="validate",
        )

    refactor.assert_called_once_with(
        export_state,
        no_cache=False,
        force_rebuild=False,
        timings=ANY,
        lab_options=None,
    )
    validate.assert_called_once_with(refactor_state, no_cache=False, timings=ANY)
    document.assert_not_called()


def test_run_pipeline_default_runs_through_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=0,
        ) as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once_with(refactor_state, no_cache=False, timings=ANY)
    document.assert_called_once_with(synthetic_pipeline_config_fixture)


def test_run_pipeline_passes_no_cache_to_validate_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ) as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline"),
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="validate",
            no_cache=True,
        )

    refactor.assert_called_once_with(
        export_state,
        no_cache=True,
        force_rebuild=False,
        timings=ANY,
        lab_options=None,
    )
    validate.assert_called_once_with(refactor_state, no_cache=True, timings=ANY)


def test_run_pipeline_rejects_unknown_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with pytest.raises(ValueError, match="unknown pipeline stage"):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="not-a-stage",
        )


def test_main_stop_after_stage_extract_uses_extract_path(
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
        main(["--stop-after-stage", "extract"])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "extract"


def test_main_extract_graph_alias_stops_after_extract(
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
        main(["--extract-graph"])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "extract"


def test_main_rejects_extract_graph_with_stop_after_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch(
            "src.extraction_pipeline.load_pipeline_config",
            return_value=synthetic_pipeline_config_fixture,
        ),
        pytest.raises(SystemExit),
    ):
        main(["--extract-graph", "--stop-after-stage", "export"])


def test_run_pipeline_skips_document_when_differential_failed(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=1,
        ) as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once_with(refactor_state, no_cache=False, timings=ANY)
    document.assert_not_called()


def test_run_pipeline_force_document_runs_docs_after_differential_failure(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=1,
        ),
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            force_document=True,
        )

    document.assert_called_once_with(synthetic_pipeline_config_fixture)


def test_run_pipeline_document_failure_raises_document_stage_error(
    synthetic_pipeline_config_fixture,
) -> None:
    from src.extraction_pipeline import DocumentStageError

    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    refactor_state = _mock_refactor_state(synthetic_pipeline_config_fixture)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=extract_result,
        ),
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=0,
        ) as validate,
        patch(
            "src.documentation_pipeline.run_documentation_pipeline",
            side_effect=RuntimeError("guide rewrite hung"),
        ),
        pytest.raises(DocumentStageError, match="document stage failed"),
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once()


def test_run_refactor_stage_records_spans_and_profiles(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    package_root = tmp_path / "dist" / "my_model"
    package_root.mkdir(parents=True)
    (package_root / "internals.py").write_text("pass\n", encoding="utf-8")
    state = ExportStageState(
        config=config,
        graph_cache_key="cache-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key="codegen-key",
        package_root=package_root,
    )
    timings = PipelineTimings()
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import ExportStageArtifacts

    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key="cluster-key",
        cache_hit=False,
        elapsed_seconds=0.01,
    )
    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch("src.internals_refactor.refactor_internals_all_clusters"),
        patch("src.extraction_pipeline.profile_if_enabled") as profile,
    ):
        run_refactor_stage(state, timings=timings)

    assert profile.call_args.kwargs["basename"] == "refactor"
    record = timings.stages[0]
    assert record.name == "refactor"
    assert "build_refactor_bindings" in record.spans
    assert "cluster_graph_formulas" in record.spans
    # Rollup would overlap nested pass1_* spans recorded inside the refactor.
    assert "internals_refactor" not in record.spans


def test_run_refactor_stage_forwards_the_stage_timer_to_the_refactor(
    tmp_path: Path,
) -> None:
    config = _sample_config(tmp_path)
    package_root = tmp_path / "dist" / "my_model"
    package_root.mkdir(parents=True)
    state = ExportStageState(
        config=config,
        graph_cache_key="cache-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key="codegen-key",
        package_root=package_root,
    )
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import ExportStageArtifacts

    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key="cluster-key",
        cache_hit=False,
        elapsed_seconds=0.01,
    )
    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch("src.internals_refactor.refactor_internals_all_clusters") as refactor,
    ):
        run_refactor_stage(state, timings=PipelineTimings())

    assert refactor.call_args.kwargs["timer"] is not None
    assert refactor.call_args.kwargs["refactor_schedule"] == ()
    assert refactor.call_args.kwargs["codegen_cache_key"] == "codegen-key"


def test_run_validate_stage_records_spans_and_profiles(tmp_path: Path) -> None:
    state = RefactorStageState(
        config=_sample_config(tmp_path),
        codegen_cache_key="c" * 64,
        internals_cache_key="i" * 64,
    )
    timings = PipelineTimings()

    with (
        patch(
            "src.extraction_pipeline.run_post_refactor_differential",
            return_value=0,
        ),
        patch("src.extraction_pipeline.export_reference_reports"),
        patch("src.extraction_pipeline.profile_if_enabled") as profile,
    ):
        exit_code = run_validate_stage(state, timings=timings)

    assert exit_code == 0
    assert profile.call_args.kwargs["basename"] == "validate"
    record = timings.stages[0]
    assert record.name == "validate"
    assert set(record.spans) == {
        "post_refactor_differential",
        "export_reference_reports",
    }


def test_run_pipeline_writes_stage_timings_artifact(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    config = replace(synthetic_pipeline_config_fixture, repo_root=tmp_path)
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=MagicMock(graph=object(), graph_cache_key="g" * 64),
        ),
        patch("src.extraction_pipeline.run_export_stage"),
        patch("src.extraction_pipeline.run_refactor_stage"),
        patch("src.extraction_pipeline.run_validate_stage", return_value=0),
        patch("src.extraction_pipeline.run_document_stage"),
        patch(
            "src.extraction_pipeline.stage_timings_path",
            side_effect=stage_timings_path,
        ),
    ):
        run_pipeline(config)

    payload = json.loads(
        (tmp_path / "artifacts" / "stage-timings.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"]
    assert set(payload["caches"]) == {
        "dependency-graph",
        "bindings-validation",
        "series-resolution",
        "series-derived",
        "projection",
        "codegen",
        "clusters",
        "internals",
    }


def test_run_pipeline_threads_one_timings_object_through_every_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch(
            "src.extraction_pipeline.extract_dependency_graph",
            return_value=MagicMock(graph=object(), graph_cache_key="g" * 64),
        ) as extract,
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch("src.extraction_pipeline.run_refactor_stage") as refactor,
        patch("src.extraction_pipeline.run_validate_stage", return_value=0) as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    timings = extract.call_args.kwargs["timings"]
    assert isinstance(timings, PipelineTimings)
    assert export.call_args.kwargs["timings"] is timings
    assert refactor.call_args.kwargs["timings"] is timings
    assert validate.call_args.kwargs["timings"] is timings
    assert document.call_args.kwargs["timings"] is timings


def test_run_document_stage_is_profiled(
    synthetic_pipeline_config_fixture,
) -> None:
    from src.extraction_pipeline import run_document_stage

    timings = PipelineTimings()
    with (
        patch("src.documentation_pipeline.run_documentation_pipeline"),
        patch("src.extraction_pipeline.profile_if_enabled") as profile,
    ):
        run_document_stage(synthetic_pipeline_config_fixture, timings=timings)

    assert profile.call_args.kwargs["basename"] == "document"
    assert timings.stages[0].name == "document"


def test_main_force_document_flag_is_passed(
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
        main(["--force-document"])

    assert pipeline.call_args.kwargs["force_document"] is True


def test_main_force_rebuild_flag_is_passed(
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
        main(["--force-rebuild"])

    assert pipeline.call_args.kwargs["force_rebuild"] is True


def test_main_start_from_stage_is_passed(
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
        main(["--start-from-stage", "refactor"])

    assert pipeline.call_args.kwargs["start_from_stage"] == "refactor"
    assert pipeline.call_args.kwargs["stop_after_stage"] == "document"
    assert pipeline.call_args.kwargs["only_stage"] is None


def test_main_only_stage_is_passed(
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
        main(["--only-stage", "validate"])

    assert pipeline.call_args.kwargs["only_stage"] == "validate"
    assert pipeline.call_args.kwargs["start_from_stage"] == "validate"
    assert pipeline.call_args.kwargs["stop_after_stage"] == "validate"


def test_main_rejects_start_from_with_only_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch(
            "src.extraction_pipeline.load_pipeline_config",
            return_value=synthetic_pipeline_config_fixture,
        ),
        pytest.raises(SystemExit),
    ):
        main(["--start-from-stage", "refactor", "--only-stage", "validate"])


def test_main_rejects_only_stage_with_stop_after_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch(
            "src.extraction_pipeline.load_pipeline_config",
            return_value=synthetic_pipeline_config_fixture,
        ),
        pytest.raises(SystemExit),
    ):
        main(["--only-stage", "validate", "--stop-after-stage", "document"])


def _isolated_config_for_manifests(
    base: PipelineConfig,
    tmp_path: Path,
) -> PipelineConfig:
    """Copy fingerprint inputs under tmp_path so tests never touch repo fixtures."""
    workbook = tmp_path / "data" / "workbook.xlsx"
    guide = tmp_path / "data" / "guide.md"
    bindings = tmp_path / "bindings"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"wb")
    guide.write_text("guide\n", encoding="utf-8")
    bindings.mkdir(parents=True, exist_ok=True)
    (bindings / "inputs.bindings.yaml").write_text("series: []\n", encoding="utf-8")
    (bindings / "outputs.bindings.yaml").write_text("series: []\n", encoding="utf-8")
    (bindings / "internals.bindings.yaml").write_text("series: []\n", encoding="utf-8")
    return replace(
        base,
        repo_root=tmp_path,
        workbook_path=workbook,
        guide_path=guide,
        bindings_path=bindings,
        dist_root=tmp_path / "dist",
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
    )


def _seed_differential_harness(config: PipelineConfig) -> None:
    """Create the minimal repo-side harness files ``materialize_package`` copies."""
    (config.repo_root / "tests" / "differential").mkdir(parents=True, exist_ok=True)
    (config.repo_root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (config.repo_root / "tests" / "differential" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    for name in (
        "differential_types.py",
        "differential_excel.py",
        "comparison_utils.py",
        "differential_test_exported_library.py",
    ):
        (config.repo_root / "tests" / "differential" / name).write_text(
            f"# {name}\n", encoding="utf-8"
        )


def _write_export_manifest_for_codegen(
    config: PipelineConfig,
    *,
    codegen_key: str,
) -> None:
    from src.stage_manifest import (
        compute_input_fingerprints,
        write_stage_manifest,
    )

    write_stage_manifest(
        config,
        stage="export",
        cache_keys={
            "graph_cache_key": "g" * 64,
            "projection_cache_key": "p" * 64,
            "series_derived_cache_key": "s" * 64,
            "codegen_cache_key": codegen_key,
        },
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )


def _commit_refactored_dist(
    config: PipelineConfig,
    *,
    codegen_key: str,
    internals_key: str,
    keep_codegen_cache: bool,
) -> None:
    """Simulate a fresh clone: committed ``dist/`` with sidecar keys, cold caches."""
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    _seed_differential_harness(config)
    save_codegen_payload(
        _COLD_CLONE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="p" * 64,
    )
    materialize_package(config, codegen_key=codegen_key)
    (config.package_root / "internals.py").write_text(
        _COLD_CLONE_REFACTORED, encoding="utf-8"
    )
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=current_internals_inputs(),
        ),
    )
    if not keep_codegen_cache:
        for suffix in (".pkl.gz", ".meta.json"):
            path = DEFAULT_CODEGEN_CACHE_DIR / f"{codegen_key}{suffix}"
            path.unlink(missing_ok=True)
    internals_cache_path = DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py"
    internals_cache_path.unlink(missing_ok=True)
    (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.meta.json").unlink(missing_ok=True)
    assert not internals_cache_path.is_file()
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=internals_key,
        internals_inputs=current_internals_inputs(),
    )


def test_run_pipeline_start_from_refactor_skips_export(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    from src.stage_manifest import (
        compute_input_fingerprints,
        write_stage_manifest,
    )

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)

    write_stage_manifest(
        config,
        stage="export",
        cache_keys={
            "graph_cache_key": "g" * 64,
            "projection_cache_key": "p" * 64,
            "series_derived_cache_key": "s" * 64,
            "codegen_cache_key": "c" * 64,
        },
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    export_state = ExportStageState(
        config=config,
        graph_cache_key="g" * 64,
        projection_cache_key="p" * 64,
        series_derived_cache_key="s" * 64,
        codegen_cache_key="c" * 64,
        package_root=config.package_root,
    )

    with (
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch(
            "src.extraction_pipeline.export_stage_state_from_manifest",
            return_value=export_state,
        ),
        patch("src.extraction_pipeline.materialize_package") as materialize,
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=RefactorStageState(
                config=config,
                codegen_cache_key="c" * 64,
                internals_cache_key="i" * 64,
            ),
        ) as refactor,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(
            config,
            start_from_stage="refactor",
            stop_after_stage="refactor",
        )

    export.assert_not_called()
    materialize.assert_called_once_with(config, codegen_key="c" * 64)
    refactor.assert_called_once()
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_only_stage_validate_materializes_from_manifest(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    from src.stage_manifest import (
        compute_input_fingerprints,
        write_stage_manifest,
    )

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)

    write_stage_manifest(
        config,
        stage="refactor",
        cache_keys={
            "codegen_cache_key": "c" * 64,
            "internals_cache_key": "i" * 64,
            "clusters_cache_key": "k" * 64,
        },
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )

    with (
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch("src.extraction_pipeline.run_refactor_stage") as refactor,
        patch("src.extraction_pipeline.materialize_package") as materialize,
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=0,
        ) as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(config, only_stage="validate")

    export.assert_not_called()
    refactor.assert_not_called()
    materialize.assert_called_once_with(
        config,
        codegen_key="c" * 64,
        internals_key="i" * 64,
    )
    validate.assert_called_once()
    document.assert_not_called()


def test_run_pipeline_start_from_refactor_aborts_on_workbook_drift(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    from src.stage_manifest import (
        StageManifestDriftError,
        compute_input_fingerprints,
        write_stage_manifest,
    )

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    config.workbook_path.write_bytes(b"original")

    write_stage_manifest(
        config,
        stage="export",
        cache_keys={"codegen_cache_key": "c" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    config.workbook_path.write_bytes(b"drifted")

    with pytest.raises(StageManifestDriftError, match="workbook"):
        run_pipeline(
            config,
            start_from_stage="refactor",
            stop_after_stage="refactor",
        )


def _refactor_entry_probe(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
    lab_options: RefactorLabOptions | None = None,
) -> dict[str, Any]:
    """Run ``start_from_stage=refactor`` and capture what refactor was handed.

    Returns the ``internals.py`` text as it existed when
    ``refactor_internals_all_clusters`` was invoked, plus the mock handles, so a
    caller can assert on both the cache decision and the module Pass 1 started
    from.
    """
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import ExportStageArtifacts

    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key="k" * 64,
        cache_hit=False,
        elapsed_seconds=0.0,
    )
    export_artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )
    seen: dict[str, Any] = {}

    def _capture(*_args, **kwargs):
        seen["internals_at_refactor"] = kwargs["internals_path"].read_text(
            encoding="utf-8"
        )
        return MagicMock(cacheable=False, final_source=None)

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            side_effect=_capture,
        ) as refactor,
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=export_artifacts,
        ),
    ):
        run_pipeline(
            config,
            start_from_stage="refactor",
            stop_after_stage="refactor",
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            lab_options=lab_options,
        )
    seen["refactor"] = refactor
    return seen


@pytest.mark.parametrize("flag", ["force_rebuild", "no_cache"])
def test_run_pipeline_start_from_refactor_rebuilds_from_pristine_internals(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
    flag: str,
) -> None:
    """A rebuild must not start Pass 1 from an already-refactored module.

    Mid-pipeline entry materializes ``dist/`` before the refactor stage runs.
    When the run is going to rebuild, that materialization has to be the pristine
    codegen module — adopting the cached refactored ``internals.py`` would make
    Pass 1 rewrite an already-rewritten module while the parity oracle still
    compares against pristine.
    """
    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "c" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key="d" * 64,
        keep_codegen_cache=True,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)

    seen = _refactor_entry_probe(
        config,
        force_rebuild=flag == "force_rebuild",
        no_cache=flag == "no_cache",
    )

    seen["refactor"].assert_called_once()
    assert seen["internals_at_refactor"] != _COLD_CLONE_REFACTORED
    assert seen["internals_at_refactor"] == _COLD_CLONE_MODULES["internals.py"]


def test_run_pipeline_start_from_refactor_refuses_adopt_on_provenance_drift(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """A committed dist built under a different refactor recipe must not be adopted.

    Adoption happens before clustering, so the full content key cannot be
    recomputed there. The sidecar's recorded provenance is the only available
    signal that the refactor model / schema versions / ``excel-grapher`` version
    drifted, and none of those are covered by manifest fingerprints.
    """
    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "c" * 64
    internals_key = "d" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
        keep_codegen_cache=True,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)

    drifted = dict(current_internals_inputs())
    drifted["refactor_model"] = "some-other-model"
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=drifted,
        ),
    )

    seen = _refactor_entry_probe(config)

    seen["refactor"].assert_called_once()
    assert seen["internals_at_refactor"] == _COLD_CLONE_MODULES["internals.py"]


def test_run_pipeline_start_from_refactor_refuses_adopt_without_provenance(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """A sidecar with an internals_key but no provenance is unverifiable, so refuse."""
    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "c" * 64
    internals_key = "d" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
        keep_codegen_cache=True,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(codegen_key=codegen_key, internals_key=internals_key),
    )

    seen = _refactor_entry_probe(config)

    seen["refactor"].assert_called_once()
    assert seen["internals_at_refactor"] == _COLD_CLONE_MODULES["internals.py"]


@pytest.mark.parametrize(
    "lab_options",
    [
        pytest.param(RefactorLabOptions(dry_run=True), id="dry_run"),
        pytest.param(RefactorLabOptions(parity_gate=False), id="no_parity_gate"),
        pytest.param(
            RefactorLabOptions(prompt_observer=lambda *_: None), id="prompt_observer"
        ),
        pytest.param(
            RefactorLabOptions(cluster_context_observer=lambda *_: None),
            id="cluster_observer",
        ),
        pytest.param(
            RefactorLabOptions(singleton_context_observer=lambda *_: None),
            id="singleton_observer",
        ),
    ],
)
def test_run_pipeline_lab_options_bypass_warm_cache_short_circuits(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
    lab_options: RefactorLabOptions,
) -> None:
    """Lab runs must reach the refactor: every short-circuit returns before it.

    ``scripts/run_refactor_stage.py`` exists to observe prompts, report synthesis
    coverage, and iterate with the gate off. Answering those runs from the dist
    adopt or the internals cache would make every one of them a silent no-op.
    """
    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "c" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key="d" * 64,
        keep_codegen_cache=True,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)

    seen = _refactor_entry_probe(config, lab_options=lab_options)

    seen["refactor"].assert_called_once()
    assert seen["internals_at_refactor"] == _COLD_CLONE_MODULES["internals.py"]


def test_refactor_lab_options_requires_refactor_run_is_false_by_default() -> None:
    """A production run carries no lab options and must stay fully cacheable."""
    assert RefactorLabOptions().requires_refactor_run() is False


def test_run_pipeline_start_from_refactor_adopts_committed_dist_when_internals_cold(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Production entry must adopt committed dist without wiping the sidecar first.

    The Phase 3a cold-clone path trusts ``dist/.pipeline-cache-keys.json`` when
    ``.cache/internals/`` is empty. ``run_pipeline(start_from_stage=refactor)``
    must not rematerialize pristine codegen (clearing ``internals_key``) before
    ``try_materialize_refactored_package_from_cache`` runs — that wipe defeats
    adoption and forces a full clustering / Pass-1 rebuild.
    """
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import ExportStageArtifacts
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "c" * 64
    internals_key = "d" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
        keep_codegen_cache=True,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)

    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key="k" * 64,
        cache_hit=False,
        elapsed_seconds=0.0,
    )
    export_artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ) as cluster_build,
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            return_value=MagicMock(cacheable=False, final_source=None),
        ) as refactor,
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=export_artifacts,
        ) as load_artifacts,
    ):
        run_pipeline(
            config,
            start_from_stage="refactor",
            stop_after_stage="refactor",
        )

    load_artifacts.assert_not_called()
    cluster_build.assert_not_called()
    refactor.assert_not_called()
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == _COLD_CLONE_REFACTORED
    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py").is_file()
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=internals_key,
        internals_inputs=current_internals_inputs(),
    )


def test_run_pipeline_start_from_refactor_adopts_when_codegen_and_internals_cold(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Fresh clone: empty ``.cache/``, committed ``dist/`` with matching sidecar keys.

    Entry must not call ``materialize_package`` against a cold codegen cache
    (``FileNotFoundError``) before adoption. ``try_materialize_refactored_package_from_cache``
    already rebuilds from package modules when codegen is missing; production
    entry must reach that path.
    """
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "e" * 64
    internals_key = "f" * 64
    _commit_refactored_dist(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
        keep_codegen_cache=False,
    )
    _write_export_manifest_for_codegen(config, codegen_key=codegen_key)

    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch("src.cluster_cache.get_or_build_clusters_and_schedule") as cluster_build,
        patch("src.internals_refactor.refactor_internals_all_clusters") as refactor,
        patch("src.extraction_pipeline.load_export_stage_artifacts") as load_artifacts,
    ):
        run_pipeline(
            config,
            start_from_stage="refactor",
            stop_after_stage="refactor",
        )

    load_artifacts.assert_not_called()
    cluster_build.assert_not_called()
    refactor.assert_not_called()
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == _COLD_CLONE_REFACTORED
    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py").is_file()
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=internals_key,
        internals_inputs=current_internals_inputs(),
    )


def test_run_pipeline_only_stage_validate_preserves_lab_internals_without_cache_key(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Non-cacheable refactor leaves no ``internals_cache_key`` in the manifest.

    Lab / ``--no-parity-gate`` / ungated full-body runs still write a refactored
    ``dist/.../internals.py``. ``--only-stage validate`` must not rematerialize
    pristine codegen over that output when the refactor manifest omits
    ``internals_cache_key`` (``materialize_package(..., internals_key=None)``).
    """
    from src.stage_manifest import (
        compute_input_fingerprints,
        write_stage_manifest,
    )

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "a" * 64
    _seed_differential_harness(config)
    save_codegen_payload(
        _COLD_CLONE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="p" * 64,
    )
    materialize_package(config, codegen_key=codegen_key)
    # Simulate a completed non-cacheable lab/refactor write into dist/.
    (config.package_root / "internals.py").write_text(
        _COLD_CLONE_REFACTORED, encoding="utf-8"
    )
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(codegen_key=codegen_key, internals_key=None),
    )
    write_stage_manifest(
        config,
        stage="refactor",
        cache_keys={
            "codegen_cache_key": codegen_key,
            # Intentionally omit internals_cache_key (non-cacheable run).
        },
        upstream_keys={"codegen_cache_key": codegen_key},
        fingerprints=compute_input_fingerprints(config),
    )

    with patch(
        "src.extraction_pipeline.run_validate_stage",
        return_value=0,
    ) as validate:
        run_pipeline(config, only_stage="validate")

    validate.assert_called_once()
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == _COLD_CLONE_REFACTORED
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) != _COLD_CLONE_MODULES["internals.py"]


def test_run_pipeline_start_from_document_preserves_lab_internals_without_cache_key(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Same overwrite hazard on document entry, which rematerializes from validate."""
    from src.stage_manifest import (
        compute_input_fingerprints,
        write_stage_manifest,
    )

    config = _isolated_config_for_manifests(synthetic_pipeline_config_fixture, tmp_path)
    codegen_key = "b" * 64
    _seed_differential_harness(config)
    save_codegen_payload(
        _COLD_CLONE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="p" * 64,
    )
    materialize_package(config, codegen_key=codegen_key)
    (config.package_root / "internals.py").write_text(
        _COLD_CLONE_REFACTORED, encoding="utf-8"
    )
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(codegen_key=codegen_key, internals_key=None),
    )
    write_stage_manifest(
        config,
        stage="validate",
        cache_keys={
            "codegen_cache_key": codegen_key,
        },
        upstream_keys={"codegen_cache_key": codegen_key},
        fingerprints=compute_input_fingerprints(config),
    )

    with patch("src.extraction_pipeline.run_document_stage") as document:
        run_pipeline(config, only_stage="document")

    document.assert_called_once()
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == _COLD_CLONE_REFACTORED
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) != _COLD_CLONE_MODULES["internals.py"]
