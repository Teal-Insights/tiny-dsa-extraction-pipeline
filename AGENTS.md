## Python best practices

Always run Python code with `uv run`.

Use `fastpyxl` as a drop-in replacement for `openpyxl`.

Use static type annotations and direct attribute access. True defensive programming means enforcing that incorrect code fails fast and loudly.

## Test-driven development

Practice test-driven development (TDD). First write RED-phase tests and watch them fail for the right reason, then write code to turn the tests GREEN.

## Git branching

If you are asked to commit your work, make sure you commit it to an issue branch created with `gh issue develop`. Check that you are not already on such a branch before creating a new one.

## Binding and graph-cache utilities

Three workflow scripts live under `scripts/`:

- `uv run python -m scripts.regenerate_graph_cache` — rebuild and prune `.cache/dependency-graph/` so warm pytest/CI runs can skip cold graph builds. Use `--force` after changing the workbook, `bindings/*.bindings.yaml`, `workbook_config.py` targets/constraints, or upgrading excel-grapher. Optional extra target bundles come from `GRAPH_CACHE_TARGET_BUNDLES` in `workbook_config.py`. Commit the cache directory only when your downstream pipeline chooses to vendor it (override `.gitignore` for `.cache/dependency-graph/`).
- `uv run python -m scripts.internal_binding_burndown` — print a burn-down worklist of formula cells not covered by input/output/internal bindings or `INTERNAL_BINDING_EXEMPT_CELLS`, grouped into contiguous `(sheet, row)` column ranges. Reuses the newest cached graph pickle even when the bindings fingerprint has changed.
- `uv run python -m scripts.author_bindings` — emit `inputs.bindings.yaml`, `outputs.bindings.yaml`, and `internals.bindings.yaml` from a declarative catalog (default: `templates/binding-catalog.example.yaml`), then validate with `validate_bindings_workbook`. Specialize the catalog per derived repo rather than hard-coding sheet geometry in Python.

Run `uv run pytest tests/test_binding_utility_scripts.py` after changing these utilities.

## Opening issues to `Teal-Insights/excel-grapher`

We control the `Teal-Insights/excel-grapher` repository, so we can and should open issues there directly when we encounter bugs or need new features.

If a bug blocks our work, open an issue, mark it urgent, and stop working until the bug is fixed. If we can work around it, you should still open an issue so that we can track it and fix it in the future.

Remember to include at least a working minimal complete verifiable example (MCVE) of the bug in the issue body. The MCVE must be *self-contained* (must not depend on any local file artifacts or environment variables).