from pathlib import Path

import pytest

from excel_grapher.grapher import create_dependency_graph, DynamicRefConfig, to_graphviz
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
)

from src.dependency_graph_viz import (
    build_cytoscape_preset_payload,
    build_dot_with_sheet_clusters,
    constant_keys_from_leaf_classification,
    parse_graphviz_json,
    series_cell_keys,
    write_dependency_graph_site,
)
from src.extraction_pipeline import (
    constraints,
    leaf_classification,
    targets,
    workbook_path,
)


@pytest.fixture(scope="module")
def tiny_graph():
    config = DynamicRefConfig.from_constraints(constraints, {})
    return create_dependency_graph(
        workbook_path,
        targets,
        load_values=True,
        dynamic_refs=config,
    )


def test_build_dot_with_sheet_clusters_wraps_to_graphviz_nodes(tiny_graph) -> None:
    flat = to_graphviz(tiny_graph, max_formula_length=40)
    clustered = build_dot_with_sheet_clusters(tiny_graph, max_formula_length=40)
    assert "subgraph" in clustered
    assert "cluster_sheet_" in clustered
    for line in flat.splitlines():
        stripped = line.strip()
        if stripped.endswith("];") and "[label=" in stripped:
            assert stripped in clustered or f"    {stripped}" in clustered


def test_parse_graphviz_json_and_build_payload(tiny_graph) -> None:
    dot_text = build_dot_with_sheet_clusters(tiny_graph, max_formula_length=40)
    graphviz_json = parse_graphviz_json(dot_text)
    bindings_path = Path(__file__).resolve().parents[1] / "bindings"
    series_bindings = load_series_bindings(bindings_path)
    input_series = derive_input_series(
        tiny_graph, series_bindings, workbook=workbook_path
    )
    output_series = derive_output_series(
        tiny_graph, series_bindings, workbook=workbook_path
    )
    payload = build_cytoscape_preset_payload(
        tiny_graph,
        graphviz_json,
        target_keys=series_cell_keys(output_series),
        input_keys=series_cell_keys(input_series),
        output_keys=series_cell_keys(output_series),
        constant_keys=constant_keys_from_leaf_classification(leaf_classification),
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


def test_write_dependency_graph_site(tmp_path: Path, tiny_graph) -> None:
    meta = write_dependency_graph_site(tiny_graph, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "dependency-graph.json").is_file()
    assert "fetch(" in html
    assert meta["node_count"] == len(list(tiny_graph))
