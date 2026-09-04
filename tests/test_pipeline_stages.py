from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.extraction_pipeline import (
    PIPELINE_STAGES,
    AnnotateStageState,
    ExportStageState,
    main,
    run_export_stage,
    run_pipeline,
    run_validate_stage,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.stage_timings import PipelineTimings, stage_timings_path


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
        binding_authoring_prompt_path=repo_root
        / "templates"
        / "binding-authoring-prompt.txt",
        user_guide_agent_prompt_path=repo_root / "templates" / "user-guide-agent.txt",
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
        patch("src.extraction_pipeline.CodeGenerator") as generator_cls,
        patch("src.extraction_pipeline.materialize_package"),
        patch("src.package_materialize.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_export_stage(config, no_cache=True)

    generator.generate_modules.assert_called()
    assert generator.generate_modules.call_args.kwargs["blank_ranges"] == blank_ranges
    assert generator.generate_modules.call_args.kwargs["paradigm"] == "inverted_tree"


def test_run_export_stage_builds_code_generator_from_graph(
    tmp_path: Path,
) -> None:
    graph = MagicMock()
    config = replace(_sample_config(tmp_path), blank_ranges=())
    (tmp_path / "dist" / "my_model").mkdir(parents=True)
    config.guide_path.write_text("guide\n", encoding="utf-8")

    with (
        patch(
            "src.extraction_pipeline.build_pipeline_graph",
            return_value=MagicMock(
                graph=graph,
                series_bindings=MagicMock(),
                input_series=(),
                output_series=(),
                internal_series=(),
                constant_series=(),
                graph_cache_key="cache-key",
                leaf_classification={"Inputs!A1": "input"},
            ),
        ),
        patch("src.extraction_pipeline.CodeGenerator") as generator_cls,
        patch("src.extraction_pipeline.materialize_package"),
        patch("src.package_materialize.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_export_stage(config, no_cache=True)

    generator_cls.assert_called_once_with(graph)
    assert generator.generate_modules.call_args.kwargs["paradigm"] == "inverted_tree"


def test_pipeline_stages_order() -> None:
    assert PIPELINE_STAGES == (
        "extract",
        "export",
        "annotate",
        "validate",
        "document",
    )


def test_run_pipeline_stop_after_extract_skips_later_stages(
    synthetic_pipeline_config_fixture,
) -> None:
    with (
        patch("src.extraction_pipeline.extract_dependency_graph") as extract,
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch("src.extraction_pipeline.run_annotate_stage") as annotate,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="extract",
        )

    extract.assert_called_once()
    export.assert_not_called()
    annotate.assert_not_called()
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
        patch("src.extraction_pipeline.run_annotate_stage") as annotate,
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
    annotate.assert_not_called()
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
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
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


def _mock_annotate_state(config: PipelineConfig) -> AnnotateStageState:
    return AnnotateStageState(config=config, codegen_cache_key="c" * 64)


def test_run_pipeline_stop_after_annotate_skips_validate_and_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    graph = object()
    graph_cache_key = "g" * 64
    extract_result = MagicMock(graph=graph, graph_cache_key=graph_cache_key)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
        ) as annotate,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="annotate",
        )

    export.assert_called_once()
    assert export.call_args.kwargs["graph"] is graph
    assert export.call_args.kwargs["graph_cache_key"] is graph_cache_key
    annotate.assert_called_once_with(
        export_state,
        no_cache=False,
        force_rebuild=False,
        timings=ANY,
    )
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_stop_after_validate_skips_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
        ) as annotate,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="validate",
        )

    annotate.assert_called_once_with(
        export_state,
        no_cache=False,
        force_rebuild=False,
        timings=ANY,
    )
    validate.assert_called_once_with(annotate_state, no_cache=False, timings=ANY)
    document.assert_not_called()


def test_run_pipeline_default_runs_through_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=0,
        ) as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once_with(annotate_state, no_cache=False, timings=ANY)
    document.assert_called_once_with(
        synthetic_pipeline_config_fixture,
        no_cache=False,
        force_rebuild=False,
    )


