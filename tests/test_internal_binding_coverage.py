"""Tests for internal binding coverage validation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.extraction_pipeline import build_pipeline_graph
from src.internal_binding_coverage import (
    InternalBindingCoverageError,
    enforce_internal_binding_coverage,
    find_unbound_internal_formula_cells,
    required_internal_formula_cells,
)
from tests.conftest import SyntheticConfiguredPipeline


def test_required_internal_formula_cells_excludes_public_io(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    required = required_internal_formula_cells(
        pipeline.graph,
        input_cells=series_cell_keys(pipeline.input_series),
        output_cells=series_cell_keys(pipeline.output_series),
    )
    assert required == ("Engine!B2", "Engine!C2")


def test_find_unbound_internal_formula_cells_respects_exempt_list(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    input_cells = series_cell_keys(pipeline.input_series)
    output_cells = series_cell_keys(pipeline.output_series)
    unbound = find_unbound_internal_formula_cells(
        graph=pipeline.graph,
        internal_series=(),
        input_cells=input_cells,
        output_cells=output_cells,
        exempt_cells=frozenset({"Engine!B2", "Engine!C2"}),
    )
    assert unbound == ()


def test_enforce_internal_binding_coverage_off_is_noop(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    report = enforce_internal_binding_coverage(
        graph=pipeline.graph,
        internal_series=pipeline.internal_series,
        input_cells=series_cell_keys(pipeline.input_series),
        output_cells=series_cell_keys(pipeline.output_series),
        exempt_cells=frozenset(),
        mode="off",
        context="pytest",
    )
    assert report is None


def test_enforce_internal_binding_coverage_pytest_errors_when_unbound(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with pytest.raises(InternalBindingCoverageError, match="Engine!B2"):
        enforce_internal_binding_coverage(
            graph=pipeline.graph,
            internal_series=(),
            input_cells=series_cell_keys(pipeline.input_series),
            output_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="warn",
            context="pytest",
        )


def test_enforce_internal_binding_coverage_pipeline_warns_without_raising(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with caplog.at_level("WARNING"):
        report = enforce_internal_binding_coverage(
            graph=pipeline.graph,
            internal_series=(),
            input_cells=series_cell_keys(pipeline.input_series),
            output_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="warn",
            context="pipeline",
        )
    assert report is not None
    assert report.unbound_cells
    assert "Engine!B2" in caplog.text


def test_enforce_internal_binding_coverage_pipeline_error_raises(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    with pytest.raises(InternalBindingCoverageError, match="Engine!B2"):
        enforce_internal_binding_coverage(
            graph=pipeline.graph,
            internal_series=(),
            input_cells=series_cell_keys(pipeline.input_series),
            output_cells=series_cell_keys(pipeline.output_series),
            exempt_cells=frozenset(),
            mode="error",
            context="pipeline",
        )


def test_enforce_internal_binding_coverage_passes_when_bound(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.dependency_graph_viz import series_cell_keys

    pipeline = synthetic_configured_pipeline
    report = enforce_internal_binding_coverage(
        graph=pipeline.graph,
        internal_series=pipeline.internal_series,
        input_cells=series_cell_keys(pipeline.input_series),
        output_cells=series_cell_keys(pipeline.output_series),
        exempt_cells=frozenset(),
        mode="warn",
        context="pytest",
    )
    assert report is not None
    assert report.unbound_cells == ()


def test_synthetic_pipeline_config_defaults_validation_to_off(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    assert (
        synthetic_configured_pipeline.config.internal_binding_validation_mode == "off"
    )
    assert (
        synthetic_configured_pipeline.config.internal_binding_exempt_cells
        == frozenset()
    )


def test_workbook_config_defaults_validation_to_warn() -> None:
    import workbook_config

    assert workbook_config.INTERNAL_BINDING_VALIDATION_MODE == "warn"
    assert workbook_config.INTERNAL_BINDING_EXEMPT_CELLS == frozenset()


def test_load_pipeline_config_reads_internal_binding_validation_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config
    from src.pipeline_config import load_pipeline_config

    monkeypatch.setattr(
        workbook_config,
        "INTERNAL_BINDING_VALIDATION_MODE",
        "error",
        raising=False,
    )
    monkeypatch.setattr(
        workbook_config,
        "INTERNAL_BINDING_EXEMPT_CELLS",
        frozenset({"Engine!A1"}),
        raising=False,
    )
    config = load_pipeline_config()
    assert config.internal_binding_validation_mode == "error"
    assert config.internal_binding_exempt_cells == frozenset({"Engine!A1"})


def test_build_pipeline_graph_raises_when_validation_mode_is_error(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    tmp_path,
) -> None:
    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    for name in ("inputs.bindings.yaml", "outputs.bindings.yaml"):
        source = synthetic_configured_pipeline.config.bindings_path / name
        (bindings_path / name).write_text(source.read_text(encoding="utf-8"))

    config = replace(
        synthetic_configured_pipeline.config,
        bindings_path=bindings_path,
        internal_binding_validation_mode="error",
    )
    with pytest.raises(InternalBindingCoverageError, match="Engine!B2"):
        build_pipeline_graph(config)


def test_workbook_internal_binding_coverage_gate() -> None:
    from src.pipeline_config import load_pipeline_config, validate_pipeline_config

    config = load_pipeline_config()
    if config.internal_binding_validation_mode == "off":
        pytest.skip("INTERNAL_BINDING_VALIDATION_MODE is off")
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    with pytest.raises(InternalBindingCoverageError):
        build_pipeline_graph(config)
