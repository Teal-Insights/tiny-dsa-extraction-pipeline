# Migrating a derived pipeline to inverted-tree export

This template exports **only** inverted-tree Python. excel-grapher 21.1.0+
`generate_modules()` is keyword-only inverted-tree codegen (no `paradigm`, no
cell-address `targets`; outputs come from the bindings catalog). There is no
ctx dual-mode flag, no `make_context` / `set_*` public API, and no clustering
or Pass-1 / Pass-2 internals refactor. Derived repos that still run the old
ctx path should follow this playbook rather than keeping leftover clustering
CLIs.

Tiny DSA already completed the same migration. Port **end-state generic
machinery** from this template (or from Tiny DSA after its stack delete), not
cherry-picks of Tiny DSA commits — those mix `dist/` regenerations, workbook
hooks, and files later deleted.

The first Tiny DSA inverted-tree pin was excel-grapher git rev
`3c759a472f85c115359e9cb14c05eac86432e093` (excel-grapher #597). That pin is
**historical**: this template now requires `excel-grapher>=21.1.0`. Raise the
floor and `uv lock` when upgrading, then regenerate caches with `--force`.

## Why move

Ctx export is a mutable evaluation context plus records-shaped setters. Inverted
tree is a pure function of the leaf closure of each output subgraph:

- `compute_*` is keyword-only.
- Scalars stay scalars; series are 1-D sequences in canonical key order.
- Returns are `tuple[float, ...]` (tuple index ↔ key order), not SDMX records.
- There is no `make_context`, no `set_*`, and no `_api_helpers` / `_readers`.

Bindings remain the Excel map (`series_id` ↔ range). Internals helpers are
named from `series_id`. Clustering and Pass-1 / Pass-2 internals refactor are
not part of the orchestrator and are not shipped in this template.

Once the graph matches Excel on the authored scenario sweep, the exported
library should treat **the graph** as its oracle. Driving Excel again from
`compute_*` would only re-prove extraction, and inverted tree has no setter
path to do it with.

| Question | Oracle | SUT |
|---|---|---|
| Did we extract the workbook faithfully? | Excel (`xlwings`) | `FormulaEvaluator` on the graph |
| Did we code-generate that graph faithfully? | Graph (`FormulaEvaluator`) | `compute_*` |

Library ≈ Excel then follows by transitivity on the **same** scenarios, without
COM at pipeline `validate` time.

## Live stages

The orchestrator is `extract → export → annotate → validate → document`.

| Stage | What it does |
|---|---|
| **extract** | Dependency graph + series resolution. Graph cache keys do not fold bindings. |
| **export** | `CodeGenerator(graph).generate_modules(series_bindings=..., bindings_workbook=..., blank_ranges=...)`. The codegen cache key still requires `paradigm="inverted_tree"` so a ctx payload cannot be served as inverted tree. |
| **annotate** | LLM Google-style docstrings spliced onto `api.py` and `internals.py` (`src/inverted_tree_docstrings.py`, cache `.cache/inverted-tree-docstrings.json`). Fail closed if the model returns argument names that do not match the signature. |
| **validate** | Run the authored library-vs-graph FormulaEvaluator sweep (`src/differential_validation.py` → `differential_test_exported_library.py`). Empty `build_scenarios()` / `output_cell_labels()` fail closed. Writes `data/differential/exported_library/` and copies reports into `dist/tests/results/reference/` when present. |
| **document** | Cursor SDK agent authors `user_guide/` against keyword-only `compute_*`. Bump `USER_GUIDE_AGENT_PROMPT_VERSION` (and clear `.cache/user-guide/`) when the agent prompt template changes. |

`--only-stage`, `--start-from-stage`, and `--stop-after-stage` use these names.
There is no `refactor` stage and no clustering CLI.

Package shape after export: `api.py`, `internals.py`, `runtime.py`, `data.py`,
`__init__.py`.

## Derived-repo checklist

These do not come along automatically from a template merge:

1. **Author `constants.bindings.yaml`** for every graph leaf classified
   `constant`. Inverted-tree helpers take those leaves as arguments (with
   `data.py` defaults). A MATCH key or year-label row that stays an unbound
   `xl_cell` will not appear as a typed sequence on `compute_*`. Stamp binding
   schema **1.17.0** on every shard together. Add a coverage test: every
   `kind == "constant"` leaf must appear in the constants shard. Fail closed.
2. **Fill graph (and library) scenario hooks** in
   `tests/differential/differential_test_graph.py` and
   `tests/differential/differential_test_exported_library.py` before claiming
   Excel ≈ graph ≈ library. Empty `build_scenarios()` / `output_cell_labels()`
   is the template default and is **not** extraction proof.
   - `build_scenarios()` — the same matrix both harnesses use.
   - `output_cell_labels()` — `(label, address)` pairs for every compared cell.
   - `inputs_for_excel()` — scenario → Excel cell writes (the graph driver
     still sets nodes by those addresses).
   - `mvp_outputs_for_scenario()` — keyword-only `compute_*` calls.
   - Leave `build_axes()` as `()` unless the derived repo already thinks in axes.
3. **Map scenario fields onto `compute_*` kwargs.** Leaves that are not in the
   scenario come from `data.py` defaults and must match the graph's stored
   workbook values. Canonical baseline tables may differ from `data.py`
   defaults; copy whatever the library hooks already used so the two harnesses
   stay aligned.
4. **Rewrite tests** that import `make_context`, `set_*`, `_api_helpers`, or
   records-shaped `OBS_VALUE` outputs. Prefer explicit kwargs over
   `**dict[str, object]` so `ty` can check them.
5. **Refresh user-guide caches** after the agent prompt or exported API
   changes (`USER_GUIDE_AGENT_PROMPT_VERSION`, `.cache/user-guide/`).
6. **`workbook_config.RUNNABLE_CELL_RULES`** should reject `make_context(` and
   `set_*` in user-guide `{python}` cells.

Keep `xlwings` / `fastpyxl` for the graph-vs-Excel sweep. Documentation
packages (`great-docs`, Quarto, pandas, …) belong in the **dist** baseline,
not the extraction venv.

## Pitfalls

- **Fail closed.** Missing scenarios, empty `output_cell_labels()`, LLM arg-name
  drift, unbound constant leaves, and graph writes with no `compute_*`
  counterpart should raise. Do not add skip-if-absent paths.
- **Codegen cache `paradigm`.** Omitting it (or defaulting it to `ctx`) serves
  the wrong modules on a cache hit. The key requires `paradigm`.
- **Do not treat empty graph hooks as extraction proof.** README coverage
  numbers that assume an authored matrix will lie until hooks are filled.
- **Dist `--layout exported` still needs the extraction repo** for
  `workbook_config` / `src.graph_cache` / `MvpGraphDriver`. Dist ships reports
  and a copy of the harness; the sweep is an extraction-repo tool.
- **`ty` and `**kwargs`.** Unpacking `dict[str, object]` into keyword-only
  `compute_*` fails the type checker. Pass named arguments.
- **Do not keep clustering CLIs.** `compare_cluster_variation_modes`,
  `diagnose_schedule_atomization`, `inspect_cluster`, `run_refactor_stage`,
  and `run_semantic_naming` are gone. Do not reintroduce `VARIATION_MODE`,
  `CLUSTERING_MODE`, or `DOCSTRING_CALLBACK_NAME`.

## Suggested sequence if you are still on ctx

1. Land constant-binding coverage and schema 1.17.0 while still on ctx export.
   Ctx and inverted tree both need those series.
2. Require `excel-grapher>=21.1.0`. `generate_modules()` is inverted-tree only; keep `paradigm="inverted_tree"` in the codegen cache key.
3. Remodel stages to `extract → export → annotate → validate → document`.
4. Author graph-vs-Excel hooks and run that sweep on Windows before trusting
   extraction.
5. Remodel the exported-library harness to FormulaEvaluator. Run the full
   scenario sweep in the extraction repo (no Excel).
6. Commit new library-vs-graph reports under
   `data/differential/exported_library/` only after that sweep passes. Until
   then, leave any historical Excel goldens under `data/differential/graph/`
   in place; pipeline `validate` writes library-vs-graph reports only.
7. Delete the ctx clustering / Pass-1 / Pass-2 stack. This template has
   already done that step.
