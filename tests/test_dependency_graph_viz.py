from pathlib import Path

from excel_grapher.grapher import to_graphviz
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
)

from src.dependency_graph_viz import (
    build_cytoscape_preset_payload,
    build_dot_with_clusters,
    constant_keys_from_leaf_classification,
    parse_graphviz_json,
    series_cell_keys,
    write_dependency_graph_site,
)
from src.internal_bindings import binding_node_labels, build_internal_binding_index


def test_build_dot_with_clusters_wraps_to_graphviz_nodes(
    tiny_dsa_configured_pipeline,
) -> None:
    tiny_graph = tiny_dsa_configured_pipeline.graph
    flat = to_graphviz(tiny_graph, max_formula_length=40)
    clustered = build_dot_with_clusters(tiny_graph, max_formula_length=40)
    assert "subgraph" in clustered
    assert "cluster_sheet_" in clustered
    for line in flat.splitlines():
        stripped = line.strip()
        if stripped.endswith("];") and "[label=" in stripped:
            assert stripped in clustered or f"    {stripped}" in clustered


def test_parse_graphviz_json_and_build_payload(tiny_dsa_configured_pipeline) -> None:
    pipeline = tiny_dsa_configured_pipeline
    tiny_graph = pipeline.graph
    dot_text = build_dot_with_clusters(tiny_graph, max_formula_length=40)
    graphviz_json = parse_graphviz_json(dot_text)
    bindings_path = Path(__file__).resolve().parents[1] / "bindings"
    series_bindings = load_series_bindings(bindings_path)
    input_series = derive_input_series(
        tiny_graph, series_bindings, workbook=pipeline.config.workbook_path
    )
    output_series = derive_output_series(
        tiny_graph, series_bindings, workbook=pipeline.config.workbook_path
    )
    payload = build_cytoscape_preset_payload(
        tiny_graph,
        graphviz_json,
        target_keys=series_cell_keys(output_series),
        input_keys=series_cell_keys(input_series),
        output_keys=series_cell_keys(output_series),
        constant_keys=constant_keys_from_leaf_classification(
            pipeline.leaf_classification
        ),
    )
    assert payload["meta"]["node_count"] == len(list(tiny_graph))
    assert payload["meta"]["cluster_count"] >= 1
    assert payload["meta"]["edge_count"] > 0
    assert payload["meta"]["constant_count"] > 0
    constant_nodes = [
        node
        for node in payload["elements"]["nodes"]
        if node["data"].get("role") == "constant"
    ]
    assert constant_nodes
    assert payload["elements"]["nodes"]
    assert payload["elements"]["edges"]


def test_build_dot_with_custom_clusters_and_node_labels(
    tiny_dsa_configured_pipeline,
) -> None:
    tiny_graph = tiny_dsa_configured_pipeline.graph
    node_labels = {
        "Engine!C10": "Engine!C10\nrow: Baseline debt",
        "Engine!D10": "Engine!D10\nrow: Baseline debt",
    }
    dot_text = build_dot_with_clusters(
        tiny_graph,
        clusters={"Debt dynamics": ["Engine!C10", "Engine!D10"]},
        node_labels=node_labels,
        include_formula_on_nodes=False,
    )
    graphviz_json = parse_graphviz_json(dot_text)
    payload = build_cytoscape_preset_payload(tiny_graph, graphviz_json)

    cluster_nodes = [
        node
        for node in payload["elements"]["nodes"]
        if node["data"].get("type") == "cluster"
    ]
    engine_c10 = next(
        node
        for node in payload["elements"]["nodes"]
        if node["data"].get("id") == "Engine!C10"
    )

    assert any(node["data"].get("label") == "Debt dynamics" for node in cluster_nodes)
    assert engine_c10["data"]["label"] == "Engine!C10\nrow: Baseline debt"
    assert engine_c10["data"]["parent"].startswith("cluster::cluster_group_")


def test_binding_node_labels_include_key_and_record_metadata(
    tiny_dsa_configured_pipeline,
) -> None:
    tiny_graph = tiny_dsa_configured_pipeline.graph
    internal_binding_index = {
        "Engine!C10": {
            "address": "Engine!C10",
            "key": {"TIME_PERIOD": 1, "INDICATOR": "debt_to_gdp"},
            "record": {"OBS_VALUE": 0.0},
        }
    }

    labels = binding_node_labels(
        tiny_graph,
        internal_binding_index,
        keys=["Engine!C10"],
    )

    node = tiny_graph.get_node("Engine!C10")
    formula = node.formula or node.normalized_formula
    assert formula is not None
    assert labels == {
        "Engine!C10": (
            "Engine!C10\n"
            f"{formula}\n"
            "keys: INDICATOR='debt_to_gdp', TIME_PERIOD=1\n"
            "record: OBS_VALUE=0.0"
        )
    }


def test_payload_node_data_includes_internal_binding_fields(
    tiny_dsa_configured_pipeline,
) -> None:
    tiny_graph = tiny_dsa_configured_pipeline.graph
    internal_binding_index = build_internal_binding_index(
        [
            {
                "cells": [
                    {
                        "address": "Engine!C10",
                        "key": {"TIME_PERIOD": 1},
                        "record": {"INDICATOR": "debt_to_gdp"},
                    }
                ]
            }
        ]
    )
    dot_text = build_dot_with_clusters(tiny_graph, max_formula_length=40)
    graphviz_json = parse_graphviz_json(dot_text)
    payload = build_cytoscape_preset_payload(
        tiny_graph,
        graphviz_json,
        internal_binding_index=internal_binding_index,
    )

    engine_c10 = next(
        node
        for node in payload["elements"]["nodes"]
        if node["data"].get("id") == "Engine!C10"
    )

    assert engine_c10["data"]["binding_keys"] == {"TIME_PERIOD": 1}
    assert engine_c10["data"]["binding_record"] == {"INDICATOR": "debt_to_gdp"}


def test_write_dependency_graph_site(
    tmp_path: Path, tiny_dsa_configured_pipeline
) -> None:
    tiny_graph = tiny_dsa_configured_pipeline.graph
    meta = write_dependency_graph_site(tiny_graph, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "dependency-graph.json").is_file()
    assert "fetch(" in html
    assert meta["node_count"] == len(list(tiny_graph))
