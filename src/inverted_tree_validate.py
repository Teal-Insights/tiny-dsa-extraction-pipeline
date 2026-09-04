"""FormulaEvaluator parity for the inverted-tree exported package."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import DependencyGraph

from src.pipeline_config import PipelineConfig

ATOL = 1e-9
_BASELINE_ADDRS = tuple(f"Outputs!{col}12" for col in "BCDEF")
_SHOCKED_ADDRS = tuple(f"Outputs!{col}13" for col in "BCDEF")


def _load_package(config: PipelineConfig):
    dist_root = str(config.dist_root)
    if dist_root not in sys.path:
        sys.path.insert(0, dist_root)
    name = config.dist_metadata.package_name
    for key in list(sys.modules):
        if key == name or key.startswith(f"{name}."):
            del sys.modules[key]
    package = importlib.import_module(name)
    importlib.import_module(f"{name}.api")
    importlib.import_module(f"{name}.data")
    return package


def _close(left: tuple[float, ...], right: tuple[object, ...]) -> bool:
    if len(left) != len(right):
        return False
    for observed, expected in zip(left, right, strict=True):
        if not isinstance(expected, (int, float)):
            return False
        if abs(float(observed) - float(expected)) > ATOL:
            return False
    return True


def _report_text(
    *,
    config: PipelineConfig,
    passed: int,
    failed: int,
    total: int,
) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    result = "PASS" if failed == 0 else "FAIL"
    rate = "100.00%" if total and failed == 0 else f"{100.0 * passed / total:.2f}%"
    return (
        "Parity report: inverted-tree package vs FormulaEvaluator\n"
        f"Generated: {generated}\n"
        f"Workbook:  {config.workbook_path}\n"
        f"Package:   {config.package_root}\n"
        f"Tolerance: atol = {ATOL}\n"
        "\n"
        f"Total comparisons: {total}\n"
        f"Passed:            {passed}\n"
        f"Failed:            {failed}\n"
        f"Pass rate:         {rate}\n"
        "Acceptance bar:    100.00%\n"
        f"Result:            {result}\n"
    )


def write_formula_evaluator_parity_reports(
    config: PipelineConfig,
    *,
    report_dir: Path,
    graph: DependencyGraph | None = None,
) -> int:
    """Compare default inverted-tree computes to the pipeline graph evaluator.

    Returns 0 when every compared cell matches, otherwise 1.
    """
    graph_result = None
    if graph is None:
        from src.extraction_pipeline import build_pipeline_graph

        graph_result = build_pipeline_graph(config)
        graph = graph_result.graph
    evaluator = FormulaEvaluator(graph)
    outputs = evaluator.evaluate(list(_BASELINE_ADDRS + _SHOCKED_ADDRS))
    expected_baseline = tuple(outputs[addr] for addr in _BASELINE_ADDRS)
    expected_shocked = tuple(outputs[addr] for addr in _SHOCKED_ADDRS)

    package = _load_package(config)
    data = package.data
    baseline = package.compute_output_baseline(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
    )
    shocked = package.compute_output_shocked(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
        shock_year=data.SHOCK_YEAR_DEFAULT,
        shock_type=data.SHOCK_TYPE_DEFAULT,
        shock_magnitudes=data.SHOCK_MAGNITUDES_DEFAULT,
    )
    passed = 0
    failed = 0
    for observed, expected in (
        (tuple(baseline), expected_baseline),
        (tuple(shocked), expected_shocked),
    ):
        if _close(observed, expected):
            passed += len(observed)
        else:
            failed += len(observed)
    total = passed + failed
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "parity_report.txt").write_text(
        _report_text(config=config, passed=passed, failed=failed, total=total),
        encoding="utf-8",
    )
    (report_dir / "parity_report.csv").write_text(
        f"scenario,passed,failed\ndefault_borvelia,{passed},{failed}\n",
        encoding="utf-8",
    )
    return 0 if failed == 0 else 1
