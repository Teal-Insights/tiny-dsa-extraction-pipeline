# Interactive dependency graph

Fullscreen Cytoscape explorer for the exported package. Values recompute via
**excel-grapher FormulaEvaluator** (and optionally the exported `Model`).

| File | Role |
|------|------|
| `index.html` | Shell UI; `?preview=1` hides chrome |
| `config.js` | Optional remote API base (empty = same-origin) |
| `app.js` | Cytoscape wiring; `GET /api/graph`, `POST /api/evaluate` |
| `bootstrap.json` | Optional static snapshot when the API is offline |
| `style.css` | Layout and role colors |
| `API.md` | Full HTTP contract the UI expects |

## Run locally

From the exported package root (`dist/`):

```bash
uv sync --group graph
uv run python scripts/serve_graph_api.py
# http://127.0.0.1:8765/
```

Author `{package}/graph_schema.py` (`NODES` / `EDGES`) before the viz is useful.
See `examples/reference_graph_schema.py` and `../README.md`.

```bash
uv run python scripts/write_graph_bootstrap.py
uv run python scripts/check_graph_eval.py
```