def test_run_pipeline_passes_no_cache_to_validate_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
        ) as annotate,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline"),
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="validate",
            no_cache=True,
        )

    annotate.assert_called_once_with(
        export_state,
        no_cache=True,
        force_rebuild=False,
        timings=ANY,
    )
    validate.assert_called_once_with(annotate_state, no_cache=True, timings=ANY)


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
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=1,
        ) as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once_with(annotate_state, no_cache=False, timings=ANY)
    document.assert_not_called()


def test_run_pipeline_force_document_runs_docs_after_differential_failure(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
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

    document.assert_called_once_with(
        synthetic_pipeline_config_fixture,
        no_cache=False,
        force_rebuild=False,
    )


def test_run_pipeline_document_failure_raises_document_stage_error(
    synthetic_pipeline_config_fixture,
) -> None:
    from src.extraction_pipeline import DocumentStageError

    export_state = object()
    extract_result = MagicMock(graph=object(), graph_cache_key="g" * 64)
    annotate_state = _mock_annotate_state(synthetic_pipeline_config_fixture)
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=annotate_state,
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


def test_run_validate_stage_records_spans_and_profiles(tmp_path: Path) -> None:
    state = AnnotateStageState(
        config=_sample_config(tmp_path),
        codegen_cache_key="c" * 64,
    )
    timings = PipelineTimings()

    with (
        patch(
            "src.inverted_tree_validate.write_formula_evaluator_parity_reports",
            return_value=0,
        ) as write_reports,
        patch("src.extraction_pipeline.profile_if_enabled") as profile,
    ):
        exit_code = run_validate_stage(state, timings=timings)

    assert exit_code == 0
    assert profile.call_args.kwargs["basename"] == "validate"
    record = timings.stages[0]
    assert record.name == "validate"
    assert set(record.spans) == {"formula_evaluator_parity"}
    canary_dir = tmp_path / "dist" / "tests" / "results" / "reference"
    write_reports.assert_called_once()
    assert write_reports.call_args.kwargs["report_dir"] == canary_dir
    assert write_reports.call_args.args[0].differential_report_dir_rel == Path(
        "data/differential/exported_library"
    )
    assert canary_dir != tmp_path / "data" / "differential" / "exported_library"


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
        patch("src.extraction_pipeline.run_annotate_stage"),
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
        patch("src.extraction_pipeline.run_annotate_stage") as annotate,
        patch("src.extraction_pipeline.run_validate_stage", return_value=0) as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    timings = extract.call_args.kwargs["timings"]
    assert isinstance(timings, PipelineTimings)
    assert export.call_args.kwargs["timings"] is timings
    assert annotate.call_args.kwargs["timings"] is timings
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
        main(["--start-from-stage", "annotate"])

    assert pipeline.call_args.kwargs["start_from_stage"] == "annotate"
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
        main(["--start-from-stage", "annotate", "--only-stage", "validate"])


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


def test_run_pipeline_start_from_annotate_skips_export(
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
            "src.extraction_pipeline.run_annotate_stage",
            return_value=_mock_annotate_state(config),
        ) as annotate,
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(
            config,
            start_from_stage="annotate",
            stop_after_stage="annotate",
        )

    export.assert_not_called()
    materialize.assert_called_once_with(config, codegen_key="c" * 64)
    annotate.assert_called_once()
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
        stage="annotate",
        cache_keys={"codegen_cache_key": "c" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )

    with (
        patch("src.extraction_pipeline.run_export_stage") as export,
        patch("src.extraction_pipeline.run_annotate_stage") as annotate,
        patch("src.extraction_pipeline.materialize_package") as materialize,
        patch(
            "src.inverted_tree_docstrings.annotate_exported_package",
        ),
        patch(
            "src.extraction_pipeline.run_validate_stage",
            return_value=0,
        ) as validate,
        patch("src.extraction_pipeline.run_document_stage") as document,
    ):
        run_pipeline(config, only_stage="validate")

    export.assert_not_called()
    annotate.assert_not_called()
    materialize.assert_called_once_with(config, codegen_key="c" * 64)
    validate.assert_called_once()
    document.assert_not_called()


def test_run_pipeline_start_from_annotate_aborts_on_workbook_drift(
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
            start_from_stage="annotate",
            stop_after_stage="annotate",
        )
