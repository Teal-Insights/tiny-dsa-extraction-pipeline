"""FormulaEvaluator parity for the inverted-tree exported package."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import DependencyGraph

from src.pipeline_config import InvertedTreeValidateCase, PipelineConfig

ATOL = 1e-9


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


def _close(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    if len(left) != len(right):
        return False
    for observed, expected in zip(left, right, strict=True):
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return False
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
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


def _require_cases(
    cases: tuple[InvertedTreeValidateCase, ...],
) -> tuple[InvertedTreeValidateCase, ...]:
    if not cases:
        raise RuntimeError(
            "No inverted-tree validate cases configured. Author "
            "INVERTED_TREE_VALIDATE_CASES in workbook_config.py with default "
            "output addresses and compute_* kwargs before claiming "
            "FormulaEvaluator parity."
        )
    for case in cases:
        if not case.addresses:
            raise RuntimeError(
                f"{case.compute_name}: inverted-tree validate case has no output "
                "addresses. Declare the default FormulaEvaluator cells in "
                "workbook_config.INVERTED_TREE_VALIDATE_CASES."
            )
        if not case.compute_name:
            raise RuntimeError(
                "inverted-tree validate case is missing compute_name. Name the "
                "keyword-only compute_* helper in workbook_config."
            )
    return cases


def _compute_kwargs(
    package: object, case: InvertedTreeValidateCase
) -> dict[str, object]:
    data = getattr(package, "data", None)
    if data is None:
        raise AttributeError(
            f"{package!r} has no data module; inverted-tree validate reads "
            "default kwargs from generated data.py attributes"
        )
    kwargs: dict[str, object] = {}
    for arg_name, attr_name in case.data_kwargs:
        if not hasattr(data, attr_name):
            raise AttributeError(
                f"{case.compute_name}: data.{attr_name} is missing; "
                "INVERTED_TREE_VALIDATE_CASES data_kwargs must name generated "
                "data.py defaults"
            )
        kwargs[arg_name] = getattr(data, attr_name)
    return kwargs


def write_formula_evaluator_parity_reports(
    config: PipelineConfig,
    *,
    report_dir: Path,
    graph: DependencyGraph | None = None,
) -> int:
    """Compare configured inverted-tree computes to the pipeline graph evaluator.

    Returns 0 when every compared cell matches, otherwise 1. Empty configured
    addresses fail closed — the same pattern as empty graph differential hooks.
    """
    cases = _require_cases(config.inverted_tree_validate_cases)
    if graph is None:
        from src.extraction_pipeline import build_pipeline_graph

        graph = build_pipeline_graph(config).graph
    evaluator = FormulaEvaluator(graph)
    addresses = [address for case in cases for address in case.addresses]
    outputs = evaluator.evaluate(addresses)

    package = _load_package(config)
    passed = 0
    failed = 0
    for case in cases:
        compute = getattr(package, case.compute_name, None)
        if compute is None:
            api = importlib.import_module(f"{config.dist_metadata.package_name}.api")
            compute = getattr(api, case.compute_name, None)
        if compute is None or not callable(compute):
            raise AttributeError(
                f"exported package has no callable {case.compute_name}; "
                "INVERTED_TREE_VALIDATE_CASES must name a generated compute_* "
                "helper"
            )
        observed = tuple(compute(**_compute_kwargs(package, case)))
        expected = tuple(outputs[address] for address in case.addresses)
        if _close(observed, expected):
            passed += len(observed)
        else:
            failed += len(case.addresses)
    total = passed + failed
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "parity_report.txt").write_text(
        _report_text(config=config, passed=passed, failed=failed, total=total),
        encoding="utf-8",
    )
    (report_dir / "parity_report.csv").write_text(
        f"scenario,passed,failed\ndefault,{passed},{failed}\n",
        encoding="utf-8",
    )
    return 0 if failed == 0 else 1
