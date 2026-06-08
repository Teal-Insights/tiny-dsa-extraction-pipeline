"""Graphviz layout + Cytoscape preset explorer for DependencyGraph."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from excel_grapher.grapher.export import to_graphviz
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey

GRAPH_FONT_NAME = "Arial"
GRAPH_FONT_SIZE = 10
GRAPH_NODE_SEP = 0.4
GRAPH_RANK_SEP = 0.7
DEFAULT_JSON_FILENAME = "dependency-graph.json"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sheet_cluster_id(sheet: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", sheet)
    return f"cluster_sheet_{safe}"


def _node_sheet(key: NodeKey, graph: DependencyGraph) -> str:
    node = graph.get_node(key)
    if node is not None and node.sheet:
        return node.sheet
    if "!" in key:
        return key.split("!", 1)[0]
    return "Workbook"


def build_dot_with_sheet_clusters(
    graph: DependencyGraph,
    *,
    highlight: set[NodeKey] | None = None,
    rankdir: str = "TB",
    include_formula_on_nodes: bool = True,
    max_formula_length: int | None = 120,
) -> str:
    """Extend ``to_graphviz`` output with one Graphviz cluster per worksheet."""
    flat_dot = to_graphviz(
        graph,
        highlight=highlight,
        rankdir=rankdir,
        include_formula_on_nodes=include_formula_on_nodes,
        max_formula_length=max_formula_length,
    )

    sheets: dict[str, list[NodeKey]] = {}
    for key in graph.keys(order="workbook"):
        if graph.get_node(key) is None:
            continue
        sheets.setdefault(_node_sheet(key, graph), []).append(key)

    node_lines: dict[NodeKey, str] = {}
    for line in flat_dot.splitlines():
        stripped = line.strip()
        if not stripped.endswith("];") or "[label=" not in stripped:
            continue
        match = re.match(r'^"((?:\\.|[^"\\])*)" \[(.+)\];$', stripped)
        if match is None:
            continue
        key = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
        node_lines[key] = stripped

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

    for sheet in sorted(sheets):
        lines.append(f'  subgraph "{_sheet_cluster_id(sheet)}" {{')
        lines.append(f"    label={_quote(sheet)};")
        lines.append("")
        for key in sheets[sheet]:
            node_line = node_lines.get(key)
            if node_line is None:
                continue
            lines.append(f"    {node_line}")
        lines.append("  }")
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


def build_cytoscape_preset_payload(
    graph: DependencyGraph,
    graphviz_json: dict[str, Any],
    *,
    target_keys: set[NodeKey] | None = None,
    input_keys: set[NodeKey] | None = None,
    output_keys: set[NodeKey] | None = None,
    constant_keys: set[NodeKey] | None = None,
) -> dict[str, Any]:
    objects = graphviz_json.get("objects", [])
    if not isinstance(objects, list):
        raise RuntimeError("Graphviz JSON missing objects list")

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
            "formula": node.formula,
        }
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


def build_index_html(*, json_filename: str = DEFAULT_JSON_FILENAME) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dependency Graph (Graphviz preset + Cytoscape)</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    }}
    body {{
      margin: 0;
      display: grid;
      grid-template-columns: 320px 1fr;
      height: 100vh;
    }}
    #sidebar {{
      border-right: 1px solid #8884;
      padding: 12px;
      overflow: auto;
    }}
    #cy {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    input, select, button {{
      width: 100%;
      margin: 0.3rem 0;
      padding: 0.45rem;
      box-sizing: border-box;
    }}
    .muted {{
      opacity: 0.8;
      font-size: 0.9rem;
    }}
    .legend {{
      display: grid;
      gap: 0.35rem;
      margin-top: 0.75rem;
      font-size: 0.85rem;
    }}
    .swatch {{
      display: inline-block;
      width: 0.85rem;
      height: 0.85rem;
      border-radius: 0.15rem;
      margin-right: 0.35rem;
      vertical-align: -0.1rem;
    }}
  </style>
</head>
<body>
  <aside id="sidebar">
    <h3>Dependency graph</h3>
    <div id="summary" class="muted">Loading...</div>
    <label for="sheetFilter">Sheet filter</label>
    <select id="sheetFilter">
      <option value="__all__">All sheets</option>
    </select>
    <label for="roleFilter">Role filter</label>
    <select id="roleFilter">
      <option value="__all__">All roles</option>
      <option value="target">Targets</option>
      <option value="input">Inputs</option>
      <option value="constant">Constants</option>
      <option value="output">Outputs</option>
      <option value="internal">Internal</option>
    </select>
    <label for="searchBox">Search cells</label>
    <input id="searchBox" type="text" placeholder="address, formula..." />
    <button id="resetView">Reset view</button>
    <div class="legend muted">
      <div><span class="swatch" style="background:#FFB347"></span>Target</div>
      <div><span class="swatch" style="background:#6DA6FF"></span>Input</div>
      <div><span class="swatch" style="background:#6BCB77"></span>Constant</div>
      <div><span class="swatch" style="background:#B28DFF"></span>Output</div>
      <div><span class="swatch" style="background:#D9D9D9"></span>Internal</div>
    </div>
    <p class="muted">
      Layout comes from Graphviz (<code>dot -Tjson</code>) and renders with Cytoscape preset layout.
      Dashed edges are guarded dependencies.
    </p>
  </aside>
  <main id="cy"></main>

  <script src="https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js"></script>
  <script>
    async function init() {{
      const res = await fetch({json.dumps(json_filename)});
      if (!res.ok) {{
        throw new Error(`Failed to load {json_filename}: ${{res.status}}`);
      }}
      const graph = await res.json();
      const nodes = graph.elements.nodes || [];
      const edges = graph.elements.edges || [];

      const cy = cytoscape({{
        container: document.getElementById('cy'),
        elements: [...nodes, ...edges],
        style: [
          {{
            selector: 'node',
            style: {{
              'label': 'data(label)',
              'font-size': 10,
              'text-wrap': 'wrap',
              'text-max-width': 220,
              'text-valign': 'center',
              'text-halign': 'center',
              'shape': 'round-rectangle',
            }}
          }},
          {{
            selector: 'node[type = "cluster"]',
            style: {{
              'background-opacity': 0.06,
              'border-width': 1.4,
              'border-style': 'dashed',
              'border-color': '#666',
              'font-size': 12,
              'font-weight': 600,
              'text-valign': 'top',
              'text-halign': 'center',
              'text-wrap': 'wrap',
              'text-max-width': 260,
              'text-margin-y': -8,
              'padding': '14px',
            }}
          }},
          {{
            selector: 'node[type = "cell"]',
            style: {{
              'background-opacity': 1,
              'font-size': 9,
              'text-wrap': 'wrap',
              'text-max-width': (ele) => {{
                const w = ele.data('gv_width');
                if (w == null || Number.isNaN(Number(w))) return 220;
                return Math.max(40, Number(w) - 24);
              }},
              'width': (ele) => {{
                const w = ele.data('gv_width');
                return (w != null && !Number.isNaN(Number(w))) ? Number(w) : 'label';
              }},
              'height': (ele) => {{
                const h = ele.data('gv_height');
                return (h != null && !Number.isNaN(Number(h))) ? Number(h) : 'label';
              }},
              'padding': '10px',
            }}
          }},
          {{
            selector: 'node[role = "target"]',
            style: {{ 'background-color': '#FFB347' }}
          }},
          {{
            selector: 'node[role = "input"]',
            style: {{ 'background-color': '#6DA6FF' }}
          }},
          {{
            selector: 'node[role = "constant"]',
            style: {{ 'background-color': '#6BCB77' }}
          }},
          {{
            selector: 'node[role = "output"]',
            style: {{ 'background-color': '#B28DFF' }}
          }},
          {{
            selector: 'node[role = "internal"]',
            style: {{ 'background-color': '#D9D9D9' }}
          }},
          {{
            selector: 'node[is_leaf = true][role = "internal"]',
            style: {{ 'shape': 'round-rectangle' }}
          }},
          {{
            selector: 'node[is_leaf = false][role = "internal"]',
            style: {{ 'shape': 'ellipse' }}
          }},
          {{
            selector: 'edge',
            style: {{
              'curve-style': 'bezier',
              'target-arrow-shape': 'triangle',
              'width': 1.4,
              'line-color': '#666',
              'target-arrow-color': '#666',
              'label': 'data(label)',
              'font-size': 8,
              'text-background-opacity': 1,
              'text-background-color': '#fff',
              'text-background-padding': 1,
            }}
          }},
          {{
            selector: 'edge[guarded = true]',
            style: {{
              'line-style': 'dashed',
            }}
          }},
          {{
            selector: '.hidden',
            style: {{
              'display': 'none'
            }}
          }}
        ],
        layout: {{
          name: 'preset',
          fit: true,
          padding: 30
        }}
      }});

      const summary = document.getElementById('summary');
      summary.textContent =
        `${{graph.meta.node_count}} cells · ${{graph.meta.edge_count}} edges · ${{graph.meta.sheets.length}} sheets`;

      const sheetFilter = document.getElementById('sheetFilter');
      for (const sheet of graph.meta.sheets || []) {{
        const option = document.createElement('option');
        option.value = sheet;
        option.textContent = sheet;
        sheetFilter.appendChild(option);
      }}

      const applyFilters = () => {{
        const selectedSheet = sheetFilter.value;
        const selectedRole = document.getElementById('roleFilter').value;
        const query = document.getElementById('searchBox').value.trim().toLowerCase();
        cy.elements().removeClass('hidden');

        if (selectedSheet !== '__all__') {{
          cy.nodes('[type = "cell"]').forEach((node) => {{
            if (node.data('sheet') !== selectedSheet) {{
              node.addClass('hidden');
            }}
          }});
        }}

        if (selectedRole !== '__all__') {{
          cy.nodes('[type = "cell"]').forEach((node) => {{
            if (node.data('role') !== selectedRole) {{
              node.addClass('hidden');
            }}
          }});
        }}

        if (query) {{
          cy.nodes('[type = "cell"]').forEach((node) => {{
            const hay = `${{node.data('label')}} ${{node.data('formula') || ''}}`.toLowerCase();
            if (!hay.includes(query)) {{
              node.addClass('hidden');
            }}
          }});
        }}

        cy.edges().forEach((edge) => {{
          if (edge.source().hasClass('hidden') || edge.target().hasClass('hidden')) {{
            edge.addClass('hidden');
          }}
        }});
      }};

      sheetFilter.addEventListener('change', applyFilters);
      document.getElementById('roleFilter').addEventListener('change', applyFilters);
      document.getElementById('searchBox').addEventListener('input', applyFilters);
      document.getElementById('resetView').addEventListener('click', () => {{
        sheetFilter.value = '__all__';
        document.getElementById('roleFilter').value = '__all__';
        document.getElementById('searchBox').value = '';
        cy.elements().removeClass('hidden');
        cy.fit();
      }});
    }}

    init().catch((error) => {{
      document.getElementById('summary').textContent = String(error);
      console.error(error);
    }});
  </script>
</body>
</html>
"""


