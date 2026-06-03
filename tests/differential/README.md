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

## The two harnesses (and why there are two)

Both scripts use Excel (via `xlwings`) as the golden-master oracle and compare at
`atol = 1e-6`. They differ in *which* Python implementation they put under test:

### 1. `differential_testing.py` — validates the **dependency graph + evaluator**

Puts the in-memory `excel-grapher` dependency graph, evaluated by `FormulaEvaluator`,
under test. It reuses the *exact* `constraints` dictionary that
[`src/extraction_pipeline.py`](../../src/extraction_pipeline.py) ships, so the test
stays in lockstep with the extraction pipeline as it evolves rather than re-declaring
its own copy. This answers the question: *does the extracted formula graph compute the
same numbers as the spreadsheet?*

Coverage: **14 axes × 106 input points × 15 output cells**. Inputs that the test tries
to set but that are absent from the graph are surfaced explicitly (see **Output**) —
a missing input is itself a finding about the extraction.

### 2. `differential_test_exported_library.py` — validates the **shipped package**

Puts the generated standalone package in [`dist/`](../../dist/) under test, imported as
`dist.api` and exercised *only* through its public Records-shaped API
(`set_*` / `compute_*`) — the same surface a real consumer of `py-tiny-dsa` would call.
The package never touches the workbook at runtime; all model constants are baked into
its `data.py` snapshot. This answers a stronger question: *does the code we actually
hand to users compute the same numbers as the spreadsheet?*

Coverage: **118 scenarios × 15 output cells = 1,770 comparisons**.

**Why both matter:** a passing graph differential proves the *extraction* is faithful;
a passing exported-library differential proves the *code generation* on top of it is
faithful too. Each harness alone leaves a gap — the second closes the distance between
"the graph is right" and "the artifact callers consume is right."

## Run

Microsoft Excel must be installed locally — `xlwings` drives it through COM automation.
`xlwings` is already in `pyproject.toml`'s dev dependencies.

```pwsh
# Validate the dependency graph + evaluator against Excel
uv run python tests/differential/differential_testing.py

# Validate the shipped package against Excel
#   Regenerate the package first if it is missing or stale:
#   uv run python src/extraction_pipeline.py
uv run python tests/differential/differential_test_exported_library.py
```

Each script exits **`0`** when every comparison passes, **`1`** when any comparison
fails, and **`2`** when a prerequisite is missing (no workbook, no Excel, package not
yet generated). The acceptance bar is 100%: any single mismatch is a failure.

## Output

Both harnesses write a **parity report** in two formats — a machine-readable CSV (one
row per cell comparison) and a human-readable TXT summary:

- `differential_testing.py` → [`data/differential/differential_report.{txt,csv}`](../../data/differential/).
  The TXT contains the headline pass/fail summary, **per-axis** and **per-point**
  pass-rate tables, and the top failures ranked by absolute difference. Any input cell
  the test tried to set but that is absent from the graph is listed under **ABSENT
  INPUTS** at the top — itself a differential signal about the extraction.
- `differential_test_exported_library.py` → [`data/differential/exported_library/parity_report.{txt,csv}`](../../data/differential/exported_library/).
  The TXT contains a timestamped header (workbook + package paths, tolerance), aggregate
  counts, the **first divergence** (scenario + cell), and the complete list of any
  failing comparisons.

> **Keep reports current.** A parity report is only meaningful for the workbook,
> constraints, and sweep definition that produced it. If you change the workbook,
> `src/extraction_pipeline.py`'s `constraints`, or a harness's axis definitions,
> re-run the affected sweep and commit the refreshed report — a stale report silently
> describes a version of the model that no longer exists.
