## Python best practices

Always run Python code with `uv run`.

Use `fastpyxl` as a drop-in replacement for `openpyxl`.

Use static type annotations and direct attribute access. True defensive programming means enforcing that incorrect code fails fast and loudly.

## Test-driven development

Practice test-driven development (TDD). First write RED-phase tests and watch them fail for the right reason, then write code to turn the tests GREEN.

## Git branching

If you are asked to commit your work, make sure you commit it to an issue branch created with `gh issue develop`. Check that you are not already on such a branch before creating a new one.

## Binding and graph-cache utilities

Warm extract also caches `derive_*_series` and `validate_series_bindings` payloads under `.cache/series-resolution/` and `.cache/bindings-validation/` (keyed from `graph_cache_key`, like `.cache/projection/`), derived leaf classification / binding indexes / bound keys under `.cache/series-derived/`, formula clusters and the refactor schedule under `.cache/clusters/` (keyed from `projection_cache_key`, `variation_mode`, `clustering_mode`, bindings fingerprint, schema, and `excel-grapher` version), codegen module texts under `.cache/codegen/`, and content-keyed refactored `internals.py` under `.cache/internals/<key>.py` (keyed from `codegen_cache_key`, clusters key, a digest of `.cache/internals-refactors.json`, `MECHANICAL_BODY_SCHEMA_VERSION`, `PARITY_GATE_SCHEMA_VERSION`, `MECHANICAL_REFACTOR_BODIES`, the refactor model, and `excel-grapher` version). The Pass 1 mechanical checkpoint lives under `.cache/internals/<package-namespace>/internals.mechanical.py` (deleted after successful promotion). `dist/.pipeline-cache-keys.json` records an `internals_inputs` block (refactor model, mechanical/parity schema versions, `MECHANICAL_REFACTOR_BODIES`, `excel-grapher` version) so pre-clustering adoption of a committed `dist/` can be refused when the refactor recipe drifted; a sidecar carrying an `internals_key` without that block is treated as unverifiable and refused. `--no-cache`, `--force-rebuild`, and any `RefactorLabOptions` that need Pass 1/2 to run (dry run, gate off, or an observer) all skip adoption and start from pristine codegen. Those directories are local/untracked; pytest redirects them via `tests/fixtures/test_state.py`. Pass `--no-cache` to bypass them. Derived repos that previously committed `dist/<package>/internals.mechanical.py` should delete that stale package-root sidecar.

When upgrading excel-grapher, raise the `excel-grapher>=…` floor in `pyproject.toml` to the new version, then `uv lock` / sync, and run regenerate with `--force`. Regenerate prunes entries whose keys are not in the current key set. A normal pipeline or pytest `get_or_build_*` cache hit or miss also drops entries whose meta `excel_grapher_version` no longer matches the installed package, so local cache dirs do not accumulate old-version pickles even if regenerate was skipped.

Five workflow scripts live under `scripts/`:

- `uv run python -m scripts.regenerate_graph_cache` — rebuild and prune `.cache/dependency-graph/` and `.cache/bindings-validation/` so warm pytest/CI runs can skip cold graph builds and `validate_series_bindings`. Use `--force` after changing the workbook, `bindings/*.bindings.yaml`, `workbook_config.py` targets/constraints, or upgrading excel-grapher; after an excel-grapher upgrade also raise the `excel-grapher>=…` floor in `pyproject.toml` to the new version before locking. `--force` also clears `.cache/series-resolution/`, `.cache/series-derived/`, `.cache/bindings-validation/`, `.cache/clusters/`, and `.cache/internals/` (entries plus Pass 1 checkpoint dirs) and prunes series/validation caches to keys derived from the current graph cache keys. Clusters and internals are cleared rather than pruned because their keys fold in projection/codegen/cluster keys the script does not compute. Optional extra target bundles come from `GRAPH_CACHE_TARGET_BUNDLES` in `workbook_config.py`. Commit the cache directory only when your downstream pipeline chooses to vendor it (override `.gitignore` for `.cache/dependency-graph/`).
- `uv run python -m scripts.internal_binding_burndown` — print a burn-down worklist of formula cells not covered by input/output/internal bindings or `INTERNAL_BINDING_EXEMPT_CELLS`, grouped into contiguous `(sheet, row)` column ranges. Prefers the fingerprint-matching graph cache entry when present; otherwise falls back to the newest pickle with a stale-key warning.
- `uv run python -m scripts.binding_resolution_audit` — resolve authored input/output/internal bindings and print Tier-1 codegen-fatal issues (`bind_resolution_failed`, empty public series, partial bind failures, sparse `column_header`/`row_label` spans without `fill: true`). Exit non-zero on errors; `--strict` also fails on warnings. Complements burndown (coverage of unbound cells) rather than replacing it. Shares burndown's graph-cache loader (fingerprint match, then newest-with-warning).
- `uv run python -m scripts.author_bindings` — emit `inputs.bindings.yaml`, `outputs.bindings.yaml`, and `internals.bindings.yaml` from a declarative catalog (default: `templates/binding-catalog.example.yaml`), then validate with `validate_bindings_workbook`. Specialize the catalog per derived repo rather than hard-coding sheet geometry in Python.
- `uv run python -m scripts.run_semantic_naming --internals dist/<package>/internals.py` — run standalone Pass-2 semantic naming on a mechanical `internals.py` without graph, clustering, or parity-gate context. Discovers helpers still carrying the pending-naming placeholder docstring, asks the refactor model for docstring + local renames, and writes the module. Supports `--dry-run`, `--no-cache`, and `--list-only`; successful responses are written to `.cache/internals-refactors.json` as they arrive, so an interrupted run resumes without re-paying completed LLM calls. Its v1 prompts are thinner than in-pipeline Pass 2 (no fingerprint context), so naming quality may differ slightly.

Run `uv run pytest tests/test_binding_utility_scripts.py` after changing these utilities.

## Opening issues to `Teal-Insights/excel-grapher`

We control the `Teal-Insights/excel-grapher` repository, so we can and should open issues there directly when we encounter bugs or need new features.

If a bug blocks our work, open an issue, mark it urgent, and stop working until the bug is fixed. If we can work around it, you should still open an issue so that we can track it and fix it in the future.

Remember to include at least a working minimal complete verifiable example (MCVE) of the bug in the issue body. The MCVE must be *self-contained* (must not depend on any local file artifacts or environment variables).