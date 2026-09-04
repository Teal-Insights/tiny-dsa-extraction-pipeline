"""Graphviz layout + Cytoscape preset explorer for DependencyGraph."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from src.pipeline_monitor import StageTimer

from excel_grapher.grapher.export import to_graphviz
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey

from src.cytoscape_explorer_html import build_cytoscape_explorer_html
from src.env_utils import env_int
from src.internal_bindings import InternalBindingIndex, internal_binding_for_address

GRAPH_FONT_NAME = "Arial"
GRAPH_FONT_SIZE = 10
GRAPH_NODE_SEP = 0.4
GRAPH_RANK_SEP = 0.7
DEFAULT_JSON_FILENAME = "dependency-graph.json"
DEFAULT_DOT_FILENAME = "dependencies.dot"
DEFAULT_TOPOLOGY_FILENAME = "graph-topology.json"
DEFAULT_GRAPHVIZ_NODE_LIMIT = 10_000
DEFAULT_GRAPHVIZ_EDGE_LIMIT = 50_000
LayoutMode = Literal["graphviz_preset", "structure_only"]
GraphvizLayoutSetting = Literal["auto", "always", "never"]


def _graphviz_layout_setting() -> GraphvizLayoutSetting:
    raw = os.environ.get("GRAPHVIZ_LAYOUT", "auto").strip().lower()
    if raw not in ("auto", "always", "never"):
        raise ValueError(f"GRAPHVIZ_LAYOUT must be auto, always, or never; got {raw!r}")
    return raw


def graph_topology_metrics(graph: DependencyGraph) -> dict[str, Any]:
    """Return node/edge counts with a per-worksheet breakdown."""
    sheet_nodes: dict[str, int] = {}
    sheet_edges: dict[str, int] = {}
    edge_count = 0

    for key in graph.keys(order="workbook"):
        sheet = _node_sheet(key, graph)
        sheet_nodes[sheet] = sheet_nodes.get(sheet, 0) + 1
        for dependency in graph.get_dependencies(key):
            edge_count += 1
            source_sheet = _node_sheet(dependency, graph)
            sheet_edges[source_sheet] = sheet_edges.get(source_sheet, 0) + 1

    sheets = sorted(set(sheet_nodes) | set(sheet_edges))
    return {
        "node_count": len(graph),
        "edge_count": edge_count,
        "sheets": {
            sheet: {
                "node_count": sheet_nodes.get(sheet, 0),
                "edge_count": sheet_edges.get(sheet, 0),
            }
            for sheet in sheets
        },
    }


def graphviz_layout_enabled(metrics: Mapping[str, Any]) -> bool:
    """Decide whether to run Graphviz layout from topology and env settings."""
    setting = _graphviz_layout_setting()
    if setting == "always":
        return True
    if setting == "never":
        return False
    node_limit = env_int("GRAPHVIZ_NODE_LIMIT", DEFAULT_GRAPHVIZ_NODE_LIMIT)
    edge_limit = env_int("GRAPHVIZ_EDGE_LIMIT", DEFAULT_GRAPHVIZ_EDGE_LIMIT)
    return (
        int(metrics["node_count"]) <= node_limit
        and int(metrics["edge_count"]) <= edge_limit
    )


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _cluster_id(label: str, *, prefix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", label)
    return f"{prefix}_{safe}"


def _sheet_cluster_id(sheet: str) -> str:
    return _cluster_id(sheet, prefix="cluster_sheet")


def _node_sheet(key: NodeKey, graph: DependencyGraph) -> str:
    node = graph.get_node(key)
    if node is not None and node.sheet:
        return node.sheet
    if "!" in key:
        return key.split("!", 1)[0]
    return "Workbook"


def _node_line_with_label(node_line: str, label: str) -> str:
    return re.sub(
        r'label="(?:\\.|[^"\\])*"',
        f"label={_quote(label)}",
        node_line,
        count=1,
    )


def _graph_node_lines(
    graph: DependencyGraph,
    flat_dot: str,
    *,
    node_labels: Mapping[NodeKey, str] | None = None,
) -> dict[NodeKey, str]:
    labels = dict(node_labels or {})
    node_lines: dict[NodeKey, str] = {}
    for line in flat_dot.splitlines():
        stripped = line.strip()
        if not stripped.endswith("];") or "[label=" not in stripped:
            continue
        match = re.match(r'^"((?:\\.|[^"\\])*)" \[(.+)\];$', stripped)
        if match is None:
            continue
        key = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
        if graph.get_node(key) is None:
            continue
        node_lines[key] = (
            _node_line_with_label(stripped, labels[key]) if key in labels else stripped
        )
    return node_lines


def _sheet_groups(graph: DependencyGraph) -> dict[str, list[NodeKey]]:
    sheets: dict[str, list[NodeKey]] = {}
    for key in graph.keys(order="workbook"):
        if graph.get_node(key) is None:
            continue
        sheets.setdefault(_node_sheet(key, graph), []).append(key)
    return sheets


def _validate_cluster_groups(
    graph: DependencyGraph,
    clusters: Mapping[str, Iterable[NodeKey]],
) -> dict[str, list[NodeKey]]:
    graph_keys = set(graph.keys(order="workbook"))
    grouped_keys: dict[NodeKey, str] = {}
    normalized: dict[str, list[NodeKey]] = {}
    for label, keys in clusters.items():
        cluster_keys = list(keys)
        unknown = sorted(set(cluster_keys) - graph_keys)
        if unknown:
            raise ValueError(f"Unknown graph cells in cluster {label!r}: {unknown}")
        for key in cluster_keys:
            existing_label = grouped_keys.get(key)
            if existing_label is not None:
                raise ValueError(
                    f"Cell {key!r} appears in both {existing_label!r} and {label!r}"
                )
            grouped_keys[key] = label
        normalized[label] = cluster_keys
    return normalized


def build_dot_with_clusters(
    graph: DependencyGraph,
    *,
    clusters: Mapping[str, Iterable[NodeKey]] | None = None,
    node_labels: Mapping[NodeKey, str] | None = None,
    highlight: set[NodeKey] | None = None,
    rankdir: str = "TB",
    include_formula_on_nodes: bool = True,
    max_formula_length: int | None = 120,
) -> str:
    """Build Graphviz DOT with worksheet clusters or caller-provided clusters."""
    flat_dot = to_graphviz(
        graph,
        highlight=highlight,
        rankdir=rankdir,
        include_formula_on_nodes=include_formula_on_nodes,
        max_formula_length=max_formula_length,
    )

    node_lines = _graph_node_lines(graph, flat_dot, node_labels=node_labels)
    if clusters is None:
        cluster_groups = _sheet_groups(graph)
        cluster_id_for_label = {
            label: _sheet_cluster_id(label) for label in cluster_groups
        }
    else:
        cluster_groups = _validate_cluster_groups(graph, clusters)
        cluster_id_for_label = {
            label: f"{_cluster_id(label, prefix='cluster_group')}_{idx}"
            for idx, label in enumerate(cluster_groups, start=1)
        }
    grouped_keys = {key for keys in cluster_groups.values() for key in keys}

    lines: list[str] = [
        "digraph dependencies {",
        (
            "  graph "
            f"[compound=true, rankdir={_quote(rankdir)}, "
            f"nodesep={GRAPH_NODE_SEP}, ranksep={GRAPH_RANK_SEP}];"
        ),
        (f"  node [fontname={_quote(GRAPH_FONT_NAME)}, fontsize={GRAPH_FONT_SIZE}];"),
        f"  edge [fontname={_quote(GRAPH_FONT_NAME)}, fontsize=9];",
        "",
    ]

    for cluster_label, keys in cluster_groups.items():
        lines.append(f'  subgraph "{cluster_id_for_label[cluster_label]}" {{')
        lines.append(f"    label={_quote(cluster_label)};")
        lines.append("")
        for key in keys:
            node_line = node_lines.get(key)
            if node_line is None:
                continue
            lines.append(f"    {node_line}")
        lines.append("  }")
        lines.append("")

    for key in graph.keys(order="workbook"):
        if key in grouped_keys:
            continue
        node_line = node_lines.get(key)
        if node_line is not None:
            lines.append(f"  {node_line}")
    if len(grouped_keys) < len(node_lines):
        lines.append("")

    for line in flat_dot.splitlines():
        stripped = line.strip()
        if " -> " in stripped and stripped.endswith(";"):
            lines.append(f"  {stripped}")

    lines.append("}")
    return "\n".join(lines)


def parse_graphviz_json(dot_text: str, *, dot_bin: str | None = None) -> dict[str, Any]:
    binary = dot_bin or os.environ.get("DOT_BIN", "dot")
    result = subprocess.run(
        [binary, "-Tjson"],
        input=dot_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip() or result.stdout.strip() or "unknown graphviz error"
        )
        raise RuntimeError(f"Graphviz JSON render failed: {detail}")
    return json.loads(result.stdout)


def _parse_xy(pos: str) -> tuple[float, float]:
    x_text, y_text = pos.split(",", 1)
    return float(x_text), float(y_text)


def _graphviz_size_inches_to_points(value: Any) -> float | None:
    if value is None:
        return None
    try:
        inches = float(value)
    except (TypeError, ValueError):
        return None
    return inches * 72.0


def _graphviz_max_y(graph_json: dict[str, Any]) -> float:
    bb = graph_json.get("bb")
    if not isinstance(bb, str):
        return 0.0
    parts = bb.split(",")
    if len(parts) != 4:
        return 0.0
    return float(parts[3])


def _cluster_depths(cluster_parents: dict[int, int | None]) -> dict[int, int]:
    cache: dict[int, int] = {}

    def depth(cluster_id: int) -> int:
        if cluster_id in cache:
            return cache[cluster_id]
        parent = cluster_parents.get(cluster_id)
        cache[cluster_id] = 0 if parent is None else depth(parent) + 1
        return cache[cluster_id]

    for cluster_id in cluster_parents:
        depth(cluster_id)
    return cache


def _node_role(
    key: NodeKey,
    *,
    target_keys: set[NodeKey],
    input_keys: set[NodeKey],
    output_keys: set[NodeKey],
    constant_keys: set[NodeKey],
) -> str:
    if key in target_keys:
        return "target"
    if key in output_keys:
        return "output"
    if key in input_keys:
        return "input"
    if key in constant_keys:
        return "constant"
    return "internal"


def _attach_internal_binding_fields(
    data: dict[str, Any],
    *,
    address: str,
    internal_binding_index: InternalBindingIndex | None,
) -> None:
    if internal_binding_index is None:
        return
    binding = internal_binding_for_address(internal_binding_index, address)
    if binding is None:
        return
    if binding.key:
        data["binding_keys"] = dict(binding.key)
    if binding.record:
        data["binding_record"] = dict(binding.record)


def build_cytoscape_preset_payload(
    graph: DependencyGraph,
    graphviz_json: dict[str, Any],
    *,
    target_keys: set[NodeKey] | None = None,
    input_keys: set[NodeKey] | None = None,
    output_keys: set[NodeKey] | None = None,
    constant_keys: set[NodeKey] | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
) -> dict[str, Any]:
    objects = graphviz_json.get("objects", [])
    if not isinstance(objects, list):
        raise TypeError("Graphviz JSON missing objects list")

    targets = set(target_keys or ())
    inputs = set(input_keys or ())
    outputs = set(output_keys or ())
    constants = set(constant_keys or ())

    objects_by_gvid: dict[int, dict[str, Any]] = {}
    node_gvid_by_name: dict[str, int] = {}
    cluster_gvids: set[int] = set()
    for obj in objects:
        gvid = obj.get("_gvid")
        name = obj.get("name")
        if not isinstance(gvid, int) or not isinstance(name, str):
            continue
        objects_by_gvid[gvid] = obj
        if name.startswith("cluster_"):
            cluster_gvids.add(gvid)
        else:
            node_gvid_by_name[name] = gvid

    cluster_parents: dict[int, int | None] = dict.fromkeys(cluster_gvids, None)
    for cluster_id in cluster_gvids:
        cluster_obj = objects_by_gvid[cluster_id]
        for child in cluster_obj.get("subgraphs", []) or []:
            if isinstance(child, int) and child in cluster_gvids:
                cluster_parents[child] = cluster_id

    cluster_nodes: dict[int, set[int]] = {
        cluster_id: {
            n
            for n in (objects_by_gvid[cluster_id].get("nodes", []) or [])
            if isinstance(n, int)
        }
        for cluster_id in cluster_gvids
    }
    for cluster_id in cluster_gvids:
        if cluster_parents[cluster_id] is not None:
            continue
        my_nodes = cluster_nodes[cluster_id]
        if not my_nodes:
            continue
        candidates: list[tuple[int, int]] = []
        for other_id in cluster_gvids:
            if other_id == cluster_id:
                continue
            other_nodes = cluster_nodes[other_id]
            if my_nodes < other_nodes:
                candidates.append((len(other_nodes), other_id))
        if candidates:
            candidates.sort()
            cluster_parents[cluster_id] = candidates[0][1]
    cluster_depth = _cluster_depths(cluster_parents)

    node_to_clusters: dict[int, set[int]] = {}
    for cluster_id in cluster_gvids:
        cluster_obj = objects_by_gvid[cluster_id]
        for node_gvid in cluster_obj.get("nodes", []) or []:
            if isinstance(node_gvid, int):
                node_to_clusters.setdefault(node_gvid, set()).add(cluster_id)

    elements_nodes: list[dict[str, Any]] = []
    elements_edges: list[dict[str, Any]] = []
    max_y = _graphviz_max_y(graphviz_json)

    cluster_node_id_by_gvid = {
        gvid: f"cluster::{objects_by_gvid[gvid]['name']}" for gvid in cluster_gvids
    }
    for cluster_id in sorted(cluster_gvids):
        cluster_obj = objects_by_gvid[cluster_id]
        label = cluster_obj.get("label") or cluster_obj.get("name")
        data: dict[str, Any] = {
            "id": cluster_node_id_by_gvid[cluster_id],
            "label": label,
            "type": "cluster",
            "cluster_name": cluster_obj.get("name"),
        }
        parent_id = cluster_parents.get(cluster_id)
        if parent_id is not None:
            data["parent"] = cluster_node_id_by_gvid[parent_id]
        elements_nodes.append({"data": data})

    graph_node_names = set(node_gvid_by_name)
    for key in graph.keys(order="workbook"):
        node_gvid = node_gvid_by_name.get(key)
        if node_gvid is None:
            continue
        node_obj = objects_by_gvid[node_gvid]
        pos = node_obj.get("pos")
        if not isinstance(pos, str):
            continue
        x, y = _parse_xy(pos)
        y = max_y - y

        node = graph.get_node(key)
        assert node is not None
        role = _node_role(
            key,
            target_keys=targets,
            input_keys=inputs,
            output_keys=outputs,
            constant_keys=constants,
        )
        data = {
            "id": key,
            "label": str(node_obj.get("label") or key).replace("\\n", "\n"),
            "type": "cell",
            "sheet": _node_sheet(key, graph),
            "role": role,
            "is_leaf": node.is_leaf,
            "formula": node.formula or node.normalized_formula,
        }
        _attach_internal_binding_fields(
            data,
            address=key,
            internal_binding_index=internal_binding_index,
        )
        w_pt = _graphviz_size_inches_to_points(node_obj.get("width"))
        h_pt = _graphviz_size_inches_to_points(node_obj.get("height"))
        if w_pt is not None and h_pt is not None:
            data["gv_width"] = w_pt
            data["gv_height"] = h_pt
        containing_clusters = node_to_clusters.get(node_gvid, set())
        if containing_clusters:
            leaf_cluster = max(
                containing_clusters, key=lambda cid: cluster_depth.get(cid, 0)
            )
            data["parent"] = cluster_node_id_by_gvid[leaf_cluster]
        elements_nodes.append({"data": data, "position": {"x": x, "y": y}})

    for edge in graphviz_json.get("edges", []) or []:
        tail = edge.get("tail")
        head = edge.get("head")
        if not isinstance(tail, int) or not isinstance(head, int):
            continue
        tail_obj = objects_by_gvid.get(tail)
        head_obj = objects_by_gvid.get(head)
        if tail_obj is None or head_obj is None:
            continue
        source = tail_obj.get("name")
        target = head_obj.get("name")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in graph_node_names or target not in graph_node_names:
            continue
        guarded = edge.get("style") == "dashed"
        elements_edges.append(
            {
                "data": {
                    "id": f"edge::{source}->{target}",
                    "source": source,
                    "target": target,
                    "type": "dependency",
                    "label": edge.get("label") or "",
                    "guarded": guarded,
                }
            }
        )

    sheets = sorted({_node_sheet(key, graph) for key in graph_node_names})
    return {
        "meta": {
            "node_count": len(
                [node for node in elements_nodes if node["data"]["type"] == "cell"]
            ),
            "cluster_count": len(
                [node for node in elements_nodes if node["data"]["type"] == "cluster"]
            ),
            "edge_count": len(elements_edges),
            "sheets": sheets,
            "target_count": len(targets),
            "input_count": len(inputs),
            "output_count": len(outputs),
            "constant_count": len(constants),
        },
        "elements": {
            "nodes": elements_nodes,
            "edges": elements_edges,
        },
    }


def build_cytoscape_structure_payload(
    graph: DependencyGraph,
    *,
    target_keys: set[NodeKey] | None = None,
    input_keys: set[NodeKey] | None = None,
    output_keys: set[NodeKey] | None = None,
    constant_keys: set[NodeKey] | None = None,
    node_labels: Mapping[NodeKey, str] | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
) -> dict[str, Any]:
    """Build a worksheet-clustered Cytoscape payload without Graphviz coordinates."""
    targets = set(target_keys or ())
    inputs = set(input_keys or ())
    outputs = set(output_keys or ())
    constants = set(constant_keys or ())
    labels = dict(node_labels or {})

    elements_nodes: list[dict[str, Any]] = []
    elements_edges: list[dict[str, Any]] = []

    sheet_groups = _sheet_groups(graph)
    cluster_node_id_by_sheet = {
        sheet: f"cluster::{_sheet_cluster_id(sheet)}" for sheet in sheet_groups
    }
    for sheet in sorted(sheet_groups):
        elements_nodes.append(
            {
                "data": {
                    "id": cluster_node_id_by_sheet[sheet],
                    "label": sheet,
                    "type": "cluster",
                    "cluster_name": _sheet_cluster_id(sheet),
                }
            }
        )

    graph_keys = set(graph.keys(order="workbook"))
    for key in graph.keys(order="workbook"):
        node = graph.get_node(key)
        if node is None:
            continue
        role = _node_role(
            key,
            target_keys=targets,
            input_keys=inputs,
            output_keys=outputs,
            constant_keys=constants,
        )
        data: dict[str, Any] = {
            "id": key,
            "label": labels.get(key, key),
            "type": "cell",
            "sheet": _node_sheet(key, graph),
            "role": role,
            "is_leaf": node.is_leaf,
            "formula": node.formula or node.normalized_formula,
            "parent": cluster_node_id_by_sheet[_node_sheet(key, graph)],
        }
        _attach_internal_binding_fields(
            data,
            address=key,
            internal_binding_index=internal_binding_index,
        )
        elements_nodes.append({"data": data})

    seen_edges: set[tuple[str, str]] = set()
    for key in graph.keys(order="workbook"):
        for dependency in graph.get_dependencies(key):
            if dependency not in graph_keys or key not in graph_keys:
                continue
            edge_key = (dependency, key)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            guarded = graph.get_edge_guard(dependency, key) is not None
            elements_edges.append(
                {
                    "data": {
                        "id": f"edge::{dependency}->{key}",
                        "source": dependency,
                        "target": key,
                        "type": "dependency",
                        "label": "",
                        "guarded": guarded,
                    }
                }
            )

    sheets = sorted(sheet_groups)
    return {
        "meta": {
            "layout": "structure_only",
            "node_count": len(
                [node for node in elements_nodes if node["data"]["type"] == "cell"]
            ),
            "cluster_count": len(
                [node for node in elements_nodes if node["data"]["type"] == "cluster"]
            ),
            "edge_count": len(elements_edges),
            "sheets": sheets,
            "target_count": len(targets),
            "input_count": len(inputs),
            "output_count": len(outputs),
            "constant_count": len(constants),
        },
        "elements": {
            "nodes": elements_nodes,
            "edges": elements_edges,
        },
    }


def build_index_html(*, json_filename: str = DEFAULT_JSON_FILENAME) -> str:
    return build_cytoscape_explorer_html(
        json_filename=json_filename,
        layout_mode="graphviz_preset",
    )


def build_structure_only_index_html(
    *, json_filename: str = DEFAULT_JSON_FILENAME
) -> str:
    return build_cytoscape_explorer_html(
        json_filename=json_filename,
        layout_mode="structure_only",
    )


def series_cell_keys(series_items: Iterable[Mapping[str, Any]]) -> set[NodeKey]:
    keys: set[NodeKey] = set()
    for item in series_items:
        for cell in item.get("cells", []):
            address = cell.get("address")
            if isinstance(address, str):
                keys.add(address)
    return keys


_LEAF_COVERAGE_KINDS = frozenset({"constant", "input"})


def unbound_classified_leaf_keys(
    leaf_classification: Mapping[str, str],
    bound_cells: AbstractSet[str],
    *,
    kind: str,
) -> list[str]:
    """Return sorted leaves of ``kind`` that are missing from ``bound_cells``.

    An empty classification, or a classification with no leaves of ``kind``,
    returns ``[]``. Callers must assert emptiness rather than skip.
    """
    if kind not in _LEAF_COVERAGE_KINDS:
        raise ValueError(f"unsupported leaf kind {kind!r}")
    classified = {
        key for key, leaf_kind in leaf_classification.items() if leaf_kind == kind
    }
    return sorted(classified - bound_cells)


def constant_keys_from_leaf_classification(
    leaf_classification: Mapping[str, str],
) -> set[NodeKey]:
    return {key for key, kind in leaf_classification.items() if kind == "constant"}


def write_dependency_graph_site(
    graph: DependencyGraph,
    output_dir: Path,
    *,
    clusters: Mapping[str, Iterable[NodeKey]] | None = None,
    node_labels: Mapping[NodeKey, str] | None = None,
    target_keys: set[NodeKey] | None = None,
    input_keys: set[NodeKey] | None = None,
    output_keys: set[NodeKey] | None = None,
    constant_keys: set[NodeKey] | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    rankdir: str = "TB",
    dot_bin: str | None = None,
    json_filename: str = DEFAULT_JSON_FILENAME,
    timer: StageTimer | None = None,
) -> dict[str, Any]:
    """Write Cytoscape JSON and HTML for a dependency graph."""
    dot_text = build_dot_with_clusters(
        graph,
        clusters=clusters,
        node_labels=node_labels,
        rankdir=rankdir,
    )
    metrics = graph_topology_metrics(graph)
    layout_enabled = graphviz_layout_enabled(metrics)
    layout_mode: LayoutMode = "graphviz_preset" if layout_enabled else "structure_only"

    output_dir.mkdir(parents=True, exist_ok=True)
    dot_path = output_dir / DEFAULT_DOT_FILENAME
    dot_path.write_text(dot_text, encoding="utf-8")

    topology = {
        **metrics,
        "layout_mode": layout_mode,
        "graphviz_layout_enabled": layout_enabled,
        "dot_byte_size": len(dot_text.encode("utf-8")),
    }
    topology_path = output_dir / DEFAULT_TOPOLOGY_FILENAME
    topology_path.write_text(json.dumps(topology, indent=2), encoding="utf-8")

    role_kwargs = {
        "target_keys": target_keys,
        "input_keys": input_keys,
        "output_keys": output_keys,
        "constant_keys": constant_keys,
        "internal_binding_index": internal_binding_index,
    }
    if layout_enabled:
        layout_stage = (
            timer.stage("graphviz_layout") if timer is not None else nullcontext()
        )
        with layout_stage:
            graphviz_json = parse_graphviz_json(dot_text, dot_bin=dot_bin)
        payload = build_cytoscape_preset_payload(
            graph,
            graphviz_json,
            **role_kwargs,
        )
        html = build_index_html(json_filename=json_filename)
    else:
        payload = build_cytoscape_structure_payload(
            graph,
            node_labels=node_labels,
            **role_kwargs,
        )
        html = build_structure_only_index_html(json_filename=json_filename)

    json_path = output_dir / json_filename
    html_path = output_dir / "index.html"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return {**dict(payload["meta"]), "layout_mode": layout_mode}
