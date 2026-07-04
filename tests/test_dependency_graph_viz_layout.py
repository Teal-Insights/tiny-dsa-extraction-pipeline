from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dependency_graph_viz import (
    build_cytoscape_structure_payload,
    graph_topology_metrics,
    graphviz_layout_enabled,
    write_dependency_graph_site,
)


def test_graph_topology_metrics_includes_per_sheet_breakdown(synthetic_graph) -> None:
    metrics = graph_topology_metrics(synthetic_graph)

    assert metrics["node_count"] == 6
    assert metrics["edge_count"] == 6
    assert "sheets" in metrics
    assert metrics["sheets"]["Engine"]["node_count"] >= 1
    assert metrics["sheets"]["Engine"]["edge_count"] >= 0


@pytest.mark.parametrize(
    ("layout", "node_count", "edge_count", "expected"),
    [
        ("always", 100_000, 200_000, True),
        ("never", 1, 1, False),
        ("auto", 5, 4, True),
        ("auto", 10_001, 4, False),
        ("auto", 5, 50_001, False),
    ],
)
def test_graphviz_layout_enabled(
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    node_count: int,
    edge_count: int,
    expected: bool,
) -> None:
    monkeypatch.setenv("GRAPHVIZ_LAYOUT", layout)
    metrics = {"node_count": node_count, "edge_count": edge_count}
    assert graphviz_layout_enabled(metrics) is expected


def test_small_graph_writes_graphviz_preset_layout(
    synthetic_graph,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "graph-site"
    meta = write_dependency_graph_site(synthetic_graph, output_dir)

    payload = json.loads(
        (output_dir / "dependency-graph.json").read_text(encoding="utf-8")
    )
    cell_nodes = [
        node for node in payload["elements"]["nodes"] if node["data"]["type"] == "cell"
    ]
    assert cell_nodes
    assert all("position" in node for node in cell_nodes)
    assert meta["layout_mode"] == "graphviz_preset"

    topology = json.loads(
        (output_dir / "graph-topology.json").read_text(encoding="utf-8")
    )
    assert topology["layout_mode"] == "graphviz_preset"
    assert topology["graphviz_layout_enabled"] is True
    assert topology["dot_byte_size"] > 0
    assert topology["sheets"]["Engine"]["node_count"] >= 1
    assert (output_dir / "dependencies.dot").is_file()


def test_large_graph_skips_graphviz_layout(
    synthetic_graph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHVIZ_LAYOUT", "auto")
    monkeypatch.setenv("GRAPHVIZ_NODE_LIMIT", "3")

    output_dir = tmp_path / "graph-site"
    meta = write_dependency_graph_site(synthetic_graph, output_dir)

    payload = json.loads(
        (output_dir / "dependency-graph.json").read_text(encoding="utf-8")
    )
    cell_nodes = [
        node for node in payload["elements"]["nodes"] if node["data"]["type"] == "cell"
    ]
    assert cell_nodes
    assert all("position" not in node for node in cell_nodes)
    assert payload["meta"]["layout"] == "structure_only"
    assert meta["layout_mode"] == "structure_only"

    html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "Graphviz layout skipped" in html

    topology = json.loads(
        (output_dir / "graph-topology.json").read_text(encoding="utf-8")
    )
    assert topology["graphviz_layout_enabled"] is False


def test_graphviz_layout_always_forces_preset_layout(
    synthetic_graph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHVIZ_LAYOUT", "always")
    monkeypatch.setenv("GRAPHVIZ_NODE_LIMIT", "1")

    output_dir = tmp_path / "graph-site"
    meta = write_dependency_graph_site(synthetic_graph, output_dir)

    payload = json.loads(
        (output_dir / "dependency-graph.json").read_text(encoding="utf-8")
    )
    cell_nodes = [
        node for node in payload["elements"]["nodes"] if node["data"]["type"] == "cell"
    ]
    assert all("position" in node for node in cell_nodes)
    assert meta["layout_mode"] == "graphviz_preset"


def test_build_cytoscape_structure_payload_clusters_by_sheet(synthetic_graph) -> None:
    payload = build_cytoscape_structure_payload(synthetic_graph)

    cluster_nodes = [
        node
        for node in payload["elements"]["nodes"]
        if node["data"]["type"] == "cluster"
    ]
    cell_nodes = [
        node for node in payload["elements"]["nodes"] if node["data"]["type"] == "cell"
    ]
    assert cluster_nodes
    assert cell_nodes
    assert all("position" not in node for node in cell_nodes)
    assert payload["meta"]["layout"] == "structure_only"
    assert payload["meta"]["node_count"] == 6
