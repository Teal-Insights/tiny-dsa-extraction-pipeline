# Extraction Pipeline Template

Cookie-cutter template for turning an Excel financial model into a semantic, distributable Python library using [excel-grapher](https://github.com/Teal-Insights/excel-grapher). The pipeline combines target-driven graph extraction, explicit dynamic-reference constraints, series bindings, LLM-assisted documentation, Excel-backed **graph-oracle** parity, and FormulaEvaluator **library** parity.

See [technical_standard.md](technical_standard.md) for the acceptance bar and [lessons-learned.md](lessons-learned.md) for design rationale.

## Clone and configure (onboarding checklist)

Follow this order when adapting the template to a new workbook. Each step has a stage-gate owner who signs off before the next step begins.

| Step | Owner | Action |
|---|---|---|
| 1. Ingest | **Config author** | Clone the repo. This repo uses `data/tiny-dsa.xlsx` and `data/tiny-dsa-guide.md`. When adapting to a new workbook, replace those files. Clear workbook-specific state: reset `bindings/*.bindings.yaml` to empty `series: []` placeholders (or delete them), and delete `dist/` and `.cache/`. Point [workbook_config.py](workbook_config.py) paths at the new workbook. |
| 2. Audit | **Config author** | Run `uv run python -m src.workbook_audit --output artifacts/workbook-audit.md`. Resolve blocking automation (VBA, macros, external links) before graph work. |
| 3. Configure | **Config author** | Declare extraction targets, `BLANK_RANGES`, and dynamic-ref constraints (empty `series: []` binding placeholders are fine). See [Configure](#1-configure) below. |
| 4. Extract | **Config author** | Run `uv run python -m src.extraction_pipeline --extract-graph`. Confirm the graph builds without `DynamicRefError` (bindings are not required yet). |
| 5. Review graph | **Graph reviewer** | Inspect `artifacts/dependency-graph/` (see [artifacts/README.md](artifacts/README.md)). Confirm expected sheets, no spurious nodes, and complete shock/engine paths. Optionally run opt-in LLM dependency audits: `uv run pytest tests/test_extraction_graph_accuracy.py --run-skipped` (workbook audits auto-select parents from the warm committed `.cache/dependency-graph/` entry — run `--only-stage extract` or `scripts.regenerate_graph_cache` first; `GRAPH_AUDIT_CASES` is optional steering only. The synthetic smoke-test audit runs without extra configuration). Set the provider API key for `LLM_GRAPH_AUDIT_MODEL` (defaults to `gpt-5.5`). |
| 6. Verify graph | **Parity owner** | Define a scenario matrix in `tests/differential/` and run graph-oracle differential parity before export (see [Verify graph](#3-verify-graph)). Prefer a warm `.cache/dependency-graph/` from extract first. Do not proceed to export until graph-oracle parity passes. |
| 7. Export | **Parity owner** | Run export (`uv run python -m src.extraction_pipeline --stop-after-stage export` or the full pipeline). Codegen emits keyword-only `compute_*`. See [Export](#4-export). |
| 8. Validate and annotate | **Parity owner** | Validate compares `compute_*` to `FormulaEvaluator` on the graph. Annotate splices LLM docstrings onto `api.py` / `internals.py` after parity (see [Validate](#5-validate) and [Annotate](#6-annotate)). |
| 9. Document | **Config author** | Generate economist-facing docs from the exported package (see [Document](#7-document)). |

Copy the checkbox list in [Checklist for a new workbook](#checklist-for-a-new-workbook) into your extraction tracking issue and check items off as you go.

## What you provide

Before running the pipeline, populate this repository with workbook-specific inputs:

| Input | Location | Purpose |
|---|---|---|
| Workbook | `data/tiny-dsa.xlsx` | Source Excel model |
| Human guide | `data/tiny-dsa-guide.md` | Domain usage, public I/O catalog, scenario narrative |
| Targets | `workbook_config.py` → `TARGETS` | Named ranges or addresses driving graph extraction |
| Blank ranges | `workbook_config.py` → `BLANK_RANGES` | Sheet-qualified A1 rectangles of structurally empty cells omitted from the graph (same strings to graph build, `FormulaEvaluator`, and codegen) |
| Constraints | `workbook_config.py` → `CONSTRAINTS` | Dynamic-ref resolution and leaf input/constant classification |
| Series bindings | `bindings/inputs.bindings.yaml`, `bindings/outputs.bindings.yaml`, `bindings/internals.bindings.yaml`, `bindings/constants.bindings.yaml` | Keyword-only `compute_*` public API, internal formula-cell triangulation, and reader-only constant leaves |
| Package metadata | `workbook_config.py` → `DIST_METADATA` | Generated `dist/` project name, docs URLs, README |
| Internal binding exemptions | `workbook_config.py` → `INTERNAL_BINDING_EXEMPT_CELLS` | Reviewed formula cells allowed to remain unbound |
| Graph-cache target bundles | `workbook_config.py` → `GRAPH_CACHE_TARGET_BUNDLES` | Optional extra target sets for `scripts/regenerate_graph_cache.py` |
| Scenario matrix | `tests/differential/*_scenario_matrix.py` (or hooks in `differential_test_graph.py`) | Representative input combinations for differential parity sweeps |
| Graph parity evidence | `data/differential/graph/` | Reference reports after passing pre-export graph-oracle sweeps (optional until configured) |
| Exported-library parity evidence | `data/differential/exported_library/` | Reference reports after passing post-export parity sweeps (optional until configured) |

Use [.agents/skills/author-bindings](.agents/skills/author-bindings/SKILL.md) with a coding agent to draft bindings from the guide, workbook, and extracted graph.

### Iterative configuration

The linear `configure → extract` summary is a stage gate, not a one-shot workflow. Expect several passes:

1. **Draft** `TARGETS`, `BLANK_RANGES`, and dynamic-ref `CONSTRAINTS`. Keep `bindings/*.bindings.yaml` as empty `series: []` placeholders (or draft shards) — extract does not load or merge series bindings.
2. **Extract** with `--extract-graph` and review `artifacts/dependency-graph/`.
3. **Declare structural blanks**, then **constrain** any remaining unconstrained graph leaves (for input/constant classification), then author / refine bindings using what the graph reveals.
4. **Re-extract** as needed for graph review; run export (or `build_pipeline_graph`) only once bindings are mergeable and complete.

Extract is graph-first. Binding load, validation, derivation, and internal-coverage enforcement run at export (and any other bindings-ready path). Empty placeholder shards are expected during bootstrap.

## Pipeline stages

The orchestrator is `extract → export → validate → annotate → document` (`PIPELINE_STAGES` in [src/extraction_pipeline.py](src/extraction_pipeline.py)). Configure and graph-vs-Excel review are human gates around that sequence:

```mermaid
flowchart LR
  configure[Configure] --> extract[Extract]
  extract --> verifyGraph[Verify graph]
  verifyGraph --> export[Export]
  export --> validate[Validate]
  validate --> annotate[Annotate]
  annotate --> document[Document]
```

### 1. Configure

1. Edit [workbook_config.py](workbook_config.py): paths, `TARGETS`, `BLANK_RANGES`, `CONSTRAINTS`, and `DIST_METADATA`. Keep `bindings/*.bindings.yaml` as empty `series: []` placeholders until after the first extract if needed.
2. **Dynamic-ref pass** — list leaves that need domains to resolve `OFFSET` / `INDIRECT` / `INDEX` (`excel_grapher.list_dynamic_ref_constraint_candidates` against `TARGETS`), constrain them, and iterate until `--extract-graph` succeeds without `DynamicRefError`. That lister only covers dynamic-ref argument leaves; it will not enumerate every leaf that later appears in the finished graph.
3. **Leaf-classification pass** — after a successful extract, review empty leaves that sit in the graph only because a formula rectangle names them (`INDEX`/`MATCH` padding, far-right `NPV`/`SUM` year-window overflow, unused ladder copies, separator rows). Declare those sheet-qualified A1 rectangles in `BLANK_RANGES` and re-extract: BFS does not create the nodes, edges that name them are kept, and OFFSET/INDEX leaves inside the rects are **not** required in `CONSTRAINTS`. Pass the **same** sequence to graph build, `FormulaEvaluator`, and codegen (the pipeline does this from `workbook_config.BLANK_RANGES`). Do **not** bind these as inputs or constants, do **not** drop them by narrowing a year domain (`C18`-style — the ranges are literal `:BB` / `:BD`), and do **not** put user-fillable yellow slots in `BLANK_RANGES`. `Literal[None]` freezes dynamic-ref *classification*; it does not omit nodes from the graph. Then constrain every remaining unconstrained graph leaf (`graph.leaf_keys()` minus `CONSTRAINTS`) so each leaf classifies as `input` or `constant`. Every mutable input leaf must appear in `inputs.bindings.yaml`. Fixed leaves that formulas should read via `read_*` (not `xl_cell`) need a `constant: {}` series in `constants.bindings.yaml` (see [Authoring constants](#authoring-constants)).
4. Author I/O `bindings/*.bindings.yaml` (schema version `1.19.0`, one logical series per public API function or input/constant group). Empty `series: []` placeholders load (excel-grapher 5.1.4+); author real series before export.
5. Bind every internal formula cell in `internals.bindings.yaml` (see [Authoring internals](#authoring-internals) below).

Validation checks:

- `validate_series_bindings(...)` reports `ok`
- `derive_input_series` / `derive_output_series` / `derive_internal_series` / `derive_constant_series` resolve every binding
- No unbound mutable input leaves
- No unbound constant leaves
- Run the pre-extraction workbook audit and review blocking automation before graph work:

```bash
uv run python -m src.workbook_audit --output artifacts/workbook-audit.md
```

See [artifacts/README.md](artifacts/README.md) and [artifacts/artifacts-catalog.md](artifacts/artifacts-catalog.md) for report sections and commit policy. Optional hooks in [workbook_config.py](workbook_config.py) (`AUDIT_TITLE`, `AUDIT_PUBLIC_INPUTS`, `AUDIT_GUIDE_USE_CASES`) add workbook-specific inventory tables when populated.

### 2. Extract

Build the dependency graph with provenance enabled and write review artifacts before export:

```bash
uv run python -m src.extraction_pipeline --stop-after-stage extract
# equivalent: --extract-graph
```

This writes `artifacts/dependency-graph/` (see [artifacts/artifacts-catalog.md](artifacts/artifacts-catalog.md)), then exits. Review graph completeness manually (step 5 in the [onboarding checklist](#clone-and-configure-onboarding-checklist)): expected sheets, no spurious nodes, shock/engine paths present. See [artifacts/README.md](artifacts/README.md) for commit policy and [artifacts/artifacts-catalog.md](artifacts/artifacts-catalog.md) for the summary schema.

You can also build the graph programmatically:

```python
from excel_grapher.grapher import DynamicRefConfig, create_dependency_graph

graph = create_dependency_graph(
    workbook_path,
    targets,
    load_values=True,
    dynamic_refs=DynamicRefConfig.from_constraints(constraints, {}),
    capture_dependency_provenance=True,
    blank_ranges=blank_ranges,
)
```

After manual review (onboarding step 5), run graph-oracle differential parity (step 6) before export.

`--extract-graph` builds the dependency graph and writes review artifacts only. **Internal binding derivation** and optional **internal binding coverage validation** run later during export (and any other call that uses `build_pipeline_graph` / `resolve_pipeline_bindings`), once series bindings are mergeable.

#### Internal series bindings

After bindings are validated (export / bindings-ready path), the pipeline derives **internal series** for formula cells declared with `internal: {}` in `bindings/internals.bindings.yaml`. Each resolved cell carries `{address, key, record}` triangulation data used by the graph explorer. Effective dimension ids live in binding manifests and derived series records, not on graph node metadata.

#### Authoring internals

Author `internals.bindings.yaml` after `--extract-graph`, when you can see which formula cells still need triangulation. Coverage requires **every formula node** not already bound in `inputs.bindings.yaml` or `outputs.bindings.yaml`.

- **One series per logical group** — a single lookup/anchor cell or one formula row/range (e.g. `Engine!C10:G10`), not one entry per cell.
- **Same YAML shape as public bindings** — use `internal: {}` instead of `input` / `output`. Scalar examples are in [tests/fixtures/synthetic/internals.bindings.yaml](tests/fixtures/synthetic/internals.bindings.yaml).
- **Scalars vs row series** — lookup and anchor formulas usually use `layout: scalar` with `key: []`. Parallel time-series rows use `layout: row_series` with a key dimension (often concept `TIME_PERIOD`) bound from the **header row that labels that row**; different tables often use different header rows.
- **Give every dimension an explicit `id`** — `id` names the dimension itself and is the effective key used in records, cell keys, graph labels, and helper parameters. `concept` is the SDMX-style meaning category. They match unless two dimensions in one series share a concept (e.g. projection axis and reference period both on `TIME_PERIOD`), in which case give each a distinct id such as `PROJECTION_PERIOD` / `REFERENCE_PERIOD`. Parameter names come from the effective id (`projection_period`). Concept-only keys remain valid only when the concept uniquely identifies one dimension.
- **Reuse public concepts** — prefer concept IDs already in your bindings / `concept_scheme` (`TIME_PERIOD`, `INDICATOR`, `PARAMETER`, etc.) so helper names stay meaningful even when dimension ids differ.
- **Validate** — `validate_series_bindings(...)`, then `derive_internal_series(...)`. Run `uv run pytest tests/test_internal_binding_coverage.py` once `INTERNAL_BINDING_VALIDATION_MODE` is enabled.
- **Review** — re-run `--extract-graph` and confirm bound formula nodes show `keys:` / `record:` labels in the graph explorer.

Schema details and field shapes: excel-grapher `user_guide/05-series-bindings.qmd` (internal direction, schema 1.19.0). Use [.agents/skills/author-bindings](.agents/skills/author-bindings/SKILL.md) for agent-assisted drafting.

#### Authoring constants

Author `constants.bindings.yaml` for graph **leaves** that formulas depend on but that are not user-editable public inputs. Classification via `Literal[...]` in `CONSTRAINTS` marks those leaves as `constant` for codegen `CONSTANTS`; a `constant: {}` binding additionally emits a semantic `read_*` and rewrites formula bodies off bare `xl_cell`.

Structural blanks are a different lever. Cells that formulas *name* but users never fill belong in `BLANK_RANGES`, not `CONSTRAINTS` + `constants.bindings.yaml`. If those nodes are deleted without `blank_ranges`, `FormulaEvaluator.evaluate()` raises `KeyError` (`Cell … not found in graph`); a still-present empty leaf returns `None`. The supported way to drop the nodes without breaking eval/export is the same sheet-qualified A1 sequence passed to `create_dependency_graph`, `FormulaEvaluator`, and `CodeGenerator.generate` / `generate_modules`.

- **Same YAML shape as public bindings** — use `constant: {}` instead of `input` / `output` / `internal`. Mutually exclusive with those directions.
- **Leaf-only** — `data_range` must cover graph leaves (not formula nodes). Formula triangulation stays in `internals.bindings.yaml`.
- **No public write surface** — do not declare `input: {}` on constant series. If downstream users must edit the cell, use an `input` binding instead.
- **Validate** — `validate_series_bindings(...)`, then `derive_constant_series(...)`. Run `uv run python -m scripts.binding_resolution_audit` (includes the `constant` direction by default).

Synthetic example: [tests/fixtures/synthetic/constants.bindings.yaml](tests/fixtures/synthetic/constants.bindings.yaml). Full rules: [bindings/README.md](bindings/README.md#constant-bindings-reader-only-leaves). Schema reference: excel-grapher `user_guide/05-series-bindings.qmd` (constant direction, schema 1.11.0+).

#### Internal binding coverage validation

Configure validation in [workbook_config.py](workbook_config.py):

- `INTERNAL_BINDING_VALIDATION_MODE` — `off`, `warn` (default), or `error`
- `INTERNAL_BINDING_EXEMPT_CELLS` — sheet-qualified formula addresses reviewed and intentionally allowed to remain unbound

Coverage applies to **formula nodes** that are not already covered by public input or output bindings.

| Mode | Pipeline | Pytest (`tests/test_internal_binding_coverage.py`) |
|---|---|---|
| `off` | Skipped | Skipped |
| `warn` | Logs warnings for unbound required formula cells | Fails the test suite |
| `error` | Raises before export | Fails the test suite |

Use `warn` while iterating locally; treat pytest failures as the CI gate once exemptions are committed. Add reviewed formula cells to `INTERNAL_BINDING_EXEMPT_CELLS` rather than weakening the mode.

#### Binding and graph-cache utilities

| Utility | Command | When to use |
|---|---|---|
| Graph-cache regeneration | `uv run python -m scripts.regenerate_graph_cache` | After changing the workbook, bindings, targets/constraints/`BLANK_RANGES`, or excel-grapher. Add `--force` to rebuild even when entries exist; `--force` also clears and prunes `.cache/series-resolution/`, `.cache/series-derived/`, and `.cache/bindings-validation/`. Optional extra bundles: `GRAPH_CACHE_TARGET_BUNDLES` in `workbook_config.py`. |
| Internal-binding burndown | `uv run python -m scripts.internal_binding_burndown` | After `--extract-graph` to see which formula rows still need `internals.bindings.yaml` entries. Supports `--per-sheet` and `--max-rows`. Reuses the newest cached graph even when bindings changed. |
| Startup-site I/O catalog | `uv run python -m scripts.i_o_tables` | After extract (warm graph cache) to write `artifacts/startup-site/`: public-input and output CSV catalogs, HTML pages, a workbook download, and excel-grapher's statement graph. Override the destination with `--output-dir`. Serve with `uv run python -m http.server 8000 --directory artifacts/startup-site` (POSIX `/` in `--directory` so Git Bash does not treat `\t` as a tab). |
| Author-bindings skill | `.agents/skills/author-bindings` | Agent-assisted series-binding authoring. Pedagogical shapes: `.agents/skills/author-bindings/assets/`. Do not emit four sidecars from a generic catalog. Keep pipeline-wired `scripts.binding_resolution_audit` and `scripts.internal_binding_burndown` (cached `TARGETS` graph); they are not replaced by `excel-grapher bindings audit` / `burndown`. |

Commit `.cache/dependency-graph/` only when your downstream pipeline vendors the cache for warm CI (override `.gitignore` for that directory). Run `uv run pytest tests/test_binding_utility_scripts.py` to exercise the synthetic fixture path end-to-end.

### 3. Verify graph

**Why:** Graph-oracle parity isolates extraction, configuration, and dynamic-ref resolution bugs from export and codegen bugs. When export happens first, exported-library differential failures are ambiguous — they may come from the graph, the bindings, or the generated package.

**What:** A scenario matrix (canonical baselines, single-axis shocks, categorical factorials, and boundary cases) exercised by [`tests/differential/differential_test_graph.py`](tests/differential/differential_test_graph.py). The harness compares Microsoft Excel (golden master via `xlwings`) against the in-memory dependency graph evaluated with `FormulaEvaluator.evaluate`.

**How:**

Prefer a warm `.cache/dependency-graph/` entry from extract (or
`scripts.regenerate_graph_cache`) so the harness loads the graph via the same
cache helpers as the pipeline instead of cold-building:

```bash
uv run python -m src.extraction_pipeline --only-stage extract
uv run python -m tests.differential.differential_test_graph
```

Reports land under `data/differential/graph/`. Exit codes: **`0`** all comparisons pass, **`1`** any failure, **`2`** prerequisite missing or scenarios not configured. See [tests/differential/README.md](tests/differential/README.md) for harness hooks, warm-cache policy, address-key normalization, and workbook-exact label resolution.

**Gate:** Do not run the full pipeline until graph-oracle parity passes.

### 4. Export

Export calls `CodeGenerator.generate_modules(...)`.
The package is `compute_*`
functions that take a typed `{Output}Inputs` bundle
(`from_defaults` fills `data.*_DEFAULT` with keyword leaf overrides).
Scalars stay scalars; series are named-axis tensors in canonical key
order. There is no `make_context`,
no `set_*`, and no records-shaped setters. Helpers are named from output
`series_id` / `output.compute.name`. Package shape: `api.py`, `internals.py`,
`runtime.py`, `data.py`, `__init__.py`. The validation bundle is copied into
`dist/tests/`. Docstrings are placeholders until [Annotate](#6-annotate).

### 5. Validate

Two oracles, two questions:

| Question | Oracle | SUT |
|---|---|---|
| Did we extract the workbook faithfully? | Excel (`xlwings`) | `FormulaEvaluator` on the graph ([Verify graph](#3-verify-graph)) |
| Did we code-generate that graph faithfully? | Graph (`FormulaEvaluator`) | keyword-only `compute_*` |

The pipeline `validate` stage runs the authored exported-library FormulaEvaluator
sweep (`tests.differential.differential_test_exported_library` via
`src.differential_validation.run_post_refactor_differential`). It compares
keyword-only `compute_*` to `FormulaEvaluator` on the extraction graph across
`build_scenarios()`. Empty `build_scenarios()` / `output_cell_labels()` fail
closed — fill those hooks in a derived repo before claiming library ≈ graph.
Microsoft Excel is not required.

Reports land under `data/differential/exported_library/`. When present, validate
copies them into `dist/tests/results/reference/`. It does not overwrite Excel
goldens under `data/differential/graph/`.

```bash
uv run python -m tests.differential.differential_test_exported_library
```

### 6. Annotate

[src/inverted_tree_docstrings.py](src/inverted_tree_docstrings.py) asks
`DOCSTRING_MODEL` for Google-style docstrings keyed by function signature and
bindings notes, then splices them onto `api.py` and `internals.py`. Successful
responses cache under `.cache/inverted-tree-docstrings.json`. The stage fails
closed if the model returns argument names that do not match the signature.
A full pipeline run skips annotate (and document) when validate exits non-zero,
unless you pass `--force-document`.

```bash
uv run python -m src.extraction_pipeline --start-from-stage annotate --stop-after-stage annotate
# equivalent: --only-stage annotate
```

Entering at annotate requires a warm `artifacts/stages/validate.json`.

### 7. Document

Great Docs generates the distributable website from the exported package. LLM
rewrites guide sections into Python-first user-guide pages when cache misses
require an API key. Runnable `{python}` cells must not call `make_context()` or
`set_*` (`RUNNABLE_CELL_RULES` in [workbook_config.py](workbook_config.py)).
[templates/canonical-api-usage.md](templates/canonical-api-usage.md) is the
canonical interaction model for those rewrites.

## Run the pipeline

After graph-oracle (Excel vs graph) parity passes (see [Verify graph](#3-verify-graph)), run the full pipeline:

```bash
uv sync
uv run python -m src.extraction_pipeline
```

### Stage entry and exit

The pipeline is ordered as `extract → export → validate → annotate → document`. Each completed stage writes `artifacts/stages/<stage>.json` (cache keys, upstream keys, and input fingerprints). Use the flags below to enter or exit at a named stage without re-paying upstream work:

| Flag | Behavior | Typical use |
|---|---|---|
| `--stop-after-stage extract` (or `--extract-graph`) | Run extract only (graph review artifacts) | Bindings / constraint iteration |
| `--stop-after-stage export` | Run through export | Inspect generated `compute_*` before the library sweep |
| `--stop-after-stage validate` | Run through the exported-library FormulaEvaluator sweep | Inspect parity before paying for docstrings |
| `--start-from-stage annotate` | Resume at annotate from warm `validate.json` | Re-run LLM docstrings after parity passes |
| `--only-stage validate` | Run validate only from warm `export.json` (no cached docstring overlay) | Exported-library FormulaEvaluator sweep without annotate |
| `--only-stage document` | Run document only | Guide rewrite against an existing package |
| `--stop-after-stage document` (default) | Full pipeline from the start | Release / complete run |
| `--force-rebuild` | Rebuild warm on-disk caches even when keys match | Invalidate stale cache payloads |

`--start-from-stage` and `--only-stage` are mutually exclusive. `--only-stage` cannot be combined with `--stop-after-stage`. Loading a stage manifest recomputes workbook / bindings / constraints / mode fingerprints and **fails loudly** (naming the drifted input) when they disagree — it never silently falls back to a full run. `--only-stage validate` / `--start-from-stage validate` rebuild `dist/` via `materialize_package` from the export codegen key and do **not** re-apply cached annotate docstrings. Entering at `annotate` requires `artifacts/stages/validate.json`. Entering at `document` rematerializes from the annotate codegen key and then re-applies cached annotate docstrings.

When the default full run reaches `annotate` after a non-zero exported-library differential exit, annotate and document are skipped so parity diagnosis is not gated on LLM docstring spend or guide rewrite. Pass `--force-document` to splice docstrings and rewrite guides anyway. Harness exceptions abort the pipeline (fail closed). Document-stage failures (timeouts, validation exhaustion, LLM errors) raise loudly after logging that export/differential artifacts under `dist/` are preserved.

The document stage launches a Cursor SDK agent against `dist/` (`CURSOR_API_KEY`, `DOCUMENT_AGENT_MODEL`, default deadline 1800s via `DOCUMENT_AGENT_DEADLINE`). Authored trees cache under `.cache/user-guide/`. Set `PIPELINE_STALL_SECONDS` for heartbeat stack dumps during the document stage.

```bash
uv run python -m src.extraction_pipeline --stop-after-stage export
uv run python -m src.extraction_pipeline --start-from-stage validate --stop-after-stage annotate
uv run python -m src.extraction_pipeline --only-stage validate
```

### Prerequisites

LLM steps (annotate docstrings, document-stage Cursor agent) cache results under `.cache/`. Dependency graph extraction caches under `.cache/dependency-graph/` as excel-grapher EGDG multipart payloads, keyed by workbook bytes, targets, constraints, load/provenance flags, and `excel-grapher` version — **not** bindings. `derive_*_series` resolution, `validate_series_bindings`, derived leaf/binding objects, projection, and codegen module texts cache gzipped pickle payloads under `.cache/series-resolution/`, `.cache/bindings-validation/`, `.cache/series-derived/`, `.cache/projection/`, and `.cache/codegen/`. Series-resolution, series-derived, and bindings-validation keys fold a `bindings_fingerprint`; projection keys fold the graph cache key, preserve-scope flag, strategy, and `excel-grapher` version; codegen keys fold `paradigm="inverted_tree"`. Annotate caches LLM docstrings in `.cache/inverted-tree-docstrings.json`. Authored user-guide trees cache under `.cache/user-guide/`. `dist/` is a disposable projection of those caches: `materialize_package` rebuilds it from the codegen cache key recorded in `dist/.pipeline-cache-keys.json`, then annotate re-applies cached docstrings. Stage entry/exit records keys plus fingerprints under `artifacts/stages/*.json`. Pass `--no-cache` to bypass graph, projection, series-resolution, series-derived, bindings-validation, codegen, and annotate caches for a single run; pass `--force-rebuild` to rewrite warm cache entries. A clean run reproduces committed output without an API key unless inputs change. For uncached steps, set provider API keys and per-stage model names in a `.env` file at the repository root:

```bash
# .env — logging verbosity for pipeline entry points (default: INFO)
LOG_LEVEL=INFO

# .env — provider API keys (set the key for whichever model family you use)
OPENAI_API_KEY=sk-...
ZAI_API_KEY=...
DEEPSEEK_API_KEY=...

# Per-stage model selection (optional; default gpt-5.5 when unset)
# Name prefix selects the provider: gpt-*, glm-*, deepseek-*
CURSOR_API_KEY=...
DOCSTRING_MODEL=gpt-5.5
DOCUMENT_AGENT_MODEL=gpt-5.6-luna
LLM_GRAPH_AUDIT_MODEL=gpt-5.5
```

If an uncached LLM step is reached without the required API key, the pipeline fails fast with an `*_API_KEY is required ...` error.

Opt-in graph dependency audits (`pytest --run-skipped`) use the same multi-provider routing: set `LLM_GRAPH_AUDIT_MODEL` to a `gpt-*`, `glm-*`, or `deepseek-*` model name and provide the matching API key (`OPENAI_API_KEY`, `ZAI_API_KEY`, or `DEEPSEEK_API_KEY`). One model drives every audit case in a run. Workbook audits auto-select difficulty-ranked formula parents from the warm committed `.cache/dependency-graph/` cache (they never cold-build under pytest's redirected cache — miss skips with a hint to run `--only-stage extract`). Optional `workbook_config.GRAPH_AUDIT_CASES` entries steer selection (`required` pins and labeled focuses); the synthetic smoke-test audit uses the fixture catalog.

DeepSeek runs with thinking mode disabled by default. To enable it (and pass reasoning effort through, which DeepSeek only honors in thinking mode), set `DEEPSEEK_THINKING` to a truthy value (`1`, `true`, `yes`, or `on`):

```bash
DEEPSEEK_THINKING=1
```

## Graph exploration

The `--extract-graph` stage writes an interactive Cytoscape site under `artifacts/dependency-graph/`. To regenerate it manually from an in-memory graph:

```python
from pathlib import Path

from src.dependency_graph_viz import (
    constant_keys_from_leaf_classification,
    series_cell_keys,
    write_dependency_graph_site,
)
from src.internal_bindings import binding_node_labels, build_internal_binding_index

output_dir = Path("artifacts/dependency-graph")
internal_binding_index = build_internal_binding_index(internal_series)
write_dependency_graph_site(
    graph,
    output_dir,
    node_labels=binding_node_labels(graph, internal_binding_index),
    input_keys=series_cell_keys(input_series),
    output_keys=series_cell_keys(output_series),
    internal_binding_index=internal_binding_index,
    constant_keys=constant_keys_from_leaf_classification(leaf_classification),
)
```

Serve locally (do not commit generated JSON/HTML):

```bash
uv run python -m http.server 8000 --directory artifacts/dependency-graph
```

Open `http://localhost:8000/`.

## Checklist for a new workbook

Ordered to match the [onboarding checklist](#clone-and-configure-onboarding-checklist):

- [ ] **Ingest:** `data/tiny-dsa.xlsx` and `data/tiny-dsa-guide.md` populated; bindings reset to empty placeholders (or removed); `dist/` and `.cache/` cleared
- [ ] **Audit:** Pre-extraction workbook audit reviewed (`uv run python -m src.workbook_audit`); blocking automation resolved
- [ ] **Configure:** Outputs declared as extraction targets in `workbook_config.py`
- [ ] **Configure:** Dynamic-ref constraint candidates constrained (`list_dynamic_ref_constraint_candidates`; graph builds without `DynamicRefError`)
- [ ] **Extract:** Graph extracts with provenance (`--extract-graph`; empty `series: []` placeholders OK)
- [ ] **Configure:** Empty leaves reviewed; structural blank rectangles declared in `BLANK_RANGES` rather than constraining each cell `Literal[None]`
- [ ] **Configure:** Remaining graph leaves constrained and classified; mutable leaves bound
- [ ] **Configure:** `bindings/inputs.bindings.yaml` + `outputs.bindings.yaml` authored and validated
- [ ] **Configure:** Every `constant` leaf bound in `bindings/constants.bindings.yaml` (`constant: {}`); every mutable `input` leaf bound in `inputs.bindings.yaml`
- [ ] **Configure:** Internal binding exemptions reviewed (`INTERNAL_BINDING_EXEMPT_CELLS`)
- [ ] **Configure:** `bindings/internals.bindings.yaml` covers internal formula cells
- [ ] **Review graph:** Manual completeness review done; optional LLM dependency audit passed (`pytest --run-skipped`)
- [ ] **Verify graph:** Scenario matrix defined in `tests/differential/`; warm `.cache/dependency-graph/` from extract (or `scripts.regenerate_graph_cache`); graph-oracle parity passes (`uv run python -m tests.differential.differential_test_graph`)
- [ ] **Configure:** Internal binding coverage passes (`uv run pytest tests/test_internal_binding_coverage.py`)
- [ ] **Export:** `dist/` package builds; keyword-only `compute_*` scenario runs (bindings authored beyond empty placeholders)
- [ ] **Annotate:** Public API and internals helpers have Google-style docstrings (`--only-stage annotate` or a full run)
- [ ] **Validate:** Authored exported-library FormulaEvaluator sweep passes (`build_scenarios()` / `output_cell_labels()` filled; no Excel required for the library harness)
- [ ] **Document:** Public API uses domain language; economist-facing `user_guide/` present

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
uv run ruff check
uv run ruff format
uv run ty check
```

Pull requests run the same test suite on Ubuntu via `.github/workflows/test.yml`.

### Deploying the generated package

The deploy workflow (`.github/workflows/deploy.yml`) is available from the Actions tab via `workflow_dispatch`. It is **publish-only**: the extraction pipeline calls LLM providers and can take hours, so generation is run locally and never in CI. Generate the package and commit `dist/`, then dispatch the workflow:

```bash
uv run python -m src.extraction_pipeline
git add -f dist && git commit -m "Regenerate dist/"
```

`dist/` is gitignored by default; commit it explicitly (`git add -f dist`) so the deploy workflow has something to publish. On dispatch the workflow verifies the committed `dist/`, uploads it as a build artifact, and — when `DIST_METADATA.repository_url` points at a GitHub repository — publishes `dist/` to that repository by rsyncing over its contents and pushing to `main`. Publishing requires a `DEPLOY_TOKEN` repository secret (a token with write access to the target repository); when `repository_url` is unset or not a GitHub URL, the publish steps are skipped and only the artifact is produced. The generated package ships its own docs deploy workflow (`dist/.github/workflows/deploy-docs.yml`), so the target repository can publish the user guide to GitHub Pages on push.

Opt-in LLM graph spot-check tests: `uv run pytest --run-skipped` (workbook audits auto-select from a warm `.cache/dependency-graph/`; optional `GRAPH_AUDIT_CASES` steers pins/labels; synthetic fixture audits run without extra setup; provider API key required for `LLM_GRAPH_AUDIT_MODEL`).

## Repository layout

| Path | Role |
|---|---|
| `workbook_config.py` | Workbook-specific configuration boundary |
| `src/` | Reusable pipeline implementation |
| `bindings/` | Series binding sidecars (user-authored) |
| `data/` | Workbook, guide, differential reports |
| `dist/` | Generated distributable package (gitignored) |
| `templates/` | Binding prompt and canonical API usage reference |
| `.github/workflows/` | Template CI (PR tests) and manual deploy workflow |
| `technical_standard.md` | Acceptance bar and stage gates |
| `lessons-learned.md` | Design rationale from the Tiny DSA rehearsal |
| `artifacts/` | Generated exploration artifacts; see [artifacts/README.md](artifacts/README.md) |
