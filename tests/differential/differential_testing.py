"""Differential test for tiny-dsa.xlsx.

Drives the same input configurations through two oracles and compares their
output cells:

    [golden] Microsoft Excel, automated via xlwings (the source of truth).
    [mvp]    excel-grapher, set up exactly as in `src/extraction_pipeline.py`:
             the imported `constraints` dict is passed to
             `DynamicRefConfig.from_constraints`, and a `FormulaEvaluator` is
             layered on top so we can actually read output cell values to
             compare. Because the constraints are imported (not redefined
             here), the differential stays in lockstep with the extraction
             pipeline as it evolves.

The 15 output cells (output_baseline / output_shocked / output_delta, 5 years
each) are compared at every input point. A text + CSV report is written to
`data/differential/graph/differential_report.{txt,csv}` at the project root.

Run from the project root:
    uv run python tests/differential/differential_testing.py

Requires Microsoft Excel installed locally (xlwings drives Excel via COM).
"""

from __future__ import annotations

import csv
import itertools
import math
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import fastpyxl
from excel_grapher import XlError
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
    create_dependency_graph,
)

# Ensure direct script execution can import the local `src` package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import the canonical configuration from workbook_config so the differential
# always tests the same shape the pipeline ships.
from src.pipeline_config import load_pipeline_config

_pipeline = load_pipeline_config(repo_root=PROJECT_ROOT)
PIPELINE_CONSTRAINTS = _pipeline.constraints
PIPELINE_TARGETS = list(_pipeline.targets)
PIPELINE_WORKBOOK_PATH = _pipeline.workbook_path

# ---- Configuration -----------------------------------------------------------

# extraction_pipeline.py declares Path("data/tiny-dsa.xlsx") relative to the
# project root (CWD-relative). Resolve it explicitly so this script can be
# launched from any working directory.
WORKBOOK_PATH = (
    PIPELINE_WORKBOOK_PATH
    if PIPELINE_WORKBOOK_PATH.is_absolute()
    else (PROJECT_ROOT / PIPELINE_WORKBOOK_PATH)
).resolve()

REPORT_DIR = PROJECT_ROOT / "data" / "differential" / "graph"
REPORT_TXT = REPORT_DIR / "differential_report.txt"
REPORT_CSV = REPORT_DIR / "differential_report.csv"

# Manifest reports golden values to ~6 decimals; relax atol so we don't trip
# on legitimate floating-point rounding between Excel and the evaluator.
ATOL = 1e-6

INPUT_NAMES: tuple[str, ...] = (
    "country_name",
    "growth_baseline",
    "interest_baseline",
    "primary_balance_baseline",
    "shock_year",
    "shock_type",
    "shock_table",
)
OUTPUT_NAMES: tuple[str, ...] = (
    "output_baseline",
    "output_shocked",
    "output_delta",
)


# ---- Inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    """One full Tiny-DSA input configuration."""

    country_name: str
    growth_baseline: tuple[float, ...]
    interest_baseline: tuple[float, ...]
    primary_balance_baseline: tuple[float, ...]
    shock_year: int
    shock_type: int
    shock_table: tuple[float, float, float]


# Borvelia canonical defaults from tiny-dsa-guide.md / the manifest.
CANONICAL = Inputs(
    country_name="Borvelia",
    growth_baseline=(3.5, 3.5, 3.5, 3.5, 3.5),
    interest_baseline=(4.0, 4.0, 4.0, 4.0, 4.0),
    primary_balance_baseline=(-1.0, -0.5, 0.0, 0.5, 1.0),
    shock_year=2,
    shock_type=1,
    shock_table=(-2.0, 2.0, -1.0),
)


# ---- Named-range resolution --------------------------------------------------


def _col_to_int(s: str) -> int:
    n = 0
    for c in s:
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n


def _int_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(r + ord("A")) + s
    return s


_A1_RE = re.compile(r"^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$")


def _expand_a1(sheet: str, a1: str) -> tuple[str, ...]:
    """Expand `Inputs`, `$C$16:$G$16` -> ('Inputs!C16', ..., 'Inputs!G16')."""
    m = _A1_RE.match(a1)
    if m is None:
        raise ValueError(f"unsupported A1 fragment: {a1!r}")
    c1 = _col_to_int(m.group(1))
    r1 = int(m.group(2))
    c2 = _col_to_int(m.group(3)) if m.group(3) else c1
    r2 = int(m.group(4)) if m.group(4) else r1
    return tuple(
        f"{sheet}!{_int_to_col(c)}{r}"
        for r in range(r1, r2 + 1)
        for c in range(c1, c2 + 1)
    )


