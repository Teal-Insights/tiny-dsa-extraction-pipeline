"""Tests for internal binding coverage validation."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.extraction_pipeline import build_pipeline_graph
from src.internal_binding_coverage import (
    InternalBindingCoverageError,
    enforce_internal_binding_coverage,
    enforce_internal_binding_coverage_from_manifest,
    find_unbound_internal_formula_cells,
    find_unbound_internal_formula_cells_from_manifest,
    manifest_binding_addresses,
    required_internal_formula_cells,
)
from tests.conftest import SyntheticConfiguredPipeline


def test_manifest_binding_addresses_expands_list_data_range(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    bindings = {
        "schema_version": "1.13.0",
        "series": [
            {
                "id": "engine_path",
                "sheet": "Engine",
                "data_range": ["Engine!B2", "Engine!C2"],
                "internal": {},
            }
        ],
    }
    addresses = manifest_binding_addresses(
        pipeline.graph,
        cast(WorkbookSeriesBindings, bindings),
        direction="internal",
    )
    assert "Engine!B2" in addresses
    assert "Engine!C2" in addresses


def test_find_unbound_from_manifest_does_not_require_workbook(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    unbound = find_unbound_internal_formula_cells_from_manifest(
        graph=pipeline.graph,
        bindings=pipeline.series_bindings,
        exempt_cells=frozenset(),
        workbook=None,
    )
    assert unbound == ()


def test_enforce_internal_binding_coverage_from_manifest_errors_when_unbound(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    tmp_path,
) -> None:
    from excel_grapher.series_bindings import load_series_bindings

    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    for name in ("inputs.bindings.yaml", "outputs.bindings.yaml"):
        source = synthetic_configured_pipeline.config.bindings_path / name
        (bindings_path / name).write_text(source.read_text(encoding="utf-8"))

    pipeline = synthetic_configured_pipeline
    bindings = load_series_bindings(bindings_path)
    with pytest.raises(InternalBindingCoverageError, match="Engine!B2"):
        enforce_internal_binding_coverage_from_manifest(
            graph=pipeline.graph,
            bindings=bindings,
            exempt_cells=frozenset(),
            mode="warn",
            context="pytest",
        )


def test_enforce_internal_binding_coverage_from_manifest_passes_when_bound(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from excel_grapher.series_bindings import load_series_bindings

    pipeline = synthetic_configured_pipeline
    bindings = load_series_bindings(pipeline.config.bindings_path)
    report = enforce_internal_binding_coverage_from_manifest(
        graph=pipeline.graph,
        bindings=bindings,
        exempt_cells=frozenset(),
        mode="warn",
        context="pytest",
    )
    assert report is not None
    assert report.unbound_cells == ()


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
    from src.dependency_graph_viz import series_cell_keys
    from src.pipeline_config import load_pipeline_config, validate_pipeline_config

    config = load_pipeline_config()
    if config.internal_binding_validation_mode == "off":
        pytest.skip("INTERNAL_BINDING_VALIDATION_MODE is off")
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    graph_result = build_pipeline_graph(config)
    report = enforce_internal_binding_coverage(
        graph=graph_result.graph,
        internal_series=graph_result.internal_series,
        input_cells=series_cell_keys(graph_result.input_series),
        output_cells=series_cell_keys(graph_result.output_series),
        exempt_cells=config.internal_binding_exempt_cells,
        mode=config.internal_binding_validation_mode,
        context="pytest",
    )
    assert report is not None
    assert report.unbound_cells == ()
