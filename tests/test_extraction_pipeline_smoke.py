from __future__ import annotations

from unittest.mock import patch

from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    validate_series_bindings,
)

from src.extraction_pipeline import build_pipeline_graph
from src.semantic_labeling import SemanticLabelingSummary


def test_build_pipeline_graph_on_synthetic_workbook(
    synthetic_pipeline_config_fixture,
) -> None:
    config = synthetic_pipeline_config_fixture
    stub_summary = SemanticLabelingSummary(
        labeled_cell_count=0,
        sheet_count=0,
        candidate_cells_by_sheet={},
    )

    with patch(
        "src.extraction_pipeline.label_internal_graph_cells",
        return_value=stub_summary,
    ):
        graph, series_bindings, input_series, output_series = build_pipeline_graph(
            config
        )

    assert series_bindings["schema_version"] == "1.5.0"
    validation = validate_series_bindings(
        graph,
        series_bindings,
        workbook=config.workbook_path,
    )
    assert validation["ok"] is True
    assert len(list(graph)) >= 5
    assert derive_input_series(graph, series_bindings, workbook=config.workbook_path)
    assert derive_output_series(graph, series_bindings, workbook=config.workbook_path)
    assert len(input_series) == 1
    assert len(output_series) == 2
