from collections.abc import Mapping
from typing import Any

import pytest

from src.dependency_graph_viz import series_cell_keys
from src.semantic_labeling import (
    CellSemanticLabels,
    SemanticLabel,
    SheetSemanticLabels,
    group_candidate_cells_by_sheet,
    label_internal_graph_cells,
    validate_sheet_semantic_labels,
)


@pytest.fixture()
def series_context(tiny_dsa_configured_pipeline):
    pipeline = tiny_dsa_configured_pipeline
    return {
        "graph": pipeline.graph,
        "workbook_path": pipeline.config.workbook_path,
        "bindings": pipeline.series_bindings,
        "input_cells": series_cell_keys(pipeline.input_series),
        "output_cells": series_cell_keys(pipeline.output_series),
    }


def test_group_candidate_cells_by_sheet_excludes_inputs_and_targets(
    series_context,
) -> None:
    tiny_graph = series_context["graph"]
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
    series_context,
) -> None:
    tiny_graph = series_context["graph"]
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
        workbook_path=series_context["workbook_path"],
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
