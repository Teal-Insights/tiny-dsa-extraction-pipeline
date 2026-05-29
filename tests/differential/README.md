# Differential testing

Two harnesses live here, both comparing Tiny-DSA outputs against Microsoft Excel via xlwings at `atol = 1e-6`:

- [`differential_testing.py`](differential_testing.py) — MVP oracle: `excel-grapher` + `FormulaEvaluator`. 14 axes × 106 input points × 15 cells. Single-axis isolation around the canonical scenario (categorical axes + continuous perturbations at years 1 / 3 / 5) plus the full categorical combo of country × shock_type × shock_year (45 points).
- [`differential_test_exported_library.py`](differential_test_exported_library.py) — MVP oracle: the exported standalone package at `dist/`, imported as `dist.api`. 118 scenarios × 15 cells = 1,770 comparisons. Canonical sweep + single-axis isolation + the same 45-point categorical combo; no access to the Excel file at runtime.

Both exit `0` on pass, `1` on any failure, `2` on a missing prerequisite. Microsoft Excel must be installed locally; `xlwings` is in `pyproject.toml`'s dev dependencies.

## Run

```pwsh
uv run python tests/differential/differential_testing.py
uv run python tests/differential/differential_test_exported_library.py
```

## Output

- `differential_testing.py` writes [`data/differential/differential_report.{txt,csv}`](../../data/differential/) — summary, per-axis pass rates, per-point pass rates, top failures. Any input cell the differential tries to set that is missing from the mvp graph appears under **ABSENT INPUTS** at the top of the report.
- `differential_test_exported_library.py` writes [`data/differential/exported_library/parity_report.{txt,csv}`](../../data/differential/exported_library/) — summary, first divergence, full failure list. Pre-flight verifies path existence, workbook-vs-`dist/data.py` staleness, and that each setter/compute binding table matches the script's hardcoded cells.
