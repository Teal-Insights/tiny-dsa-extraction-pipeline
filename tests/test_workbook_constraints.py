from __future__ import annotations

import pytest
from excel_grapher.core.cell_types import normalize_cell_type_env_key
from excel_grapher.series_bindings import validate_series_bindings

from src.dependency_graph_viz import series_cell_keys
from src.extraction_pipeline import classify_leaves_from_constraints


def test_every_graph_leaf_has_a_typed_constraint(
    synthetic_configured_pipeline,
) -> None:
    graph = synthetic_configured_pipeline.graph
    config = synthetic_configured_pipeline.config
    normalized_constraint_keys = {
        normalize_cell_type_env_key(key) for key in config.constraints
    }
    leaf_keys = list(graph.leaf_keys())
    missing = [
        key
        for key in leaf_keys
        if normalize_cell_type_env_key(key) not in normalized_constraint_keys
    ]
    assert missing == [], f"missing constraints for leaf cells: {missing!r}"


def test_input_binding_cells_are_graph_leaves(synthetic_configured_pipeline) -> None:
    graph = synthetic_configured_pipeline.graph
    input_keys = series_cell_keys(synthetic_configured_pipeline.input_series)
    leaf_keys = set(graph.leaf_keys())
    unbound = sorted(input_keys - leaf_keys)
    assert unbound == [], f"input bindings reference non-leaf cells: {unbound!r}"


def test_validate_series_bindings_passes(synthetic_configured_pipeline) -> None:
    graph = synthetic_configured_pipeline.graph
    config = synthetic_configured_pipeline.config
    validation = validate_series_bindings(
        graph,
        synthetic_configured_pipeline.series_bindings,
        workbook=config.workbook_path,
    )
    assert validation["ok"] is True, (
        f"series bindings failed validation: {validation['issues']!r}"
    )


def test_leaf_classification_matches_constraint_kinds(
    synthetic_configured_pipeline,
) -> None:
    graph = synthetic_configured_pipeline.graph
    expected = classify_leaves_from_constraints(
        synthetic_configured_pipeline.config.constraints,
        graph.leaf_keys(),
    )
    assert graph.leaf_classification == expected
    assert graph.leaf_classification["Inputs!A1"] == "input"
    assert graph.leaf_classification["Inputs!B1"] == "constant"


def test_missing_constraint_reports_graph_leaf_keys(synthetic_graph) -> None:
    with pytest.raises(
        KeyError,
        match=r"missing constraints for leaf cells: \['Inputs!A1', 'Inputs!B1'\]",
    ):
        classify_leaves_from_constraints({}, synthetic_graph.leaf_keys())
