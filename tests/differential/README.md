# Differential testing

[`differential_testing.py`](differential_testing.py) compares two oracles for
`data/tiny-dsa.xlsx`:

- **golden** — Microsoft Excel, driven via xlwings.
- **mvp** — `excel-grapher` set up from the constraints exported by
  [`src/extraction_pipeline.py`](../../src/extraction_pipeline.py), with a
  `FormulaEvaluator` layered on top so output cells can actually be read.

The script sweeps **7 axes × 31 input points** (country, shock year, shock
type, shock magnitude, and year-3 perturbations of growth / interest /
primary balance) and compares the **15 output cells** at every point. Any
divergence is logged to the report.

## Requirements

- Microsoft Excel installed locally (xlwings drives Excel via COM).
- `xlwings` in the dev dependency group (already in `pyproject.toml`).
- Run from the **project root** so the `from src.extraction_pipeline import ...`
  resolution and `data/tiny-dsa.xlsx` path work.

## Run

```pwsh
uv run python tests/differential/differential_testing.py
```

Exit codes:

- `0` — every comparison matched within `atol = 1e-6`.
- `1` — at least one comparison failed (or the mvp raised). Inspect the report.
- `2` — workbook missing or `xlwings` not installed.

## Output

Two files are written to [`data/differential/`](../../data/differential/) at the project root (the directory is created on first run):

- `differential_report.txt` — human-readable: summary, per-axis pass rates,
  per-point pass rates, and the top failures ranked by absolute difference.
- `differential_report.csv` — one row per comparison, for spreadsheet
  drill-down.

If any input cells the differential tries to set are missing from the mvp
graph, they're listed under an **ABSENT INPUTS** section at the top of the
report — that's itself a differential signal about the extraction pipeline's
coverage of dynamic-ref branches.

## Known upstream issues

- [`playground/formula_evaluator_unqualified_ref_bug.md`](../../playground/formula_evaluator_unqualified_ref_bug.md)
  documents a `FormulaEvaluator` reference-resolution bug that currently
  causes every mvp read to fail with
  `KeyError: 'Cell Inputs!C10 not found in graph'`. Until that's fixed
  upstream, the report's failure section will be dominated by that single
  cause.
