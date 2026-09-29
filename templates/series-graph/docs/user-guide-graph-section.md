# Interactive dependency graph (landing-page fragment)

Add a short **Interactive dependency graph** section on `user_guide/index.qmd`
(or the site landing page). Do **not** write a long topology blurb under the
heading — the viz is the explanation.

## Styling

Style the embed with a small `include-in-header` block using a
`series-graph-*` class prefix.

## HTML embed

The docs deploy workflow copies `assets/graph/` to the site root, so embed it
with absolute URLs built from the package's `documentation_url`:

- iframe `src="{documentation_url}assets/graph/index.html?preview=1"` (docs paint)
- fullscreen link `{documentation_url}assets/graph/index.html`

Do not use relative `../assets/graph/` paths; they break for pages under
`/user-guide/`.

## Local FormulaEvaluator API

From the exported `dist/` project:

```bash
uv sync --group graph
uv run python scripts/serve_graph_api.py
# http://127.0.0.1:8765/
```

Author `{package}/graph_schema.py` (`NODES` / `EDGES`) before the viz is useful.
Reference: `examples/reference_graph_schema.py` and
`templates/series-graph/README.md` in this pipeline repo.
