# Differential testing

## What this is, in plain terms

**Differential testing** feeds identical inputs to two oracles and compares outputs.

We have an Excel workbook (`tiny-dsa.xlsx`) that everyone agrees is "correct," and a
Python reimplementation of the same calculations. **Differential testing** is the
practice of feeding *identical inputs* to both, then checking that they produce the
*same outputs*. If they ever disagree, one of them has a bug — and because we trust
Excel, the disagreement points at the Python side. It is a long-standing, well-established method for validating a new implementation
against a trusted one (McKeeman, *Differential Testing for Software*, 1998).

A few terms used throughout, in everyday language:

| Term | Plain meaning |
|---|---|
| **Graph golden master** | Trusted Excel workbook recalculated through COM automation (`xlwings`) |
| **Library golden master** | Extraction graph evaluated with `FormulaEvaluator` |
| **MVP / SUT** | Graph evaluator (vs Excel) or exported `compute_*` (vs the graph) |
| **Hybrid tolerance (`atol` / `rtol`)** | Pass iff `abs_diff <= 1e-6` **or** `rel_diff <= 1e-12` (golden-anchored); see `technical_standard.md` §1.1 |

## Two harnesses

Run the **graph** differential before export to validate extraction fidelity
against Excel. Run the **exported-library** differential after export to
validate codegen against that graph.

| Harness | Oracle | SUT | When |
|---|---|---|---|
| [`differential_test_graph.py`](differential_test_graph.py) | Microsoft Excel (`xlwings`) | In-memory `FormulaEvaluator` | Before / alongside extraction review |
| [`differential_test_exported_library.py`](differential_test_exported_library.py) | `FormulaEvaluator` on the extracted graph | Generated package `compute_*` | After `uv run python -m src.extraction_pipeline` |

Both harnesses import shared scenario types from
[`differential_types.py`](differential_types.py) (`Scenario`, optional `Axis` /
`AxisPoint`, `ATOL`, and `RTOL`). Numeric comparison is shared via
[`comparison_utils.py`](comparison_utils.py). Golden-master cell reads go through
[`differential_excel.py`](differential_excel.py) (`err_to_str=True`) for the
graph-vs-Excel harness. Both harnesses use
[`differential_scenario_inputs.py`](differential_scenario_inputs.py) to collect
the union of scenario write addresses so each scenario restores workbook
baselines before applying overrides.

Workbook-specific hooks live at the bottom of each harness module (or in a
workbook matrix module that those hooks import).

### Address keys

`excel-grapher` stores graph keys in **canonical** form. Sheets whose names
contain spaces, hyphens, or apostrophes are quoted (e.g. `'Discrete Risks'!H2`).
Human-authored config (`CONSTRAINTS`, scenario matrices, bindings) often uses
unquoted spellings (`Discrete Risks!H2`). Both refer to the same cell, but naive
string equality against graph keys fails.

At harness boundaries, import from `excel_grapher.core.address_keys`:

| Helper | Use when |
|--------|----------|
| `normalize_key(address)` | Comparing to `leaf_keys()` / `formula_keys()`, calling `graph.set_node_value()`, `graph.get_node()`, `FormulaEvaluator.evaluate()` |
| `parse_address(normalize_key(address))` | Driving Excel via xlwings/COM (sheet name + A1 coordinate) |

Do **not** re-implement quoting rules or use `split("!", 1)` on sheet-qualified
addresses inside harness code. Config authors may keep unquoted addresses;
normalization belongs at the boundary.

### Workbook-exact dropdown labels

Scenario matrices and exported APIs naturally use clean logical values (`"High"`,
`"Real interest rate"`). Many workbooks branch with **exact string equality** on
reference label cells (e.g. `IF($B$2=$B$9, …)`). Writing logical strings to
public input cells instead of the workbook's exact label literals can silently
fall through to `""` and produce `#VALUE!` on later projection years — while
parity still passes when both oracles error the same way.

