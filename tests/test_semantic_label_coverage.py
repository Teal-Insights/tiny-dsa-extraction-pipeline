"""Tests for semantic label coverage validation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.extraction_pipeline import build_pipeline_graph
from src.semantic_labeling import (
    SemanticLabelCoverageError,
    cell_meets_semantic_label_requirements,
    enforce_semantic_label_coverage,
    find_unlabeled_cells,
    required_label_cells,
)
from tests.conftest import SyntheticConfiguredPipeline
from tests.fixtures.synthetic_pipeline import stub_semantic_labeling


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, False),
        ({}, False),
        ({"table_labels": [{"label": "Engine"}]}, False),
        (
            {
                "table_labels": [{"label": "Engine"}],
                "row_labels": [{"label": "Debt ratio"}],
            },
            True,
        ),
        (
            {
                "table_labels": [{"label": "Engine"}],
                "column_labels": [{"label": "Year 1"}],
            },
            True,
        ),
        (
            {
                "table_labels": [{"label": "Engine"}],
                "row_labels": [{"label": "Debt ratio"}],
                "column_labels": [{"label": "Year 1"}],
            },
            True,
        ),
    ],
)
def test_cell_meets_semantic_label_requirements(
    metadata: dict[str, object] | None,
    expected: bool,
) -> None:
    assert cell_meets_semantic_label_requirements(metadata) is expected


def test_required_label_cells_excludes_inputs_and_outputs(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    required = required_label_cells(
        pipeline.graph,
        input_cells=series_cell_keys(pipeline.input_series),
        target_cells=series_cell_keys(pipeline.output_series),
    )
    assert "Inputs!A1" not in required
    assert "Outputs!B1" not in required
    assert "Outputs!C1" not in required
    assert "Engine!B2" in required
    assert "Engine!C2" in required
    assert "Inputs!B1" in required


def test_find_unlabeled_cells_respects_exempt_list(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    input_cells = series_cell_keys(pipeline.input_series)
    output_cells = series_cell_keys(pipeline.output_series)
    unlabeled = find_unlabeled_cells(
        pipeline.graph,
        input_cells=input_cells,
        target_cells=output_cells,
        exempt_cells=frozenset({"Engine!B2", "Engine!C2", "Inputs!B1"}),
    )
    assert unlabeled == ()


def test_enforce_semantic_label_coverage_off_is_noop(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    report = enforce_semantic_label_coverage(
        graph=pipeline.graph,
        input_cells=series_cell_keys(pipeline.input_series),
        target_cells=series_cell_keys(pipeline.output_series),
        exempt_cells=frozenset(),
        mode="off",
        context="pytest",
    )
    assert report is None


def test_enforce_semantic_label_coverage_pytest_errors_when_unlabeled(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with pytest.raises(SemanticLabelCoverageError, match="Engine!B2"):
        enforce_semantic_label_coverage(
            graph=pipeline.graph,
            input_cells=series_cell_keys(pipeline.input_series),
            target_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="warn",
            context="pytest",
        )


def test_enforce_semantic_label_coverage_pipeline_warns_without_raising(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with caplog.at_level("WARNING"):
        report = enforce_semantic_label_coverage(
            graph=pipeline.graph,
            input_cells=series_cell_keys(pipeline.input_series),
            target_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="warn",
            context="pipeline",
        )
    assert report is not None
    assert report.unlabeled_cells
    assert "Engine!B2" in caplog.text


def test_enforce_semantic_label_coverage_pipeline_error_raises(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with pytest.raises(SemanticLabelCoverageError, match="Engine!B2"):
        enforce_semantic_label_coverage(
            graph=pipeline.graph,
            input_cells=series_cell_keys(pipeline.input_series),
            target_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="error",
            context="pipeline",
        )


def test_enforce_semantic_label_coverage_passes_when_labeled(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    labeled_metadata = {
        "table_labels": [{"label": "Engine"}],
        "row_labels": [{"label": "Formula row"}],
    }
    for address in ("Engine!B2", "Engine!C2", "Inputs!B1"):
        node = pipeline.graph.get_node(address)
        assert node is not None
        pipeline.graph.set_node_metadata(address, labeled_metadata)

    report = enforce_semantic_label_coverage(
        graph=pipeline.graph,
        input_cells=series_cell_keys(pipeline.input_series),
        target_cells=series_cell_keys(pipeline.output_series),
        exempt_cells=frozenset(),
        mode="warn",
        context="pytest",
    )
    assert report is not None
    assert report.unlabeled_cells == ()


def test_enforce_semantic_label_coverage_passes_with_exempt_cells(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    report = enforce_semantic_label_coverage(
        graph=pipeline.graph,
        input_cells=series_cell_keys(pipeline.input_series),
        target_cells=series_cell_keys(pipeline.output_series),
        exempt_cells=frozenset({"Engine!B2", "Engine!C2", "Inputs!B1"}),
        mode="warn",
        context="pytest",
    )
    assert report is not None
    assert report.unlabeled_cells == ()


def test_synthetic_pipeline_config_defaults_validation_to_off(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    assert synthetic_configured_pipeline.config.semantic_label_validation_mode == "off"
    assert (
        synthetic_configured_pipeline.config.semantic_label_exempt_cells == frozenset()
    )


def test_workbook_config_defaults_validation_to_warn() -> None:
    import workbook_config

    assert workbook_config.SEMANTIC_LABEL_VALIDATION_MODE == "warn"
    assert workbook_config.SEMANTIC_LABEL_EXEMPT_CELLS == frozenset()


def test_load_pipeline_config_reads_semantic_label_validation_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config
    from src.pipeline_config import load_pipeline_config

    monkeypatch.setattr(
        workbook_config,
        "SEMANTIC_LABEL_VALIDATION_MODE",
        "error",
        raising=False,
    )
    monkeypatch.setattr(
        workbook_config,
        "SEMANTIC_LABEL_EXEMPT_CELLS",
        frozenset({"Engine!A1"}),
        raising=False,
    )
    config = load_pipeline_config()
    assert config.semantic_label_validation_mode == "error"
    assert config.semantic_label_exempt_cells == frozenset({"Engine!A1"})


def test_build_pipeline_graph_raises_when_validation_mode_is_error(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    config = replace(
        synthetic_configured_pipeline.config,
        semantic_label_validation_mode="error",
    )
    with stub_semantic_labeling():
        with pytest.raises(SemanticLabelCoverageError, match="Engine!B2"):
            build_pipeline_graph(config)


def test_workbook_semantic_label_coverage_gate() -> None:
    from src.pipeline_config import load_pipeline_config, validate_pipeline_config

    config = load_pipeline_config()
    if config.semantic_label_validation_mode == "off":
        pytest.skip("SEMANTIC_LABEL_VALIDATION_MODE is off")
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    with stub_semantic_labeling():
        with pytest.raises(SemanticLabelCoverageError):
            build_pipeline_graph(config)
