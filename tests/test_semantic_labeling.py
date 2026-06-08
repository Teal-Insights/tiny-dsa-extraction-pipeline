from pathlib import Path
from collections.abc import Mapping
from typing import Any

import pytest
from excel_grapher.grapher import DynamicRefConfig, create_dependency_graph
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
)

from src.dependency_graph_viz import series_cell_keys
from src.extraction_pipeline import constraints, targets, workbook_path
from src.semantic_labeling import (
    CellSemanticLabels,
    SemanticLabel,
    SheetSemanticLabels,
    group_candidate_cells_by_sheet,
    label_internal_graph_cells,
    validate_sheet_semantic_labels,
)


@pytest.fixture()
def tiny_graph():
    config = DynamicRefConfig.from_constraints(constraints, {})
    return create_dependency_graph(
        workbook_path,
        targets,
        load_values=True,
        dynamic_refs=config,
    )


@pytest.fixture()
def series_context(tiny_graph):
    bindings_path = Path(__file__).resolve().parents[1] / "bindings"
    series_bindings = load_series_bindings(bindings_path)
    input_series = derive_input_series(
        tiny_graph,
        series_bindings,
        workbook=workbook_path,
    )
    output_series = derive_output_series(
        tiny_graph,
        series_bindings,
        workbook=workbook_path,
    )
    return {
        "bindings": series_bindings,
        "input_cells": series_cell_keys(input_series),
        "output_cells": series_cell_keys(output_series),
    }


def test_group_candidate_cells_by_sheet_excludes_inputs_and_targets(
    tiny_graph,
    series_context,
) -> None:
    target_cells = series_context["output_cells"] | set(tiny_graph.target_keys())
    candidate_cells_by_sheet = group_candidate_cells_by_sheet(
        tiny_graph,
        input_cells=series_context["input_cells"],
        target_cells=target_cells,
    )

    candidate_cells = {
        cell
        for sheet_cells in candidate_cells_by_sheet.values()
        for cell in sheet_cells
    }

    assert candidate_cells
    assert candidate_cells.isdisjoint(series_context["input_cells"])
    assert candidate_cells.isdisjoint(target_cells)
    assert candidate_cells.issubset(set(tiny_graph.keys(order="workbook")))


def test_validate_sheet_semantic_labels_rejects_unknown_concepts() -> None:
    labels = SheetSemanticLabels(
        cells=[
            CellSemanticLabels(
                address="Sheet1!A1",
                row_labels=[
                    SemanticLabel(
                        label="1",
                        concept="YEAR",
                        source_address="Sheet1!A1",
                    )
                ],
            )
        ]
    )

    with pytest.raises(RuntimeError, match="Unknown concept"):
        validate_sheet_semantic_labels(
            labels,
            candidate_addresses={"Sheet1!A1"},
            valid_concepts={"TIME_PERIOD"},
        )


def test_label_internal_graph_cells_applies_provider_metadata(
    tiny_graph,
    series_context,
) -> None:
    calls: list[tuple[str, list[str], list[dict[str, Any]]]] = []

    def provider(
        sheet_name: str,
        candidate_addresses: list[str],
        sheet_cells: list[dict[str, Any]],
        concept_scheme: Mapping[str, Any],
    ) -> SheetSemanticLabels:
        calls.append((sheet_name, candidate_addresses, sheet_cells))
        assert concept_scheme["id"] == "tiny_dsa"
        return SheetSemanticLabels(
            cells=[
                CellSemanticLabels(
                    address=address,
                    row_labels=[
                        SemanticLabel(
                            label="Debt-to-GDP ratio",
                            concept="INDICATOR",
                            source_address=f"{sheet_name}!A1",
                        )
                    ],
                )
                for address in candidate_addresses
            ]
        )

    summary = label_internal_graph_cells(
        graph=tiny_graph,
        workbook_path=workbook_path,
        input_cells=series_context["input_cells"],
        target_cells=series_context["output_cells"],
        concept_scheme=series_context["bindings"]["concept_scheme"],
        provider=provider,
    )

    first_candidate = next(
        cell
        for sheet_cells in summary.candidate_cells_by_sheet.values()
        for cell in sheet_cells
    )
    metadata = tiny_graph.get_node(first_candidate).metadata

    assert calls
    assert summary.labeled_cell_count > 0
    assert metadata["row_labels"][0]["label"] == "Debt-to-GDP ratio"
    assert metadata["row_labels"][0]["concept"] == "INDICATOR"
