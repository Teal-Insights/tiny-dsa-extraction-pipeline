## Python best practices

Always run Python code with `uv run`.

Use `fastpyxl` as a drop-in replacement for `openpyxl`.

Use static type annotations and direct attribute access. True defensive programming means enforcing that incorrect code fails fast and loudly.

## Test-driven development

Practice test-driven development (TDD). First write RED-phase tests and watch them fail for the right reason, then write code to turn the tests GREEN.

## Git branching

If you are asked to commit your work, make sure you commit it to an issue branch created with `gh issue develop`. Check that you are not already on such a branch before creating a new one.

## Binding and graph-cache utilities

The live orchestrator is `extract → export → annotate → validate → document`. Warm extract also caches `derive_*_series` and `validate_series_bindings` payloads under `.cache/series-resolution/` and `.cache/bindings-validation/` (keyed from `graph_cache_key` plus a bindings fingerprint, like `.cache/series-derived/`), derived leaf classification / binding indexes / bound keys under `.cache/series-derived/` (also keyed from `graph_cache_key` plus bindings fingerprint, validation mode, and exemptions), and codegen module texts under `.cache/codegen/` (keyed from the projection cache key, targets, unpack/docstring options, `paradigm="inverted_tree"`, guide fingerprint, and `excel-grapher` version). Annotate caches LLM Google-style docstrings in `.cache/inverted-tree-docstrings.json`. Authored user-guide trees cache under `.cache/user-guide/`. Dependency-graph cache keys do **not** fold bindings: the graph is a function of workbook + targets + constraints + blank_ranges + flags + `excel-grapher` version only. Those directories are local/untracked; pytest redirects them via `tests/fixtures/test_state.py`. Pass `--no-cache` to bypass them.

Leftover `.cache/clusters/` and `.cache/internals/` entries from the ctx refactor path may still sit on disk until [#55](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/issues/55). The live orchestrator does not write them.

When upgrading excel-grapher, raise the `excel-grapher>=…` floor in `pyproject.toml` to the new version, then `uv lock` / sync, and run regenerate with `--force`. Regenerate prunes entries whose keys are not in the current key set. A normal pipeline or pytest `get_or_build_*` cache hit or miss also drops entries whose meta `excel_grapher_version` no longer matches the installed package, so local cache dirs do not accumulate old-version pickles even if regenerate was skipped.

Live workflow scripts and stages:

- `uv run python -m scripts.regenerate_graph_cache` — rebuild and prune `.cache/dependency-graph/` and `.cache/bindings-validation/` so warm pytest/CI runs can skip cold graph builds and `validate_series_bindings`. Use `--force` after changing the workbook, `bindings/*.bindings.yaml`, `workbook_config.py` targets/constraints/`BLANK_RANGES`, or upgrading excel-grapher; after an excel-grapher upgrade also raise the `excel-grapher>=…` floor in `pyproject.toml` to the new version before locking. Binding-only edits do not invalidate the dependency-graph key (the graph does not read bindings), but they do invalidate series-resolution, bindings-validation, and series-derived keys via `bindings_fingerprint`. `--force` also clears `.cache/series-resolution/`, `.cache/series-derived/`, `.cache/bindings-validation/`, `.cache/clusters/`, and `.cache/internals/` and prunes series/validation caches to keys derived from the current graph cache keys plus bindings fingerprint. Clusters and internals are cleared rather than pruned because leftover keys fold in projection/codegen/cluster keys the script does not compute. Optional extra target bundles come from `GRAPH_CACHE_TARGET_BUNDLES` in `workbook_config.py`. Commit the cache directory only when your downstream pipeline chooses to vendor it (override `.gitignore` for `.cache/dependency-graph/`).
- `uv run python -m scripts.internal_binding_burndown` — print a burn-down worklist of formula cells not covered by input/output/internal bindings or `INTERNAL_BINDING_EXEMPT_CELLS`, grouped into contiguous `(sheet, row)` column ranges. Prefers the fingerprint-matching graph cache entry when present; otherwise falls back to the newest pickle with a stale-key warning (workbook/target/blank_ranges drift).
- `uv run python -m scripts.binding_resolution_audit` — resolve authored input/output/internal/constant bindings and print Tier-1 codegen-fatal issues (`bind_resolution_failed`, empty public series, partial bind failures, sparse `column_header`/`row_label` spans without `fill: true`). Exit non-zero on errors; `--strict` also fails on warnings. Complements burndown (coverage of unbound cells) rather than replacing it. Shares `src.graph_cache.load_pipeline_dependency_graph` (fingerprint match, then newest-with-warning).
- `uv run python -m scripts.author_bindings` — emit `inputs.bindings.yaml`, `outputs.bindings.yaml`, `internals.bindings.yaml`, and `constants.bindings.yaml` from a declarative catalog (default: `templates/binding-catalog.example.yaml`), then validate with `validate_bindings_workbook`. Specialize the catalog per derived repo rather than hard-coding sheet geometry in Python.
- `uv run python -m src.extraction_pipeline --only-stage annotate` — splice LLM Google-style docstrings onto the inverted-tree `api.py` / `internals.py` (`src/inverted_tree_docstrings.py`). Requires a warm `export.json` manifest. Cache: `.cache/inverted-tree-docstrings.json`. Uses `DOCSTRING_MODEL`.

Leftover clustering / Pass-2 CLIs (not called by the live orchestrator; remove in [#55](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/issues/55)):

- `uv run python -m scripts.compare_cluster_variation_modes` — compare `independent` vs `dominant_key_only` series-fingerprint family counts (`--include` for diffs; `--clustering-mode` to override).
- `uv run python -m scripts.diagnose_schedule_atomization` — explain fingerprint-family → schedule-slice fan-out with peel evidence and remodel recommendations.
- `uv run python -m scripts.inspect_cluster --cluster-id N` — print members/formulas for one fingerprint family; `--schedule` / `--sources` / `--internals` for peels and `cell_*` bodies.
- `uv run python -m scripts.run_semantic_naming --internals dist/<package>/internals.py` — standalone Pass-2 semantic naming on a mechanical `internals.py`. Uses `REFACTOR_MODEL` (dormant for the live pipeline until #55).
- `uv run python -m scripts.run_refactor_stage` — leftover Pass 1/2 entry point; not a `PIPELINE_STAGES` name.

Run `uv run pytest tests/test_binding_utility_scripts.py` after changing the live binding utilities.

## Opening issues to `Teal-Insights/excel-grapher`

We control the `Teal-Insights/excel-grapher` repository, so we can and should open issues there directly when we encounter bugs or need new features.

If a bug blocks our work, open an issue, mark it urgent, and stop working until the bug is fixed. If we can work around it, you should still open an issue so that we can track it and fix it in the future.

Remember to include at least a working minimal complete verifiable example (MCVE) of the bug in the issue body. The MCVE must be *self-contained* (must not depend on any local file artifacts or environment variables).
