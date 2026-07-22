"""Graph-oracle differential test harness.

Compare Microsoft Excel (golden master via ``xlwings``) against an in-memory
``excel-grapher`` dependency graph evaluated with ``FormulaEvaluator``. The
graph is built from the same ``constraints`` and ``TARGETS`` declared in
``workbook_config.py``, so this differential stays in lockstep with extraction
as the pipeline evolves.

Address keys: ``excel-grapher`` stores sheet-qualified addresses in canonical
form (e.g. ``'Discrete Risks'!H2``). Human-authored scenario matrices and
bindings may use unquoted spellings (``Discrete Risks!H2``). Harness drivers
normalize with ``normalize_key`` before graph lookup and use
``parse_address(normalize_key(...))`` for xlwings/COM writes — never
``split("!", 1)`` on sheet-qualified addresses.

Workbook-specific scenario definitions live in the ``Workbook-specific hooks``
section at the bottom of this module.

Run from the extraction repo after configuring hooks::

    uv run python -m tests.differential.differential_test_graph

Requires Microsoft Excel installed locally (``xlwings`` drives Excel via COM).
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .differential_excel import (
    coerce_excel_error,
    matched_error_values,
    parity_exit_code,
    read_cell_value,
)
from .differential_scenario_inputs import collect_scenario_input_addresses
from .differential_types import ATOL, Axis, AxisPoint, Scenario

from excel_grapher import XlError
from excel_grapher.core.address_keys import normalize_key, parse_address
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
    create_dependency_graph,
)

logger = logging.getLogger(__name__)

LayoutName = Literal["repo"]


@dataclass(frozen=True)
class GraphDifferentialConfig:
    """Runtime paths and extraction settings for one graph differential run."""

    repo_root: Path
    workbook_path: Path
    report_dir: Path
    targets: tuple[str, ...]
    constraints: dict[str, object]
    library_name: str
    atol: float = ATOL
    allow_matched_errors: bool = False


@dataclass(frozen=True)
class Trial:
    """One golden-vs-graph comparison at one (axis, point, output cell)."""

    axis: str
    point_label: str
    scenario_id: str
    output_label: str
    cell: str
    golden: Any
    mvp: Any
    match: bool
    abs_diff: float | None
    rel_diff: float | None
    note: str = ""
    matched_error: bool = False
    flagged_matched_error: bool = False


CSV_COLUMNS: tuple[str, ...] = (
    "axis",
    "point",
    "scenario_id",
    "output",
    "cell",
    "golden",
    "mvp",
    "match",
    "abs_diff",
    "rel_diff",
    "note",
    "matched_error",
    "flagged_matched_error",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Differential test for the extracted dependency graph.",
    )
    parser.add_argument(
        "--layout",
        choices=("repo",),
        default="repo",
        help="Path preset: extraction repo (default).",
    )
    parser.add_argument(
        "--workbook-path",
        type=Path,
        default=None,
        help="Override workbook path (defaults depend on --layout).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory for differential_report.{csv,txt} output.",
    )
    parser.add_argument(
        "--allow-matched-errors",
        action="store_true",
        help=(
            "Triage escape hatch: do not fail the run when both oracles return "
            "the same Excel error on a scenario without expects_error_values=True. "
            "Flagged comparisons are still listed in the report."
        ),
    )
    return parser.parse_args(argv)


def _project_root_from_module(module_path: Path) -> Path:
    """Return the extraction repo root from ``tests/differential/<module>.py``."""
    return module_path.resolve().parents[2]


def resolve_config(
    *,
    module_path: Path,
    layout: LayoutName,
    workbook_path: Path | None = None,
    report_dir: Path | None = None,
    allow_matched_errors: bool = False,
) -> GraphDifferentialConfig:
    """Resolve paths from ``workbook_config.py`` and the selected layout."""
    module_path = module_path.resolve()
    repo_root = _project_root_from_module(module_path)

    from src.pipeline_config import load_pipeline_config

    pipeline = load_pipeline_config(repo_root=repo_root)
    resolved_workbook = (
        pipeline.workbook_path
        if pipeline.workbook_path.is_absolute()
        else repo_root / pipeline.workbook_path
    ).resolve()

    defaults = GraphDifferentialConfig(
        repo_root=repo_root,
        workbook_path=resolved_workbook,
        report_dir=repo_root / pipeline.differential_graph_report_dir_rel,
        targets=pipeline.targets,
        constraints=pipeline.constraints,
        library_name=pipeline.dist_metadata.library_name,
    )

    return GraphDifferentialConfig(
        repo_root=repo_root,
        workbook_path=(workbook_path or defaults.workbook_path).resolve(),
        report_dir=(report_dir or defaults.report_dir).resolve(),
        targets=defaults.targets,
        constraints=defaults.constraints,
        library_name=defaults.library_name,
        atol=ATOL,
        allow_matched_errors=allow_matched_errors,
    )


def config_from_args(
    module_path: Path, args: argparse.Namespace
) -> GraphDifferentialConfig:
    return resolve_config(
        module_path=module_path,
        layout=args.layout,
        workbook_path=args.workbook_path,
        report_dir=args.report_dir,
        allow_matched_errors=args.allow_matched_errors,
    )


def values_match(
    golden: Any, mvp: Any, *, atol: float
) -> tuple[bool, float | None, float | None, str]:
    """Compare golden vs graph oracle. Returns (match, abs_diff, rel_diff, note)."""
    golden = coerce_excel_error(golden)
    mvp = coerce_excel_error(mvp)

    if isinstance(golden, XlError) or isinstance(mvp, XlError):
        if isinstance(golden, XlError) and isinstance(mvp, XlError) and golden == mvp:
            return True, None, None, f"both error: {golden}"
        return False, None, None, f"error mismatch (golden={golden!r}, mvp={mvp!r})"

    if golden is None and mvp is None:
        return True, None, None, "both None"
    if golden is None or mvp is None:
        return False, None, None, "one side None"

    if isinstance(golden, bool) and isinstance(mvp, bool):
        return (golden == mvp), None, None, "bool"

    if isinstance(golden, int | float) and isinstance(mvp, int | float):
        golden_f, mvp_f = float(golden), float(mvp)
        if math.isnan(golden_f) and math.isnan(mvp_f):
            return True, None, None, "both NaN"
        abs_diff = abs(golden_f - mvp_f)
        rel_diff = abs_diff / max(abs(golden_f), abs(mvp_f), 1e-15)
        return (abs_diff <= atol), abs_diff, rel_diff, ""

    return (golden == mvp), None, None, "exact"


class GoldenDriver:
    """Hidden xlwings Excel driver against a temp copy of the workbook."""

    def __init__(self, workbook_path: Path) -> None:
        import xlwings as xw

        self._tmpdir = Path(tempfile.mkdtemp(prefix="graph_diff_"))
        self._tmp_wb = self._tmpdir / workbook_path.name
        shutil.copy2(workbook_path, self._tmp_wb)

        self._app = xw.App(visible=False, add_book=False)
        self._app.display_alerts = False
        self._app.screen_updating = False
        self._book = self._app.books.open(str(self._tmp_wb))
        self._input_baselines: dict[str, Any] = {}

    def record_input_baselines(self, cells: frozenset[str]) -> None:
        """Snapshot baseline values for the union of all scenario input cells."""
        self._input_baselines = {cell: self.read(cell) for cell in cells}

    def reset_inputs(self) -> None:
        """Restore scenario input cells to values captured at sweep start."""
        for key, value in self._input_baselines.items():
            sheet, addr = parse_address(normalize_key(key))
            self._book.sheets[sheet].range(addr).value = value
        if self._input_baselines:
            self._app.calculate()

    def set_inputs(self, inputs: dict[str, Any]) -> None:
        for key, value in inputs.items():
            sheet, addr = parse_address(normalize_key(key))
            self._book.sheets[sheet].range(addr).value = value
        self._app.calculate()

    def read(self, cell: str) -> Any:
        return read_cell_value(self._book.sheets, cell)

    def close(self) -> None:
        try:
            self._book.close()
        finally:
            try:
                self._app.quit()
            finally:
                shutil.rmtree(self._tmpdir, ignore_errors=True)


class MvpGraphDriver:
    """In-memory graph driver built from ``workbook_config`` constraints."""

    def __init__(
        self,
        workbook_path: Path,
        *,
        targets: tuple[str, ...],
        constraints: dict[str, object],
    ) -> None:
        config = DynamicRefConfig.from_constraints(constraints, {})
        self._graph: DependencyGraph = create_dependency_graph(
            workbook_path,
            list(targets),
            load_values=True,
            dynamic_refs=config,
        )
        self._evaluator = FormulaEvaluator(self._graph)
        self._known_keys = frozenset(self._graph.leaf_keys()) | frozenset(
            self._graph.formula_keys()
        )
        self.missing_cells: set[str] = set()
        self._input_baselines: dict[str, Any] = {}

    def record_input_baselines(self, cells: frozenset[str]) -> None:
        """Snapshot baseline values for the union of all scenario input cells."""
        self._input_baselines = {
            normalize_key(cell): node.value
            for cell in cells
            if normalize_key(cell) in self._known_keys
            if (node := self._graph.get_node(normalize_key(cell))) is not None
        }

    def reset_inputs(self) -> None:
        """Restore scenario input cells to values captured at sweep start."""
        for key, value in self._input_baselines.items():
            self._graph.set_node_value(key, value)

    def set_inputs(self, inputs: dict[str, Any]) -> None:
        for key, value in inputs.items():
            canonical = normalize_key(key)
            if canonical in self._known_keys:
                self._graph.set_node_value(canonical, value)
            else:
                self.missing_cells.add(key)

    def read(self, cell: str) -> Any:
        return self._evaluator.evaluate(normalize_key(cell))


def _resolve_axes() -> tuple[Axis, ...]:
    axes = build_axes()
    if axes:
        return axes
    scenarios = build_scenarios()
    if not scenarios:
        return ()
    return (
        Axis(
            name="scenarios",
            points=tuple(
                AxisPoint(label=scenario.id, scenario=scenario)
                for scenario in scenarios
            ),
        ),
    )


def _validate_workbook_hooks() -> None:
    axes = _resolve_axes()
    if not axes:
        raise RuntimeError(
            "No differential scenarios configured. Author build_scenarios() or "
            "build_axes(), output_cell_labels(), and inputs_for_excel() in "
            "tests/differential/differential_test_graph.py."
        )
    if not output_cell_labels():
        raise RuntimeError(
            "output_cell_labels() returned no cells. Mirror your output bindings "
            "as (label, address) pairs."
        )


def _verify_paths(config: GraphDifferentialConfig) -> None:
    if not config.workbook_path.is_file():
        raise FileNotFoundError(
            f"Workbook not found: {config.workbook_path}. "
            "Populate data/ and workbook_config.py before running graph parity tests."
        )
    if not config.targets:
        raise FileNotFoundError(
            "workbook_config.TARGETS is empty. Declare extraction targets before "
            "running the graph differential."
        )
    if not config.constraints:
        raise FileNotFoundError(
            "workbook_config.CONSTRAINTS is empty. Classify graph leaves before "
            "running the graph differential."
        )


def _short_addr(key: str) -> str:
    return key.split("!", 1)[1] if "!" in key else key


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return repr(value)


def write_csv_report(trials: list[Trial], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for trial in trials:
            writer.writerow(
                [
                    trial.axis,
                    trial.point_label,
                    trial.scenario_id,
                    trial.output_label,
                    _short_addr(trial.cell),
                    _format_value(trial.golden),
                    _format_value(trial.mvp),
                    trial.match,
                    "" if trial.abs_diff is None else f"{trial.abs_diff:.3e}",
                    "" if trial.rel_diff is None else f"{trial.rel_diff:.3e}",
                    trial.note,
                    trial.matched_error,
                    trial.flagged_matched_error,
                ]
            )


def write_txt_summary(
    trials: list[Trial],
    path: Path,
    *,
    config: GraphDifferentialConfig,
    missing_inputs_in_graph: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = len(trials)
    passed = sum(1 for trial in trials if trial.match)
    failed = total - passed
    pass_rate = (100.0 * passed / total) if total else 0.0
    flagged = [trial for trial in trials if trial.flagged_matched_error]
    failing_run = bool(failed) or bool(flagged and not config.allow_matched_errors)
    result = "FAIL" if failing_run else "PASS"
    first = next((trial for trial in trials if not trial.match), None)

    by_axis: dict[str, tuple[int, int]] = {}
    for trial in trials:
        axis_passed, axis_total = by_axis.get(trial.axis, (0, 0))
        by_axis[trial.axis] = (axis_passed + int(trial.match), axis_total + 1)

    by_point: dict[tuple[str, str], tuple[int, int]] = {}
    for trial in trials:
        key = (trial.axis, trial.point_label)
        point_passed, point_total = by_point.get(key, (0, 0))
        by_point[key] = (point_passed + int(trial.match), point_total + 1)

    lines: list[str] = []
    lines.append(
        f"Parity report: extracted dependency graph vs Excel ({config.library_name})"
    )
    lines.append(
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    lines.append(f"Workbook:  {config.workbook_path}")
    lines.append(
        "Oracles:   Excel via xlwings [golden] vs "
        "excel-grapher graph + FormulaEvaluator [mvp]"
    )
    lines.append(f"Tolerance: atol = {config.atol:g}")
    lines.append(f"Trials:    {total} cell-level comparisons")
    if missing_inputs_in_graph:
        lines.append(
            f"Warning:   {len(missing_inputs_in_graph)} input cell(s) absent from the "
            "mvp graph; set_inputs skipped them. See ABSENT INPUTS below."
        )
    lines.append("")
    lines.append(f"Total comparisons: {total}")
    lines.append(f"Passed:            {passed}")
    lines.append(f"Failed:            {failed}")
    if flagged:
        allowed_note = (
            " (allowed by --allow-matched-errors)"
            if (config.allow_matched_errors)
            else " (fails the run)"
        )
        lines.append(f"Matched errors:    {len(flagged)} flagged{allowed_note}")
    lines.append(f"Pass rate:         {pass_rate:.2f}%")
    lines.append("Acceptance bar:    100.00%")
    lines.append(f"Result:            {result}")
    if first is not None:
        lines.append("")
        lines.append("First divergence:")
        lines.append(f"  scenario:  {first.scenario_id}")
        lines.append(f"  axis:      {first.axis} :: {first.point_label}")
        lines.append(f"  cell:      {first.cell}  ({first.output_label})")
        lines.append(f"  golden:    {_format_value(first.golden)}")
        lines.append(f"  mvp:       {_format_value(first.mvp)}")
        if first.abs_diff is not None:
            lines.append(f"  abs_diff:  {first.abs_diff:.3e}")
        if first.rel_diff is not None:
            lines.append(f"  rel_diff:  {first.rel_diff:.3e}")
        if first.note:
            lines.append(f"  note:      {first.note}")

    if flagged:
        lines.append("")
        lines.append(
            "MATCHED ERROR VALUES (both oracles returned the same Excel error; "
            "fails the run unless the scenario sets expects_error_values=True "
            "or --allow-matched-errors is passed)"
        )
        lines.append("-" * 78)
        for trial in flagged:
            lines.append(
                f"  {trial.scenario_id} :: {trial.cell} ({trial.output_label})"
            )
            lines.append(f"    golden = {_format_value(trial.golden)}")
            lines.append(f"    mvp    = {_format_value(trial.mvp)}")
            if trial.note:
                lines.append(f"    note   = {trial.note}")

    if missing_inputs_in_graph:
        lines.append("")
        lines.append(
            "ABSENT INPUTS (cells the differential tried to set but the mvp graph lacks)"
        )
        lines.append("-" * 78)
        for cell in missing_inputs_in_graph:
            lines.append(f"  - {cell}")

    lines.append("")
    lines.append("PER-AXIS")
    lines.append("-" * 78)
    for axis_name, (axis_passed, axis_total) in by_axis.items():
        flag = "[PASS]" if axis_passed == axis_total else "[FAIL]"
        lines.append(f"{flag} {axis_passed:5d}/{axis_total:<5d}  {axis_name}")

    lines.append("")
    lines.append("PER-POINT")
    lines.append("-" * 78)
    for (axis_name, point_label), (point_passed, point_total) in by_point.items():
        flag = "[PASS]" if point_passed == point_total else "[FAIL]"
        lines.append(
            f"{flag} {point_passed:3d}/{point_total:<3d}  {axis_name} :: {point_label}"
        )

    fails = [trial for trial in trials if not trial.match]
    if fails:
        fails.sort(key=lambda trial: trial.abs_diff or 0.0, reverse=True)
        lines.append("")
        lines.append(f"FAILURES (top {min(50, len(fails))} by abs_diff)")
        lines.append("-" * 78)
        for trial in fails[:50]:
            abs_str = "" if trial.abs_diff is None else f"  Δ={trial.abs_diff:.3e}"
            block = [
                f"{trial.axis} :: {trial.point_label}",
                f"  output: {trial.output_label}  ({_short_addr(trial.cell)})",
                f"  golden = {_format_value(trial.golden)}",
                f"  mvp    = {_format_value(trial.mvp)}{abs_str}",
            ]
            if trial.note:
                block.append(f"  note  : {trial.note}")
            lines.append("\n".join(block))
        if len(fails) > 50:
            lines.append(f"... and {len(fails) - 50} more")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_sweep(config: GraphDifferentialConfig) -> tuple[list[Trial], list[str]]:
    axes = _resolve_axes()
    targets = output_cell_labels()
    total_points = sum(len(axis.points) for axis in axes)
    logger.info(
        "Running %d axes, %d points, %d cell comparisons",
        len(axes),
        total_points,
        total_points * len(targets),
    )
    for axis in axes:
        logger.info("  - %s: %d point(s)", axis.name, len(axis.points))

    trials: list[Trial] = []
    golden = GoldenDriver(config.workbook_path)
    mvp = MvpGraphDriver(
        config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
    )
    all_input_cells = collect_scenario_input_addresses(axes, inputs_for_excel)
    missing_inputs_in_graph = sorted(  # noqa: SLF001
        cell for cell in all_input_cells if normalize_key(cell) not in mvp._known_keys
    )
    if missing_inputs_in_graph:
        logger.warning(
            "MVP graph is missing these input cells (set_inputs will skip them): %s",
            missing_inputs_in_graph,
        )

    golden.record_input_baselines(all_input_cells)
    mvp.record_input_baselines(all_input_cells)

    try:
        for axis in axes:
            for point in axis.points:
                golden.reset_inputs()
                mvp.reset_inputs()
                cells_in = inputs_for_excel(point.scenario)
                golden.set_inputs(cells_in)
                mvp.set_inputs(cells_in)
                for output_label, cell in targets:
                    golden_value = golden.read(cell)
                    try:
                        mvp_value: Any = mvp.read(cell)
                    except Exception as exc:
                        mvp_value = f"<{type(exc).__name__}: {exc}>"
                        match, abs_diff, rel_diff, note = (
                            False,
                            None,
                            None,
                            "mvp raised",
                        )
                        matched_error = False
                        flagged_matched_error = False
                    else:
                        match, abs_diff, rel_diff, note = values_match(
                            golden_value,
                            mvp_value,
                            atol=config.atol,
                        )
                        matched_error = matched_error_values(golden_value, mvp_value)
                        flagged_matched_error = (
                            matched_error and not point.scenario.expects_error_values
                        )
                    trials.append(
                        Trial(
                            axis=axis.name,
                            point_label=point.label,
                            scenario_id=point.scenario.id,
                            output_label=output_label,
                            cell=cell,
                            golden=golden_value,
                            mvp=mvp_value,
                            match=match,
                            abs_diff=abs_diff,
                            rel_diff=rel_diff,
                            note=note,
                            matched_error=matched_error,
                            flagged_matched_error=flagged_matched_error,
                        )
                    )
    finally:
        golden.close()

    return trials, missing_inputs_in_graph


def run_differential_test(config: GraphDifferentialConfig) -> int:
    _validate_workbook_hooks()
    _verify_paths(config)

    try:
        import xlwings  # noqa: F401
    except ImportError as exc:
        raise FileNotFoundError(
            "xlwings is not installed; install it via `uv add --dev xlwings`"
        ) from exc

    trials, missing_inputs_in_graph = run_sweep(config)
    config.report_dir.mkdir(parents=True, exist_ok=True)
    write_csv_report(trials, config.report_dir / "differential_report.csv")
    write_txt_summary(
        trials,
        config.report_dir / "differential_report.txt",
        config=config,
        missing_inputs_in_graph=missing_inputs_in_graph,
    )

    failed = sum(1 for trial in trials if not trial.match)
    flagged = sum(1 for trial in trials if trial.flagged_matched_error)
    logger.info("Done. Failures: %d / %d", failed, len(trials))
    if flagged:
        log = logger.warning if config.allow_matched_errors else logger.error
        log(
            "Matched error values in %d comparison(s); see MATCHED ERROR VALUES in %s "
            "(set Scenario.expects_error_values=True when intentional)",
            flagged,
            config.report_dir / "differential_report.txt",
        )
    return parity_exit_code(
        failed=failed,
        flagged_matched_errors=flagged,
        allow_matched_errors=config.allow_matched_errors,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        args = parse_args(argv)
        config = config_from_args(Path(__file__).resolve(), args)
        return run_differential_test(config)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("Graph differential test failed with an unhandled exception.")
        return 2


# --------------------------------------------------------------------------
# Workbook-specific hooks — replace these when configuring a new extraction.
# --------------------------------------------------------------------------


def build_scenarios() -> tuple[Scenario, ...]:
    """Return a flat scenario sweep for this workbook."""
    return ()


def build_axes() -> tuple[Axis, ...]:
    """Return axis-organized scenario points, or ``()`` to use ``build_scenarios()``."""
    return ()


def output_cell_labels() -> tuple[tuple[str, str], ...]:
    """Return ``((cell_label, cell_address), ...)`` for every compared output cell."""
    return ()


def inputs_for_excel(scenario: Scenario) -> dict[str, Any]:
    """Map one scenario to Excel cell writes for the golden-master oracle."""
    raise NotImplementedError(
        "Author inputs_for_excel() with cell mappings from bindings/*.bindings.yaml."
    )


if __name__ == "__main__":
    sys.exit(main())
