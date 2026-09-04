# Migrating a derived pipeline to inverted-tree export

This note records how Tiny DSA moved off the ctx (`make_context` / `set_*`)
export onto excel-grapher's inverted-tree codegen. Use it if
`extraction-pipeline-template` (or another derived workbook repo) should
follow the same path.

The work landed in three commits on `experimental/inverted-tree-export`:

| Commit | What it does |
|---|---|
| [`d9542f4`](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/commit/d9542f4) | Bind reader-only constant leaves and stamp binding shards at schema **1.13.0**. |
| [`823424c`](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/commit/823424c) | Remodel the orchestrator to `extract → export → annotate → validate → document`, emit keyword-only `compute_*`, annotate with LLM docstrings, and check default-path parity against `FormulaEvaluator`. |
| [`f5801dd`](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/commit/f5801dd) | Retarget the exported-library differential from Excel/`set_*` to the extraction graph. |

A fourth Tiny DSA commit, [`68cbb21`](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/commit/68cbb21) (also cherry-picked to `main`), authors the 118-scenario graph-vs-Excel matrix. The template can keep empty graph hooks; derived repos must fill them before the transitivity argument in [Differential testing](#differential-testing) holds.

Default excel-grapher `paradigm="ctx"` is unchanged. This repo's orchestrator opts into inverted tree.

## Why move

Ctx export is a mutable evaluation context plus records-shaped setters. Inverted
tree is a pure function of the leaf closure of each output subgraph:

- `compute_*` is keyword-only.
- Scalars stay scalars; series are 1-D sequences in canonical key order.
- Returns are `tuple[float, ...]` (tuple index ↔ key order), not SDMX records.
- There is no `make_context`, no `set_*`, and no `_api_helpers` / `_readers`.

Bindings remain the Excel map (`series_id` ↔ range). Internals helpers are
named from `series_id`. Clustering and Pass-1 / Pass-2 internals refactor are
not part of the orchestrator.

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

## Prerequisites

1. **excel-grapher with inverted-tree codegen.** Tiny DSA originally pinned
   excel-grapher git rev `3c759a472f85c115359e9cb14c05eac86432e093`
   (`CodeGenerator.generate_modules(..., paradigm="inverted_tree")`,
   excel-grapher #597). That pin is historical: `pyproject.toml` now requires
   `excel-grapher>=12.7.1`. Raise the floor and `uv lock` when upgrading, then
   regenerate caches with `--force` as usual after a grapher upgrade.
2. **Binding schema 1.13.0** on every shard (`inputs`, `outputs`, `internals`,
   `constants`). Stamp the schema without reshaping existing dimension ids.
3. **Constant series** for every graph leaf classified `constant`. Inverted-tree
   helpers take those leaves as arguments (with `data.py` defaults). A MATCH
   key or year-label row that stays an unbound `xl_cell` will not appear as a
   typed sequence on `compute_*`.
4. **Authored graph-differential hooks** in each derived repo before claiming
   Excel ≈ graph. Empty `build_scenarios()` / `output_cell_labels()` is still
   the template default; the library-vs-graph sweep does not replace that
   proof.

## 1. Constant bindings (`d9542f4`)

Do this before flipping the orchestrator. Inverted-tree codegen reads the
merged workbook bindings, including `bindings/constants.bindings.yaml`.

What Tiny DSA bound as constants:

- `country_profile_names` — MATCH lookup keys (`Inputs!A10:A12`), reader-only.
  The user-facing selector is still the input series `country_name`.
- `engine_year_labels` — Engine header years (`Engine!C5:G5`) that formulas
  compare to `shock_year`.

Rules that generalized:

- Constant MATCH keys and year labels need `read: string` / `read: int` on the
  measure (and on key binds), not a bare `xl_cell`.
- Add a coverage test: every graph leaf with `kind == "constant"` must appear
  in `constants.bindings.yaml`. Fail closed on unbound constant leaves.
- Bump `schema_version` on **all** shards together. Tiny DSA went `1.8.0` →
  `1.13.0` without changing dimension ids.

`scripts.author_bindings` can emit a constants shard from the catalog; keep the
catalog as the Excel map rather than inventing Pass-2 names.

## 2. Orchestrator remodel (`823424c`)

### Stages

| Before | After |
|---|---|
| extract → export → refactor → test → document | extract → export → annotate → validate → document |

- **extract** — dependency graph + series resolution. Unchanged. Graph cache
  keys still do not fold bindings.
- **export** — `CodeGenerator(graph).generate_modules(..., paradigm="inverted_tree")`.
  Fold `paradigm` into the codegen cache key so ctx and inverted-tree payloads
  cannot collide. Ctx `XlErrorException` rewrites are gone; `materialize_package`
  writes codegen payloads as-is.
- **annotate** — LLM Google-style docstrings spliced onto `api.py` and
  `internals.py` (`src/inverted_tree_docstrings.py`, cache
  `.cache/inverted-tree-docstrings.json`). Fail closed if the model returns
  argument names that do not match the signature.
- **validate** — compare default-path `compute_*` to `FormulaEvaluator` on the
  pipeline graph (`src/inverted_tree_validate.py`). Tiny DSA checks
  `Outputs!B12:F12` and `B13:F13` at `atol=1e-9` and writes **only**
  `dist/tests/results/reference/`. Do not overwrite committed Excel goldens
  under `data/differential/exported_library/`.
- **document** — Cursor SDK agent authors `user_guide/` against keyword-only
  `compute_*`. Bump `USER_GUIDE_AGENT_PROMPT_VERSION` (and clear
  `.cache/user-guide/`) when the agent prompt template changes.

Clustering, Pass-1 mechanical refactor, and Pass-2 semantic naming are **not**
called from the orchestrator. The first template pass left those modules on
disk so older unit tests could still import them. Issue #55 later removed that
stack (clustering CLIs, Pass-1/Pass-2, `run_refactor_stage`, and the ctx
rewrite helpers).

`--only-stage`, `--start-from-stage`, and `--stop-after-stage` must use the new
stage names. Stage manifests chain `export → annotate → validate → document`.

### Public API

Replace ctx-oriented tests and docs:

- Keyword-only `compute_*` named from `output.compute.name` / `series_id`.
- Sequences and scalars, not records / `OBS_VALUE`.
- Tuple of floats in key order (Tiny DSA: index `0..4` ↔ `Outputs!B:F`).
- `workbook_config.RUNNABLE_CELL_RULES` should reject `make_context(` and
  `set_*` in user-guide `{python}` cells.
- `templates/user-guide-agent.txt` should point the document agent at the
  generated package API and `docs-source/guidance-note.md` without prescribing
  a fixed page outline.

Package shape after export (Tiny DSA, mechanical + LLM docstrings): `api.py`,
`internals.py`, `runtime.py`, `data.py`, `__init__.py`. Ctx helpers
`_api_helpers.py` and `_readers.py` go away.

### Pipeline `validate` vs the 118-scenario sweep

`inverted_tree_validate.py` is a **narrow** default-Borvelia (or equivalent)
graph-oracle canary so the pipeline can run without Excel. It is not a
substitute for the authored scenario matrix. Keep the full sweep in
`tests/differential/`.

Specialize `src/inverted_tree_validate.py` per derived workbook: output
addresses, which `compute_*` to call, and which `data.py` defaults to pass.
Fail closed; do not skip cells because a helper is missing.

## 3. Exported-library differential (`f5801dd`)

Retarget `tests/differential/differential_test_exported_library.py`:

- Golden oracle: `FormulaEvaluator` via the graph harness's `MvpGraphDriver`
  (reset baselines between scenarios, then `inputs_for_excel()` cell writes).
- SUT: `mvp_outputs_for_scenario(api, scenario)` calling keyword-only
  `compute_*`.
- Drop `apply_inputs_to_mvp`, `XlwingsExcelOracle`, and batched COM reads.
- CSV column `excel_value` becomes `graph_value` (alias for the standard's
  `golden_value`).
- Reports say FormulaEvaluator, not Excel. Dist `tests/README.md` should
  describe a graph-oracle rerun from the extraction repo, not Windows + Excel.
- Copy `differential_scenario_inputs.py` into `dist/tests/differential/` with
  the other harness files (`src/export_validation_assets.py`).

Keep `inputs_for_excel()` as the Excel-address map. The graph driver sets
nodes by those addresses; that is still the right hook name even though the
library harness no longer launches Excel.

`expressible_input_cells()` stays a fail-closed symmetry check: every graph
write must correspond to a `compute_*` argument (reword the error from
"public setter" to `compute_*`).

User-guide evidence for `exported_library` reports should describe
FormulaEvaluator vs `compute_*`, not `xlwings` vs `set_*`.

### Graph hooks (companion to `f5801dd`)

The library harness assumes the graph is already a trusted Excel oracle on the
**same** scenarios. Fill `tests/differential/differential_test_graph.py` hooks
first:

- `build_scenarios()` — the same matrix the library harness uses.
- `output_cell_labels()` — `(label, address)` pairs for every compared cell.
- `inputs_for_excel()` — scenario → Excel cell writes.
- Leave `build_axes()` as `()` unless the derived repo already thinks in axes.

Keep `test_run_differential_test_requires_scenarios` monkeypatching hooks
empty so the template gate still fails closed. Add a derived-repo test that
the authored matrix is non-empty (Tiny DSA: 118 scenarios, 15 cells).

Do not copy `apply_inputs_to_mvp` / `mvp_outputs_for_scenario` into the graph
file.

## Workbook-specific work a derived repo must still do

These do not come along automatically from a template merge:

1. Author `constants.bindings.yaml` from that workbook's constant leaves.
2. Point `inverted_tree_validate.py` at that workbook's default output cells
   and `compute_*` kwargs.
3. Author graph (and library) scenario hooks from bindings, not from ctx
   setters. Canonical baseline shock tables may differ from `data.py` defaults
   (Tiny DSA zeros the library/graph canonical table `(0,0,0)` while
   `SHOCK_MAGNITUDES_DEFAULT` is `(-2, 2, -1)`). Copy whatever the library
   hooks already used so the two harnesses stay aligned.
4. Map scenario fields onto `compute_*` kwargs. Leaves that are not in the
   scenario (Tiny DSA `country_initial_debt`) come from `data.py` defaults and
   must match the graph's stored workbook values.
5. Rewrite any derived tests that import `make_context`, `set_*`,
   `_api_helpers`, or records-shaped `OBS_VALUE` outputs. Prefer explicit
   kwargs over `**dict[str, object]` so `ty` can check them.
6. Refresh user-guide caches after the agent prompt or exported API changes
   (`USER_GUIDE_AGENT_PROMPT_VERSION`, `.cache/user-guide/`).

## Suggested sequence for the template

1. Land constant-binding coverage and schema 1.13.0 while still on ctx export
   (equivalent of `d9542f4`). Ctx and inverted tree both need those series.
2. Pin / release excel-grapher with `paradigm="inverted_tree"`.
3. Remodel stages and templates (equivalent of `823424c`). The first pass
   skipped leftover clustering tests and left those modules in the tree;
   issue #55 later deleted them.
4. Require derived repos to author graph-vs-Excel hooks (equivalent of
   `68cbb21`) and run that sweep on Windows before trusting extraction.
5. Remodel the exported-library harness to FormulaEvaluator (equivalent of
   `f5801dd`). Run the full scenario sweep in the extraction repo (no Excel).
   Tiny DSA: 118 × 15 = 1,770 comparisons, a few seconds with a warm graph
   cache.
6. Commit new library-vs-graph reports under
   `data/differential/exported_library/` only after that sweep passes. Until
   then, leave any historical Excel goldens in place; pipeline `validate`
   must not clobber them.

## Pitfalls

- **Fail closed.** Missing scenarios, missing output cells, LLM arg-name
  drift, unbound constant leaves, and graph writes with no `compute_*`
  counterpart should raise. Do not add skip-if-absent paths.
- **Ctx rewrites are gone.** Issue #55 removed `apply_rewrites` and the
  `XlErrorException` helpers that targeted the old package shape.
- **Codegen cache `paradigm`.** Omitting it serves ctx modules as inverted
  tree (or the reverse) on a cache hit.
- **Narrow validate ≠ full sweep.** Default-path FormulaEvaluator is a
  pipeline canary. The authored matrix is the codegen proof.
- **Do not treat empty graph hooks as extraction proof.** README coverage
  numbers that assume an authored matrix will lie until hooks are filled.
- **Dist `--layout exported` still needs the extraction repo** for
  `workbook_config` / `src.graph_cache` / `MvpGraphDriver`. The 118-sweep is
  an extraction-repo tool; dist ships reports and a copy of the harness.
- **`ty` and `**kwargs`.** Unpacking `dict[str, object]` into keyword-only
  `compute_*` fails the type checker. Pass named arguments.

## Files to expect in a template port

New or heavily remodeled:

- `src/inverted_tree_docstrings.py`, `src/inverted_tree_validate.py`
- `src/extraction_pipeline.py` stage list and export/annotate/validate
- `src/codegen_cache.py` (`paradigm` in the key)
- `src/package_materialize.py`
- `src/stage_manifest.py` upstream chain
- `templates/user-guide-agent.txt`
- `workbook_config.RUNNABLE_CELL_RULES`
- `tests/differential/differential_test_exported_library.py`
- `src/documentation_pipeline.py` (validation page + Cursor agent runner)
- `src/export_validation_assets.py` (dist tests README + harness file list)

Workbook-local:

- `bindings/constants.bindings.yaml` and schema stamps
- Graph/library differential hooks
- `inverted_tree_validate.py` output addresses
