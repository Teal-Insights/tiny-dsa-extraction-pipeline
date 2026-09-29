#!/usr/bin/env python3
"""Assert FormulaEvaluator graph API matches the exported Model on defaults.

Replaces the old browser JS evaluate() mirror check now that the viz calls
``POST /api/evaluate`` with ``backend=formula_evaluator``.
"""

from __future__ import annotations

import sys
from typing import Any

ATOL = 1e-6


def _close(a: float, b: float, tol: float = ATOL) -> bool:
    return abs(float(a) - float(b)) <= tol


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, pv in left.items():
        jv = right.get(key)
        if isinstance(pv, dict):
            if not isinstance(jv, dict):
                errors.append(f"{key}: expected map, got {jv!r}")
                continue
            for k, v in pv.items():
                rk = k if k in jv else str(k)
                if rk not in jv or not _close(float(v), float(jv[rk])):
                    errors.append(
                        f"{key}[{k}]: export={v} formula_evaluator={jv.get(rk)}"
                    )
            continue
        numeric = isinstance(pv, (int, float)) and isinstance(jv, (int, float))
        close = numeric and _close(float(pv), float(jv))
        if (
            (jv is None or (numeric and not close))
            and not (isinstance(pv, str) and pv == jv)
            and not close
            and pv != jv
        ):
            errors.append(f"{key}: export={pv} formula_evaluator={jv}")
    return errors


def main() -> int:
    from __SERIES_GRAPH_PACKAGE__ import graph_formula_evaluator as fe
    from __SERIES_GRAPH_PACKAGE__.graph_api import evaluate

    if not fe.is_available():
        print(
            "skip: formula_evaluator unavailable (install excel-grapher via "
            "`uv sync --group graph` and ensure tests/fixtures/__SERIES_GRAPH_WORKBOOK_FIXTURE__)",
            file=sys.stderr,
        )
        return 0

    fe.reset_driver()
    export_values = evaluate(backend="export")
    graph_values = evaluate(backend="formula_evaluator")
    errors = compare(export_values, graph_values)
    if errors:
        print(
            "formula_evaluator diverges from export Model on defaults:",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print("ok: formula_evaluator matches Model.from_defaults() via graph_api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