This is distinct from [address-key normalization](#address-keys) ([issue #51](https://github.com/Teal-Insights/extraction-pipeline-template/issues/51)): #51 covers sheet-qualified **addresses**; [issue #53](https://github.com/Teal-Insights/extraction-pipeline-template/issues/53) covers **cell values** for dropdown/enum inputs.

At the harness boundary:

1. Load reference label literals from project-configured cells (see
   `REFERENCE_LABEL_CELLS` in `workbook_config.py`).
2. Keep logical values in scenario definitions and API parameter names.
3. Resolve logical → workbook-exact immediately before `inputs_for_excel()` /
   graph `set_inputs()` using [`workbook_labels.py`](workbook_labels.py).
4. Write the **raw workbook string** — do not strip trailing spaces or suffixes.

Matched `#VALUE!` / `#N/A` comparisons **fail the run by default**: a matched
error on a long-horizon output often signals a label mismatch rather than an
intentional error-boundary scenario, and a comparison where both oracles error
identically is not evidence of parity. Set `expects_error_values=True` on
scenarios that deliberately exercise error paths, or pass
`--allow-matched-errors` to triage without failing.

### Graph harness hooks

1. **`build_scenarios()`** or **`build_axes()`** — representative input combinations.
2. **`output_cell_labels()`** — mirror output bindings as `(label, address)` pairs.
3. **`inputs_for_excel()`** — map each scenario to Excel cell writes.

Before each scenario the graph harness restores every input cell in the union of
all scenario writes to its workbook baseline, then applies that scenario's
declared overrides. Undeclared cells therefore do not inherit values from prior
scenarios.

The graph harness also reports input cells absent from the extracted graph —
itself a differential signal about extraction coverage.

**Tiny DSA coverage:** 118 scenarios × 15 output cells = 1,770 comparisons.

### Exported-library harness hooks

1. **`build_scenarios()`** — representative input combinations.
2. **`output_cell_labels()`** — `(label, address)` pairs, one per bound output
   cell. Prefer generating from derived output series (see [Full-API
   coverage](#full-api-coverage)).
3. **`inputs_for_excel(scenario)`** — map each scenario to graph cell writes
   (Excel addresses). The graph driver sets nodes by those addresses; keep the
   hook name.
4. **`mvp_outputs_for_scenario(api, scenario)`** — wrap leaf kwargs in
   `{Output}Inputs.from_defaults(...)`, pass that bundle to `compute_*`, and
   return `{label: value}`. Do not call `compute_*(country_name=..., ...)`;
   excel-grapher 22 helpers take a single Inputs dataclass.

Optional fifth hook:

- **`expressible_input_cells() -> frozenset[str] | None`** — cells reachable as
  `compute_*` arguments. Return `None` (template default) to skip the symmetry
  preflight until the workbook matrix is authored.

**Tiny DSA coverage:** 118 scenarios × 15 output cells = 1,770 comparisons.

Keep graph (and library) hooks **empty** in this template. Derived repos must
fill them before claiming Excel ≈ graph ≈ library. Do not treat empty hooks as
extraction proof.

Commit reference reports under `data/differential/graph/` and
`data/differential/exported_library/` after passing sweeps. The export step
copies the exported-library harness, workbook fixture, and reports into
`dist/tests/`.

**Why both matter:** a passing graph differential proves the *extraction* is
faithful; a passing exported-library differential proves the *code generation*
on top of it is faithful too. Transitivity then gives library ≈ Excel on the
same scenarios without driving Excel from the library harness.

### Input symmetry

For the exported harness, every address written by `inputs_for_excel` must also
be expressible as a `compute_*` argument. Otherwise the two oracles receive
different inputs and comparison is unsatisfiable.

When `expressible_input_cells()` returns a frozenset, `_verify_input_symmetry`
fails the run (exit **2**) if any scenario write falls outside that set. Leave
the hook as `None` only while hooks are still stubs.

### Full-API coverage

Prefer building output specs from derived output series
(`id`, `compute_name`, `key_fields`, `cells[{address, key}]`) via
[`output_specs.py`](output_specs.py):

- `specs_from_output_series(...)` → one `OutputCellSpec` per bound cell
- `compute_outputs_for_writes(...)` → call each unique `compute_*` and map
  results onto those labels. Scalar returns (`str`, `float`) are one
  observation, not character sequences. Named-axis series are read by spec
  keys.

Graph outputs are keyed by **address**; MVP outputs by **label**. Prefer
`compute_outputs_for_writes` from `mvp_outputs_for_scenario()` so scalars,
1-tuples, sequences, and named-axis series all map onto those labels via
`outputs_from_sequences()`. Wrap leaf kwargs in `{Output}Inputs.from_defaults(...)`
before calling `compute_*`; do not pass leaf kwargs into `compute_*`.

### Crash attribution and reports

Oracles run in separate try/except blocks. When one side crashes,
`crash_comparisons` records the exception repr only on the crashed side and
keeps the surviving oracle's values (MVP crash after the graph succeeded
preserves `graph_value`). The TXT report includes:

- `Workbook SHA-256`
- `ENVIRONMENT` (Python, OS, excel-grapher)
- `FAILING COMPARISONS (n)` listing every failure
- hybrid `atol` / `rtol` tolerance line

When an exported layout ships a workbook fixture under
`dist/tests/fixtures/`, a SHA-256 mismatch against the current workbook hard-fails
(exit **2**). Otherwise mtime vs `data.py` only warns.

## Run

The **graph** harness requires Microsoft Excel locally — `xlwings` drives it
through COM automation. `xlwings` is already in `pyproject.toml`'s dev
dependencies. The **exported-library** harness does **not** require Excel: it
loads the same cached extraction graph as extract and compares `compute_*` to
`FormulaEvaluator`.

**Graph harness prerequisite:** load a warm dependency-graph cache first so the
MVP oracle does not cold-build. Run extract (or regenerate) for the current
workbook / targets / constraints:

```bash
uv run python -m src.extraction_pipeline --only-stage extract
# or
uv run python -m scripts.regenerate_graph_cache
```

`MvpGraphDriver` prefers a read-only hit from committed
`.cache/dependency-graph/` (`COMMITTED_GRAPH_CACHE_DIR`, same split as the
opt-in LLM graph audit under pytest). On miss it builds and saves via
`get_or_build_dependency_graph` into the writable default cache (pipeline
extract policy) and logs a hint to warm the cache first.

```bash
# Graph oracle (extraction repo — run before export)
uv run python -m tests.differential.differential_test_graph

# Triage run: matched error cells listed but not treated as failures
uv run python -m tests.differential.differential_test_graph --allow-matched-errors

# Exported library (extraction repo, after export; FormulaEvaluator oracle)
uv run python -m tests.differential.differential_test_exported_library

# Exported dist project (graph cache + excel-grapher)
uv run python -m tests.differential.differential_test_exported_library --layout exported
```

Comparisons where both oracles return the same Excel error code are always
listed in the report and **fail the run** unless the scenario sets
`expects_error_values=True`. Pass `--allow-matched-errors` on either harness to
downgrade them to warnings during triage.

Exit codes: **`0`** all comparisons pass and no unexpected matched errors, **`1`** any failure or unexpected matched error, **`2`** prerequisite missing or scenarios not configured.

## Golden-master conformance

The comparison ladder, report schema, and acceptance bar are defined in
[`technical_standard.md`](../../technical_standard.md) (§1–§3 and the conformance
checklist at the end). Unit tests in
[`tests/test_differential_harness.py`](../test_differential_harness.py) lock the
exported-library harness helpers (`compare_cell`, `crash_comparisons`,
`write_csv_report`, `write_txt_summary`) to that standard on every PR — no Excel
required.

CSV columns use `graph_value` / `mvp_value` as aliases for the standard's
`golden_value` / `sut_value` terminology.

## Output locations

| Harness | Reports |
|---|---|
| Graph (extraction repo) | `data/differential/graph/differential_report.{csv,txt}` |
| Exported library (extraction repo) | `data/differential/exported_library/parity_report.{csv,txt}` |
| Exported dist (local rerun) | `dist/tests/results/local/` |
| Exported dist (shipped reference) | `dist/tests/results/reference/` |

Refresh committed reference reports whenever the workbook, bindings, constraints, or scenario sweep changes.

The `pytest` test suite also runs a small sample of differential tests with randomized inputs.
