from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from excel_grapher.core.cell_types import normalize_cell_type_env_key
from excel_grapher.series_bindings import validate_series_bindings

from src.dependency_graph_viz import series_cell_keys, unbound_classified_leaf_keys
from src.extraction_pipeline import (
    PipelineGraphResult,
    build_pipeline_graph,
    classify_leaves_from_constraints,
)
from src.pipeline_config import PipelineConfig


def _empty_series_shard(text: str) -> str:
    marker = "\nseries:"
    index = text.find(marker)
    if index < 0:
        raise ValueError("bindings shard is missing a series key")
    return f"{text[: index + len(marker)]} []\n"


def _pipeline_with_emptied_binding_shard(
    base_config: PipelineConfig,
    tmp_path: Path,
    shard_name: str,
) -> PipelineGraphResult:
    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    for source in base_config.bindings_path.glob("*.bindings.yaml"):
        text = source.read_text(encoding="utf-8")
        if source.name == shard_name:
            text = _empty_series_shard(text)
        (bindings_path / source.name).write_text(text, encoding="utf-8")
    return build_pipeline_graph(replace(base_config, bindings_path=bindings_path))


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
    assert synthetic_configured_pipeline.leaf_classification == expected
    assert synthetic_configured_pipeline.leaf_classification["Inputs!A1"] == "input"
    assert synthetic_configured_pipeline.leaf_classification["Inputs!B1"] == "constant"


def test_missing_constraint_reports_graph_leaf_keys(synthetic_graph) -> None:
    with pytest.raises(
        KeyError,
        match=r"missing constraints for leaf cells: \['Inputs!A1', 'Inputs!B1'\]",
    ):
        classify_leaves_from_constraints({}, synthetic_graph.leaf_keys())


def test_every_constant_leaf_has_constant_series_binding(
    synthetic_configured_pipeline,
) -> None:
    unbound = unbound_classified_leaf_keys(
        synthetic_configured_pipeline.leaf_classification,
        series_cell_keys(synthetic_configured_pipeline.constant_series),
        kind="constant",
    )
    assert unbound == [], (
        "Constant leaves missing from constants.bindings.yaml: " + ", ".join(unbound)
    )


def test_every_mutable_input_leaf_has_input_series_binding(
    synthetic_configured_pipeline,
) -> None:
    unbound = unbound_classified_leaf_keys(
        synthetic_configured_pipeline.leaf_classification,
        series_cell_keys(synthetic_configured_pipeline.input_series),
        kind="input",
    )
    assert unbound == [], (
        "Mutable input leaves missing from inputs.bindings.yaml: " + ", ".join(unbound)
    )


def test_constant_leaf_coverage_is_vacuous_when_no_constant_leaves() -> None:
    unbound = unbound_classified_leaf_keys(
        {"Inputs!A1": "input"},
        frozenset(),
        kind="constant",
    )
    assert unbound == []


def test_omitted_constant_binding_reports_unbound_leaf(
    synthetic_pipeline_config_fixture: PipelineConfig,
    tmp_path: Path,
) -> None:
    result = _pipeline_with_emptied_binding_shard(
        synthetic_pipeline_config_fixture,
        tmp_path,
        "constants.bindings.yaml",
    )
    unbound = unbound_classified_leaf_keys(
        result.leaf_classification,
        series_cell_keys(result.constant_series),
        kind="constant",
    )
    assert unbound == ["Inputs!B1"]


def test_omitted_input_binding_reports_unbound_leaf(
    synthetic_pipeline_config_fixture: PipelineConfig,
    tmp_path: Path,
) -> None:
    result = _pipeline_with_emptied_binding_shard(
        synthetic_pipeline_config_fixture,
        tmp_path,
        "inputs.bindings.yaml",
    )
    unbound = unbound_classified_leaf_keys(
        result.leaf_classification,
        series_cell_keys(result.input_series),
        kind="input",
    )
    assert unbound == ["Inputs!A1"]


def test_unbound_classified_leaf_keys_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported leaf kind 'formula'"):
        unbound_classified_leaf_keys({}, frozenset(), kind="formula")