def resolve_named_ranges(workbook_path: Path) -> dict[str, tuple[str, ...]]:
    """Return {name: (cell, ...)} for every input/output named range."""
    wb = fastpyxl.load_workbook(workbook_path, data_only=False)
    try:
        out: dict[str, tuple[str, ...]] = {}
        for name in INPUT_NAMES + OUTPUT_NAMES:
            defn = wb.defined_names.get(name)
            if defn is None:
                raise KeyError(f"defined name {name!r} not found in workbook")
            dests = list(defn.destinations)
            if len(dests) != 1:
                raise ValueError(
                    f"named range {name!r} has {len(dests)} destinations; "
                    f"only single-destination ranges are supported"
                )
            sheet, cells = dests[0]
            out[name] = _expand_a1(sheet, cells)
        return out
    finally:
        wb.close()


def materialize(inputs: Inputs, names: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Map an Inputs instance to a per-A1-cell {key: value} dict.

    Both drivers consume the same dict, so the materialization is the single
    source of truth for how named ranges are unrolled.
    """
    cells: dict[str, Any] = {
        names["country_name"][0]: inputs.country_name,
        names["shock_year"][0]: inputs.shock_year,
        names["shock_type"][0]: inputs.shock_type,
    }
    for nm, vec in (
        ("growth_baseline", inputs.growth_baseline),
        ("interest_baseline", inputs.interest_baseline),
        ("primary_balance_baseline", inputs.primary_balance_baseline),
        ("shock_table", inputs.shock_table),
    ):
        for cell, val in zip(names[nm], vec, strict=True):
            cells[cell] = val
    return cells


def output_targets(names: dict[str, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
    """Return ((label, cell), ...) for the 15 comparison cells."""
    return tuple(
        (f"{nm}[year={i}]", cell)
        for nm in OUTPUT_NAMES
        for i, cell in enumerate(names[nm], start=1)
    )


# ---- Axis specification ------------------------------------------------------


@dataclass(frozen=True)
class AxisPoint:
    label: str
    inputs: Inputs


@dataclass(frozen=True)
class Axis:
    name: str
    points: tuple[AxisPoint, ...]


def _override_year(vec: tuple[float, ...], year: int, val: float) -> tuple[float, ...]:
    """Replace year-N value (1-indexed) in a 5-vector, leaving the others alone."""
    return tuple(val if i == year - 1 else v for i, v in enumerate(vec))


def _build_axes() -> tuple[Axis, ...]:
    """Axes for the differential sweep.

    Three groups:

    - **single-axis isolation** — one parameter perturbed around CANONICAL.
      Cheap regression checks; surfaces "this axis broke" cleanly in the
      per-axis pass-rate table.

    - **categorical_combo** — full factorial over the three categorical
      axes (country × shock_type × shock_year) = 45 points. Covers every
      2- and 3-way interaction the single-axis sweeps miss.

    - **year-1 / year-5 continuous perturbations** — same shape as the
      year-3 sweeps, but anchored at the boundary years of the 5-year
      recursion. Year 1 is the first recursion step off the initial debt
      stock; year 5 is the terminal step. Either is a likely site for
      off-by-one or boundary-handling bugs that year-3-only sweeps miss.
    """

    def axis(name: str, points: list[AxisPoint]) -> Axis:
        return Axis(name=name, points=tuple(points))

    def _override_continuous(name: str, year: int, values: tuple[float, ...]) -> Axis:
        """Build an axis that perturbs one year of one continuous parameter.

        `name` is the manifest field name on Inputs (e.g. `growth_baseline`);
        `year` is 1-indexed; `values` are the perturbed values to sweep through.
        """
        attr = name  # field on Inputs dataclass
        base_vec: tuple[float, ...] = getattr(CANONICAL, attr)
        return axis(
            f"{name}[year={year}]",
            [
                AxisPoint(
                    f"{name}[year={year}]={v:+.1f}",
                    replace(CANONICAL, **{attr: _override_year(base_vec, year, v)}),
                )
                for v in values
            ],
        )

    country_axis = axis(
        "country_name",
        [
            AxisPoint(f"country_name={c}", replace(CANONICAL, country_name=c))
            for c in ("Borvelia", "Litellia", "Aurelium")
        ],
    )

    shock_year_axis = axis(
        "shock_year",
        [
            AxisPoint(f"shock_year={y}", replace(CANONICAL, shock_year=y))
            for y in (1, 2, 3, 4, 5)
        ],
    )

    shock_type_axis = axis(
        "shock_type",
        [
            AxisPoint(f"shock_type={t}", replace(CANONICAL, shock_type=t))
            for t in (1, 2, 3)
        ],
    )

    # Vary the growth-shock magnitude (the table entry actually applied at
    # canonical shock_type=1). Leaves interest/PB magnitudes alone.
    shock_growth_mag_axis = axis(
        "shock_table[growth]",
        [
            AxisPoint(
                f"shock_table[growth]={m:+.1f}",
                replace(
                    CANONICAL,
                    shock_table=(m, CANONICAL.shock_table[1], CANONICAL.shock_table[2]),
                ),
            )
            for m in (-3.0, -2.0, -1.0, 0.0, 1.0)
        ],
    )

    growth_values = (0.0, 1.5, 3.5, 5.5, 7.0)
    interest_values = (0.0, 2.0, 4.0, 6.0, 8.0)
    pb_values = (-3.0, -1.5, 0.0, 1.5, 3.0)

    # Boundary-year coverage (flaw 2): years 1 and 5 are the recursion's
    # endpoints. Year 3 already covered in the original sweep.
    continuous_axes = tuple(
        _override_continuous(name, year, values)
        for name, values in (
            ("growth_baseline", growth_values),
            ("interest_baseline", interest_values),
            ("primary_balance_baseline", pb_values),
        )
        for year in (1, 3, 5)
    )

    # Full factorial over the three categorical axes (flaw 1): covers every
    # 2- and 3-way interaction the single-axis sweeps cannot reach.
    # 3 countries × 3 shock types × 5 shock years = 45 points.
    categorical_combo_axis = axis(
        "categorical_combo: country x shock_type x shock_year",
        [
            AxisPoint(
                f"country={c}, shock_type={t}, shock_year={y}",
                replace(CANONICAL, country_name=c, shock_type=t, shock_year=y),
            )
            for c, t, y in itertools.product(
                ("Borvelia", "Litellia", "Aurelium"),
                (1, 2, 3),
                (1, 2, 3, 4, 5),
            )
        ],
    )

    return (
        country_axis,
        shock_year_axis,
        shock_type_axis,
        shock_growth_mag_axis,
        *continuous_axes,
        categorical_combo_axis,
    )


# ---- Comparison --------------------------------------------------------------


@dataclass(frozen=True)
class Trial:
    """One golden-vs-mvp comparison at one (axis, point, output cell)."""

    axis: str
    point_label: str
    output_label: str
    cell: str
    golden: Any
    mvp: Any
    match: bool
    abs_diff: float | None
    rel_diff: float | None
    note: str = ""


def _coerce_excel_error(value: Any) -> Any:
    """xlwings sometimes surfaces error cells as strings like '#DIV/0!'.
    Coerce those to XlError so comparison treats them like the evaluator's errors.
    """
    if isinstance(value, str):
        err = XlError.from_text(value)
        if err is not None:
            return err
    return value


def values_match(
    g: Any, m: Any, atol: float
) -> tuple[bool, float | None, float | None, str]:
    """Compare golden vs mvp. Returns (match, abs_diff, rel_diff, note)."""
    g = _coerce_excel_error(g)
    m = _coerce_excel_error(m)

    if isinstance(g, XlError) or isinstance(m, XlError):
        if isinstance(g, XlError) and isinstance(m, XlError) and g == m:
            return True, None, None, f"both error: {g}"
        return False, None, None, f"error mismatch (golden={g!r}, mvp={m!r})"

    if g is None and m is None:
        return True, None, None, "both None"
    if g is None or m is None:
        return False, None, None, "one side None"

    if isinstance(g, bool) and isinstance(m, bool):
        return (g == m), None, None, "bool"

    if isinstance(g, int | float) and isinstance(m, int | float):
        gf, mf = float(g), float(m)
        if math.isnan(gf) and math.isnan(mf):
            return True, None, None, "both NaN"
        abs_d = abs(gf - mf)
        rel_d = abs_d / max(abs(gf), abs(mf), 1e-15)
        return (abs_d <= atol), abs_d, rel_d, ""

    return (g == m), None, None, "exact"


# ---- Drivers -----------------------------------------------------------------


class GoldenDriver:
    """Hidden xlwings Excel driver against a temp copy of the workbook."""

    def __init__(self, workbook_path: Path) -> None:
        import xlwings as xw

        self._tmpdir = Path(tempfile.mkdtemp(prefix="tiny_dsa_diff_"))
        self._tmp_wb = self._tmpdir / workbook_path.name
        shutil.copy2(workbook_path, self._tmp_wb)

        self._app = xw.App(visible=False, add_book=False)
        self._app.display_alerts = False
        self._app.screen_updating = False
        self._book = self._app.books.open(str(self._tmp_wb))

    def set_inputs(self, inputs: dict[str, Any]) -> None:
        for key, val in inputs.items():
            sheet, addr = key.split("!", 1)
            self._book.sheets[sheet].range(addr).value = val
        self._app.calculate()

    def read(self, cell: str) -> Any:
        sheet, addr = cell.split("!", 1)
        return self._book.sheets[sheet].range(addr).value

    def close(self) -> None:
        try:
            self._book.close()
        finally:
            try:
                self._app.quit()
            finally:
                shutil.rmtree(self._tmpdir, ignore_errors=True)


class MvpOracleDriver:
    """excel-grapher driver set up from `src/extraction_pipeline.py`'s exported
    constraints, plus a FormulaEvaluator on top so output cells can be read.

    A fresh graph is built here (rather than reusing the module-level one
    from extraction_pipeline) so set_node_value mutations during the sweep
    don't leak into any other consumer that imports the pipeline.

    Cells absent from the graph are skipped during `set_inputs` and recorded
    in `missing_cells`; any resulting divergence between the MVP and the
    golden Excel oracle is exactly the kind of finding this differential is
    meant to surface.
    """

    def __init__(self, workbook_path: Path, targets: list[str]) -> None:
        config = DynamicRefConfig.from_constraints(PIPELINE_CONSTRAINTS, {})
        self._graph: DependencyGraph = create_dependency_graph(
            workbook_path,
            targets,
            load_values=True,
            dynamic_refs=config,
        )
        self._evaluator = FormulaEvaluator(self._graph)
        self._known_keys = frozenset(self._graph.leaf_keys()) | frozenset(
            self._graph.formula_keys()
        )
        self.missing_cells: set[str] = set()

    def set_inputs(self, inputs: dict[str, Any]) -> None:
        for key, val in inputs.items():
            if key in self._known_keys:
                self._graph.set_node_value(key, val)
            else:
                self.missing_cells.add(key)

    def read(self, cell: str) -> Any:
        return self._evaluator.evaluate(cell)


# ---- Sweep -------------------------------------------------------------------


def run_sweep() -> tuple[list[Trial], list[str]]:
    names = resolve_named_ranges(WORKBOOK_PATH)
    targets = output_targets(names)
    axes = _build_axes()

    total_points = sum(len(a.points) for a in axes)
    print(
        f"running {len(axes)} axes, {total_points} points, "
        f"{total_points * len(targets)} cell comparisons"
    )
    for a in axes:
        print(f"  - {a.name}: {len(a.points)} point(s)")

    trials: list[Trial] = []
    golden = GoldenDriver(WORKBOOK_PATH)
    mvp = MvpOracleDriver(WORKBOOK_PATH, list(PIPELINE_TARGETS))
    all_input_cells = {
        cell
        for axis in axes
        for point in axis.points
        for cell in materialize(point.inputs, names)
    }
    missing_inputs_in_graph = sorted(all_input_cells - mvp._known_keys)
    if missing_inputs_in_graph:
        print(
            "  ! mvp graph is missing these input cells "
            f"(set_inputs will skip them): {missing_inputs_in_graph}"
        )
    try:
        for axis in axes:
            for point in axis.points:
                cells_in = materialize(point.inputs, names)
                golden.set_inputs(cells_in)
                mvp.set_inputs(cells_in)
                for output_label, cell in targets:
                    g_val = golden.read(cell)
                    try:
                        m_val: Any = mvp.read(cell)
                    except Exception as exc:  # noqa: BLE001
                        m_val = f"<{type(exc).__name__}: {exc}>"
                        match, abs_d, rel_d, note = False, None, None, "mvp raised"
                    else:
                        match, abs_d, rel_d, note = values_match(g_val, m_val, ATOL)
                    trials.append(
                        Trial(
                            axis=axis.name,
                            point_label=point.label,
                            output_label=output_label,
                            cell=cell,
                            golden=g_val,
                            mvp=m_val,
                            match=match,
                            abs_diff=abs_d,
                            rel_diff=rel_d,
                            note=note,
                        )
                    )
    finally:
        golden.close()
    return trials, missing_inputs_in_graph


# ---- Report ------------------------------------------------------------------


def _short_addr(key: str) -> str:
    return key.split("!", 1)[1] if "!" in key else key


def _format_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6f}"
    return repr(v)


def write_reports(trials: list[Trial], missing_inputs_in_graph: list[str]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "axis",
                "point",
                "output",
                "cell",
                "golden",
                "mvp",
                "match",
                "abs_diff",
                "rel_diff",
                "note",
            ]
        )
        for t in trials:
            w.writerow(
                [
                    t.axis,
                    t.point_label,
                    t.output_label,
                    _short_addr(t.cell),
                    _format_value(t.golden),
                    _format_value(t.mvp),
                    t.match,
                    "" if t.abs_diff is None else f"{t.abs_diff:.3e}",
                    "" if t.rel_diff is None else f"{t.rel_diff:.3e}",
                    t.note,
                ]
            )

    total = len(trials)
    passed = sum(1 for t in trials if t.match)
    failed = total - passed
    rate = (passed / total * 100.0) if total else 0.0

    by_axis: dict[str, tuple[int, int]] = {}
    for t in trials:
        p, n = by_axis.get(t.axis, (0, 0))
        by_axis[t.axis] = (p + int(t.match), n + 1)

    by_point: dict[tuple[str, str], tuple[int, int]] = {}
    for t in trials:
        key = (t.axis, t.point_label)
        p, n = by_point.get(key, (0, 0))
        by_point[key] = (p + int(t.match), n + 1)

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("tiny-dsa differential parity report")
    lines.append("=" * 78)
    lines.append(f"workbook : {WORKBOOK_PATH}")
    lines.append(
        "oracles  : Excel via xlwings [golden] vs "
        "excel-grapher graph + FormulaEvaluator [mvp]"
    )
    lines.append(f"tolerance: atol={ATOL:g}")
    lines.append(f"trials   : {total} cell-level comparisons")
    if missing_inputs_in_graph:
        lines.append(
            f"warning  : {len(missing_inputs_in_graph)} input cell(s) absent from the "
            "mvp graph; set_inputs skipped them. This is itself a differential signal "
            "about the extraction pipeline. See ABSENT INPUTS below."
        )
    lines.append("")
    lines.append(f"SUMMARY  : {passed}/{total} passed ({rate:.2f}%), {failed} failed")
    lines.append("")
    if missing_inputs_in_graph:
        lines.append(
            "ABSENT INPUTS (cells the differential tried to set but the mvp graph lacks)"
        )
        lines.append("-" * 78)
        for cell in missing_inputs_in_graph:
            lines.append(f"  - {cell}")
        lines.append("")
    lines.append("PER-AXIS")
    lines.append("-" * 78)
    for axis_name, (p, n) in by_axis.items():
        flag = "[PASS]" if p == n else "[FAIL]"
        lines.append(f"{flag} {p:5d}/{n:<5d}  {axis_name}")

    lines.append("")
    lines.append("PER-POINT")
    lines.append("-" * 78)
    for (axis_name, pt), (p, n) in by_point.items():
        flag = "[PASS]" if p == n else "[FAIL]"
        lines.append(f"{flag} {p:3d}/{n:<3d}  {axis_name} :: {pt}")

    fails = [t for t in trials if not t.match]
    if fails:
        fails.sort(key=lambda t: t.abs_diff or 0.0, reverse=True)
        lines.append("")
        lines.append(f"FAILURES (top {min(50, len(fails))} by abs_diff)")
        lines.append("-" * 78)
        for t in fails[:50]:
            abs_str = "" if t.abs_diff is None else f"  Δ={t.abs_diff:.3e}"
            block = [
                f"{t.axis} :: {t.point_label}",
                f"  output: {t.output_label}  ({_short_addr(t.cell)})",
                f"  golden = {_format_value(t.golden)}",
                f"  mvp    = {_format_value(t.mvp)}{abs_str}",
            ]
            if t.note:
                block.append(f"  note  : {t.note}")
            lines.append("\n".join(block))
        if len(fails) > 50:
            lines.append(f"... and {len(fails) - 50} more")

    text = "\n".join(lines) + "\n"
    REPORT_TXT.write_text(text, encoding="utf-8")
    print(text)


# ---- Main --------------------------------------------------------------------


def main() -> int:
    if not WORKBOOK_PATH.exists():
        print(f"workbook not found: {WORKBOOK_PATH}", file=sys.stderr)
        return 2

    try:
        import xlwings  # noqa: F401
    except ImportError:
        print(
            "xlwings is not installed; install it via `uv add --dev xlwings`",
            file=sys.stderr,
        )
        return 2

    trials, missing_inputs_in_graph = run_sweep()
    write_reports(trials, missing_inputs_in_graph)
    return 0 if all(t.match for t in trials) else 1


if __name__ == "__main__":
    raise SystemExit(main())
