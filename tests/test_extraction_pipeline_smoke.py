from __future__ import annotations

from excel_grapher.series_bindings import (
    CURRENT_SCHEMA_VERSION,
    derive_input_series,
    derive_internal_series,
    derive_output_series,
    validate_series_bindings,
)

from tests.conftest import SyntheticConfiguredPipeline


def test_build_pipeline_graph_on_synthetic_workbook(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    config = synthetic_configured_pipeline.config
    graph = synthetic_configured_pipeline.graph
    series_bindings = synthetic_configured_pipeline.series_bindings
    input_series = synthetic_configured_pipeline.input_series
    output_series = synthetic_configured_pipeline.output_series

    internal_series = synthetic_configured_pipeline.internal_series

    assert series_bindings["schema_version"] == CURRENT_SCHEMA_VERSION
    validation = validate_series_bindings(
        graph,
        series_bindings,
        workbook=config.workbook_path,
    )
    assert validation["ok"] is True
    assert len(list(graph)) >= 5
    assert derive_input_series(graph, series_bindings, workbook=config.workbook_path)
    assert derive_output_series(graph, series_bindings, workbook=config.workbook_path)
    assert derive_internal_series(graph, series_bindings, workbook=config.workbook_path)
    assert len(input_series) == 1
    assert len(output_series) == 2
    assert len(internal_series) == 2
