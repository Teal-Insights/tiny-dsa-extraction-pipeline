"""Tests for internal series binding derivation and indexing."""

from __future__ import annotations

from src.extraction_pipeline import build_pipeline_graph
from src.internal_bindings import (
    build_internal_binding_index,
    internal_series_cell_keys,
)
from tests.conftest import SyntheticConfiguredPipeline


def test_build_pipeline_graph_derives_internal_series(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    graph_result = build_pipeline_graph(synthetic_configured_pipeline.config)
    assert graph_result.internal_series
    internal_addresses = internal_series_cell_keys(graph_result.internal_series)
    assert internal_addresses == {"Engine!B2", "Engine!C2"}


def test_internal_binding_index_maps_formula_addresses(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    index = build_internal_binding_index(synthetic_configured_pipeline.internal_series)
    assert set(index) == {"Engine!B2", "Engine!C2"}
    assert index["Engine!B2"]["address"] == "Engine!B2"
    assert "key" in index["Engine!B2"]
    assert "record" in index["Engine!B2"]


def test_graph_nodes_do_not_carry_semantic_label_metadata(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    for address in ("Engine!B2", "Engine!C2"):
        node = synthetic_configured_pipeline.graph.get_node(address)
        assert node is not None
        metadata = node.metadata or {}
        assert "table_labels" not in metadata
        assert "row_labels" not in metadata
        assert "column_labels" not in metadata
