# Differential testing

## What this is, in plain terms

We have an Excel workbook (`tiny-dsa.xlsx`) that everyone agrees is "correct," and a
Python reimplementation of the same calculations. **Differential testing** is the
practice of feeding *identical inputs* to both, then checking that they produce the
*same outputs*. If they ever disagree, one of them has a bug — and because we trust
Excel, the disagreement points at the Python side. It is a long-standing, well-established method for validating a new implementation
against a trusted one (McKeeman, *Differential Testing for Software*, 1998).

A few terms used throughout, in everyday language:

| Term | Plain meaning |
|---|---|
| **Test oracle** | The thing that decides what "correct" means. Here, Microsoft Excel is the oracle. |
| **Golden master** (a.k.a. reference oracle) | The trusted source of truth we compare *against* — the live Excel workbook, driven through `xlwings`. |
| **System under test (SUT)** | The thing whose correctness we are checking — the Python computation. |
| **Absolute tolerance (`atol`)** | How close two numbers must be to count as "equal." We use `atol = 1e-6`, because Excel and Python can differ in the last few digits purely from floating-point rounding, not from a real bug. |

## Two harnesses

Run the **graph** differential before export to validate extraction fidelity.
Run the **exported-library** differential after export to validate the artifact
callers consume.

| Harness | SUT | When |
|---|---|---|
| [`differential_test_graph.py`](differential_test_graph.py) | In-memory `FormulaEvaluator` over the extracted graph | Before / alongside extraction review |
| [`differential_test_exported_library.py`](differential_test_exported_library.py) | Generated standalone package public API | After `uv run python -m src.extraction_pipeline` |

Both harnesses import shared scenario types from
[`differential_types.py`](differential_types.py) (`Scenario`, optional `Axis` /
`AxisPoint`, and `ATOL`). Golden-master cell reads go through
[`differential_excel.py`](differential_excel.py), which sets xlwings
`err_to_str=True` so Excel error cells (`#VALUE!`, `#N/A`, …) are returned as
strings rather than `None`. Workbook-specific hooks live at the bottom of each
harness module.

### Graph harness hooks

1. **`build_scenarios()`** or **`build_axes()`** — representative input combinations.
2. **`output_cell_labels()`** — mirror output bindings as `(label, address)` pairs.
3. **`inputs_for_excel()`** — map each scenario to Excel cell writes.

The graph harness also reports input cells absent from the extracted graph —
itself a differential signal about extraction coverage.

**Tiny DSA coverage:** 14 axes × 106 input points × 15 output cells.

### Exported-library harness hooks

1. **`build_scenarios()`** — representative input combinations.
2. **`output_cell_labels()`** — mirror output bindings as `(label, address)` pairs.
3. **`inputs_for_excel()`** / **`apply_inputs_to_mvp()`** — drive Excel and the exported `dist.tiny_dsa.api` package.

**Tiny DSA coverage:** 118 scenarios × 15 output cells = 1,770 comparisons.

**Why both matter:** a passing graph differential proves the *extraction* is faithful;
a passing exported-library differential proves the *code generation* on top of it is
faithful too. Each harness alone leaves a gap — the second closes the distance between
"the graph is right" and "the artifact callers consume is right."

Commit reference reports under `data/differential/graph/` and
`data/differential/exported_library/` after passing Windows sweeps. The export
step copies the exported-library harness, workbook fixture, and reports into
`dist/tests/`.

## Run

Microsoft Excel must be installed locally — `xlwings` drives it through COM automation.
`xlwings` is already in `pyproject.toml`'s dev dependencies.

```bash
# Graph oracle (extraction repo — run before export)
uv run python -m tests.differential.differential_test_graph

# With matched-error audit (passing error cells listed for scenario review)
uv run python -m tests.differential.differential_test_graph --warn-on-error-values

# Exported library (extraction repo, after export)
uv run python -m tests.differential.differential_test_exported_library

# Exported dist project (Windows + Excel)
uv run --project dist --group validation python -m tests.differential.differential_test_exported_library --layout exported
```

Pass `--warn-on-error-values` on either harness to list comparisons where both
oracles returned the same Excel error code. These still count as passes, but may
indicate unintended scenario setup unless the scenario sets
`expects_error_values=True`.

Exit codes: **`0`** all comparisons pass, **`1`** any failure, **`2`** prerequisite missing or scenarios not configured.

## Output locations

| Harness | Reports |
|---|---|
| Graph (extraction repo) | `data/differential/graph/differential_report.{csv,txt}` |
| Exported library (extraction repo) | `data/differential/exported_library/parity_report.{csv,txt}` |
| Exported dist (local rerun) | `dist/tests/results/local/` |
| Exported dist (shipped reference) | `dist/tests/results/reference/` |

Refresh committed reference reports whenever the workbook, bindings, constraints, or scenario sweep changes.

The `pytest` test suite also runs a small sample of differential tests with randomized inputs.
