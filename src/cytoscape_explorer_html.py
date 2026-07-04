"""HTML shell for the dependency graph Cytoscape explorer."""

from __future__ import annotations

import json
from typing import Literal

LayoutMode = Literal["graphviz_preset", "structure_only"]

_GRAPHVIZ_CELL_STYLE = """
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
          }},"""

_STRUCTURE_CELL_STYLE = """
          {{
            selector: 'node[type = "cell"]',
            style: {{
              'background-opacity': 1,
              'font-size': 9,
              'text-wrap': 'wrap',
              'text-max-width': 220,
              'width': 'label',
              'height': 'label',
              'padding': '10px',
            }}
          }},"""

_GRAPHVIZ_LAYOUT = """
        layout: {{
          name: 'preset',
          fit: true,
          padding: 30
        }}"""

_STRUCTURE_LAYOUT = """
        layout: {{
          name: 'cose',
          fit: true,
          padding: 30,
          animate: false,
        }}"""


def build_cytoscape_explorer_html(
    *,
    json_filename: str,
    layout_mode: LayoutMode,
) -> str:
    """Return the interactive Cytoscape explorer HTML for one layout mode."""
    is_preset = layout_mode == "graphviz_preset"
    title = (
        "Dependency Graph (Graphviz preset + Cytoscape)"
        if is_preset
        else "Dependency Graph (structure only)"
    )
    notice_html = (
        ""
        if is_preset
        else """
    <div class="notice">
      Graphviz layout skipped because this graph exceeds the configured node/edge
      thresholds. The explorer uses a client-side layout instead of Graphviz preset
      coordinates. See <code>graph-topology.json</code> and <code>dependencies.dot</code>.
    </div>"""
    )
    footer_note = (
        "Layout comes from Graphviz (<code>dot -Tjson</code>) and renders with "
        "Cytoscape preset layout. Dashed edges are guarded dependencies."
        if is_preset
        else "Structure-only mode clusters cells by worksheet and lays them out in "
        "the browser. Dashed edges are guarded dependencies."
    )
    cell_style_block = _GRAPHVIZ_CELL_STYLE if is_preset else _STRUCTURE_CELL_STYLE
    layout_block = _GRAPHVIZ_LAYOUT if is_preset else _STRUCTURE_LAYOUT

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
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
    .notice {{
      background: #fff3cd;
      color: #664d03;
      border: 1px solid #ffecb5;
      border-radius: 0.35rem;
      padding: 0.65rem;
      margin: 0.75rem 0;
      font-size: 0.85rem;
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
    <h3>Dependency graph</h3>{notice_html}
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
      {footer_note}
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
          }},{cell_style_block}
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
        ],{layout_block}
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
