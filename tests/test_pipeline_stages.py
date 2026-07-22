from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.extraction_pipeline import (
    PIPELINE_STAGES,
    ExportStageState,
    main,
    run_export_stage,
    run_pipeline,
    run_refactor_stage,
)
from src.formula_clustering import FormulaCluster
from src.pipeline_config import DistProjectMetadata, PipelineConfig


def _sample_config(repo_root: Path) -> PipelineConfig:
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=repo_root / "data" / "workbook.xlsx",
        guide_path=repo_root / "data" / "guide.md",
        bindings_path=repo_root / "bindings",
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
        projection_layout=None,
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
        patch("src.extraction_pipeline.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_export_stage(config)

    captured = capsys.readouterr().out
    assert "codegen: cache miss" in captured or "codegen: cache bypassed" in captured
    assert "codegen: 1 modules (" in captured


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
        graph_result=MagicMock(
            graph=MagicMock(),
            input_series=(),
            output_series=(),
            internal_series=(),
        ),
        refactor_projection=MagicMock(),
        internal_binding_index=MagicMock(),
        package_root=package_root,
    )

    with (
        patch(
            "src.refactor_bindings.build_bound_address_keys",
            return_value={},
        ),
        patch(
            "src.refactor_bindings.build_address_to_series_id",
            return_value={},
        ),
        patch(
            "src.formula_clustering.cluster_graph_formulas",
            return_value=clusters,
        ),
        patch("src.internals_refactor.refactor_internals_all_clusters"),
    ):
        run_refactor_stage(state)

    captured = capsys.readouterr().out
    assert "clustering: partitioning formulas…" in captured
    assert "clustering: 3 formulas → 2 clusters" in captured
    assert "internals_refactor: rewriting 2 clusters…" in captured
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
    with (
        patch("src.extraction_pipeline.extract_dependency_graph") as extract,
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

    extract.assert_not_called()
    export.assert_called_once_with(
        synthetic_pipeline_config_fixture,
        no_cache=True,
        force_rebuild=False,
    )
    refactor.assert_not_called()
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_stop_after_refactor_skips_validate_and_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    refactor_state = object()
    with (
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
    refactor.assert_called_once_with(export_state)
    validate.assert_not_called()
    document.assert_not_called()


def test_run_pipeline_stop_after_validate_skips_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    refactor_state = object()
    with (
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

    refactor.assert_called_once_with(export_state)
    validate.assert_called_once_with(refactor_state, no_cache=False)
    document.assert_not_called()


def test_run_pipeline_default_runs_through_document(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    refactor_state = object()
    with (
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline") as document,
    ):
        run_pipeline(synthetic_pipeline_config_fixture)

    validate.assert_called_once_with(refactor_state, no_cache=False)
    document.assert_called_once_with(synthetic_pipeline_config_fixture)


def test_run_pipeline_passes_no_cache_to_validate_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    export_state = object()
    refactor_state = object()
    with (
        patch(
            "src.extraction_pipeline.run_export_stage",
            return_value=export_state,
        ),
        patch(
            "src.extraction_pipeline.run_refactor_stage",
            return_value=refactor_state,
        ),
        patch("src.extraction_pipeline.run_validate_stage") as validate,
        patch("src.documentation_pipeline.run_documentation_pipeline"),
    ):
        run_pipeline(
            synthetic_pipeline_config_fixture,
            stop_after_stage="validate",
            no_cache=True,
        )

    validate.assert_called_once_with(refactor_state, no_cache=True)


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
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch("src.extraction_pipeline.run_pipeline") as pipeline:
                    main(["--stop-after-stage", "extract"])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "extract"


def test_main_extract_graph_alias_stops_after_extract(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.extraction_pipeline.validate_pipeline_config"):
            with patch("src.extraction_pipeline.activate_pipeline_config"):
                with patch("src.extraction_pipeline.run_pipeline") as pipeline:
                    main(["--extract-graph"])

    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["stop_after_stage"] == "extract"


def test_main_rejects_extract_graph_with_stop_after_stage(
    synthetic_pipeline_config_fixture,
) -> None:
    with patch(
        "src.extraction_pipeline.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with pytest.raises(SystemExit):
            main(["--extract-graph", "--stop-after-stage", "export"])
