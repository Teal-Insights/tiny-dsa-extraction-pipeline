# Artifacts catalog

See [README.md](README.md) for git commit policy and stage timing. Generated pipeline artifacts live under `artifacts/`. Do not commit graph JSON/HTML snapshots.

## `dependency-graph/`

Written by the extract-only stage (`uv run python -m src.extraction_pipeline --extract-graph`).

| File | Description |
|---|---|
| `index.html` | Interactive Cytoscape explorer (Graphviz preset layout) |
| `dependency-graph.json` | Cytoscape preset payload for the explorer |
| `extraction-summary.json` | Machine-readable extraction metrics and output paths |

### `extraction-summary.json` schema (version `1.0.0`)

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Summary schema version (`1.0.0`) |
| `node_count` | integer | Cells in the dependency graph |
| `edge_count` | integer | Directed dependency edges |
| `leaf_count` | integer | Graph leaves (inputs/constants) |
| `provenance_edge_count` | integer | Edges with dependency provenance metadata |
| `elapsed_seconds` | number | Wall-clock seconds for the extract stage |
| `stage_timings` | object | Per-stage seconds keyed by stage name |
| `output_paths` | object | Repo-relative paths for `output_dir`, `index_html`, `dependency_graph_json`, and `extraction_summary_json` |

Serve locally:

```bash
uv run python -m http.server 8000 --directory artifacts/dependency-graph
```

Open `http://localhost:8000/`.

## `workbook-audit.md`

Written by the pre-extraction audit CLI (`uv run python -m src.workbook_audit`).

| Section | Description |
|---|---|
| Executive summary | Proceed / do-not-proceed recommendation and blocking automation |
| Quantitative surface | Cell totals, per-sheet formula counts, function and dynamic-ref call sites |
| Automation and external dependencies | VBA, macro sheets, connections, external links, live `[book]` formula refs |
| Named ranges | Defined names with external/broken flags and formula usage counts |
| Public input inventory | Optional; rendered when `AUDIT_PUBLIC_INPUTS` is populated in `workbook_config.py` |
| Guide-described workflows | Optional; rendered when `AUDIT_GUIDE_USE_CASES` is populated |
| Charts and drawings | Embedded chart and drawing part counts |

Default output path: `artifacts/workbook-audit.md`.
