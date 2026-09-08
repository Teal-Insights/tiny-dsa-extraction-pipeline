# Artifacts catalog

See [README.md](README.md) for git commit policy and stage timing. Generated pipeline artifacts live under `artifacts/`. Do not commit graph JSON/HTML snapshots.

## `dependency-graph/`

Written by the extract-only stage (`uv run python -m src.extraction_pipeline --extract-graph`).

| File | Description |
|---|---|
| `index.html` | Interactive Cytoscape explorer; Graphviz preset layout when enabled, otherwise structure-only client layout |
| `dependency-graph.json` | Cytoscape payload consumed by `index.html` |
| `dependencies.dot` | Graphviz DOT source for the dependency graph (always written) |
| `graph-topology.json` | Node/edge counts, per-sheet breakdown, and layout-mode metadata |
| `extraction-summary.json` | Machine-readable extraction metrics and output paths |

### Layout modes

Graphviz preset layout runs when `GRAPHVIZ_LAYOUT` allows it and node/edge counts are below limits (defaults: 10,000 nodes, 50,000 edges). Otherwise the explorer falls back to structure-only mode and uses client-side layout; `dependencies.dot` and `graph-topology.json` are still written for offline review.

| `graph-topology.json` field | Description |
|---|---|
| `layout_mode` | `graphviz_preset` or `structure_only` |
| `graphviz_layout_enabled` | Whether Graphviz layout ran for this extract |
| `dot_byte_size` | Size of `dependencies.dot` in bytes |
| `node_count`, `edge_count`, `sheets` | Topology metrics (see below) |

Environment overrides: `GRAPHVIZ_LAYOUT` (`auto`, `always`, `never`), `GRAPHVIZ_NODE_LIMIT`, `GRAPHVIZ_EDGE_LIMIT`.

### `graph-topology.json` topology fields

| Field | Type | Description |
|---|---|---|
| `node_count` | integer | Cells in the dependency graph |
| `edge_count` | integer | Directed dependency edges |
| `sheets` | object | Per-worksheet `node_count` and `edge_count` |

### `extraction-summary.json` schema (version `1.0.0`)

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Summary schema version (`1.0.0`) |
| `node_count` | integer | Cells in the dependency graph |
| `edge_count` | integer | Directed dependency edges |
| `leaf_count` | integer | Graph leaves (inputs/constants) |
| `provenance_edge_count` | integer | Edges with dependency provenance metadata |
| `elapsed_seconds` | number | Wall-clock seconds for graph build plus artifact write (covers all `stage_timings` stages) |
| `stage_timings` | object | Per-stage seconds keyed by stage name |
| `output_paths` | object | Repo-relative paths for every file listed above |

`output_paths` keys: `output_dir`, `index_html`, `dependency_graph_json`, `dependencies_dot`, `graph_topology_json`, `extraction_summary_json`.

Serve locally:

```bash
uv run python -m http.server 8000 --directory artifacts/dependency-graph
```

Open `http://localhost:8000/`.

## `stages/`

Written by each completed pipeline stage. Used by `--start-from-stage` / `--only-stage` to resume without re-running upstream work. Local/untracked (see `.gitignore`).

| File | Description |
|---|---|
| `extract.json` | Graph cache key and input fingerprints after extract |
| `export.json` | Graph / projection / series-derived / codegen keys after export |
| `annotate.json` | Codegen key after inverted-tree docstring overlay |
| `validate.json` | Keys carried forward after validate |
| `document.json` | Keys carried forward after document |

Each manifest records `cache_keys`, `upstream_keys`, and labeled `fingerprints` (workbook, bindings, constraints, modes, `excel-grapher` version). Loading a manifest recomputes fingerprints and aborts on drift.

## `stage-timings.json`

Written by every `run_pipeline` invocation (`uv run python -m src.extraction_pipeline`), rewritten after each stage completes so a run that dies mid-pipeline still records the stages that finished (including the stage that raised).

### Schema (version `1.0.0`)

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Timings schema version (`1.0.0`) |
| `total_seconds` | number | Sum of every recorded stage's wall clock |
| `stages` | array | One entry per stage reached, in run order |
| `stages[].name` | string | `extract`, `export`, `annotate`, `validate`, or `document` |
| `stages[].elapsed_seconds` | number | Stage wall clock |
| `stages[].spans` | object | Seconds keyed by leaf span name inside that stage |
| `caches` | object | One entry per on-disk cache; `null` fields mean the run never reached it |
| `caches.<name>.cache_hit` | boolean \| null | Whether the payload was loaded from `.cache/` |
| `caches.<name>.elapsed_seconds` | number \| null | Seconds spent resolving the cache (the `get_or_build_*` call) |
| `caches.<name>.cache_key` | string \| null | Content key the lookup resolved to |

`caches` keys: `dependency-graph`, `bindings-validation`, `series-resolution`, `series-derived`, `projection`, `codegen`.

Spans are non-overlapping leaf measurements: do not invent a total by summing them with a parent rollup. A full `run_pipeline` records `extract`, `export`, `annotate`, `validate`, and `document` in order. Graph-build spans (`create_dependency_graph`, …) land under `extract`; binding post-processing (`load_series_bindings`, `validate_series_bindings`, `derive_series`, `series_derived`) and projection/codegen spans land under `export`; `annotate_docstrings` lands under `annotate`. Extract alone appears when `stop_after_stage=extract` (or `--extract-graph`).

Notable spans: `create_dependency_graph` (extract); `load_series_bindings`, `validate_series_bindings`, `derive_series`, `series_derived`, `codegen`, `write_export_package` (export); `annotate_docstrings` (annotate); `exported_library_differential` (validate).

### cProfile output

Set `PIPELINE_PROFILE=1` to additionally write `<stage>.prof` and `<stage>.pstats.txt` under `artifacts/dependency-graph/` for each of `extract`, `export`, `annotate`, `validate`, and `document`.

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