def series_cell_keys(series_items: Iterable[Mapping[str, Any]]) -> set[NodeKey]:
    keys: set[NodeKey] = set()
    for item in series_items:
        for cell in item.get("cells", []):
            address = cell.get("address")
            if isinstance(address, str):
                keys.add(address)
    return keys


def constant_keys_from_leaf_classification(
    leaf_classification: Mapping[str, str],
) -> set[NodeKey]:
    return {key for key, kind in leaf_classification.items() if kind == "constant"}


def write_dependency_graph_site(
    graph: DependencyGraph,
    output_dir: Path,
    *,
    target_keys: set[NodeKey] | None = None,
    input_keys: set[NodeKey] | None = None,
    output_keys: set[NodeKey] | None = None,
    constant_keys: set[NodeKey] | None = None,
    rankdir: str = "TB",
    dot_bin: str | None = None,
    json_filename: str = DEFAULT_JSON_FILENAME,
) -> dict[str, Any]:
    """Write Cytoscape preset JSON and HTML for a dependency graph."""
    dot_text = build_dot_with_sheet_clusters(graph, rankdir=rankdir)
    graphviz_json = parse_graphviz_json(dot_text, dot_bin=dot_bin)
    payload = build_cytoscape_preset_payload(
        graph,
        graphviz_json,
        target_keys=target_keys,
        input_keys=input_keys,
        output_keys=output_keys,
        constant_keys=constant_keys,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / json_filename
    html_path = output_dir / "index.html"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    html_path.write_text(
        build_index_html(json_filename=json_filename), encoding="utf-8"
    )
    return dict(payload["meta"])
