"""Happy-path value-parity gate for the exported library's public callers.

For the default workbook inputs, every public ``compute_*`` entrypoint in the
exported ``dist`` library must reproduce the values the workbook itself computes.

The reference oracle is excel-grapher's ``FormulaEvaluator`` evaluating the
dependency graph extracted from ``data/tiny-dsa.xlsx`` -- the same graph oracle
the differential parity report validates to 100% against Excel, but runnable
without Microsoft Excel. Because it is an independent implementation of the
workbook's formulas, it catches semantic regressions in the LLM-refactored
``internals.py`` that the structural refactor validators cannot see.

This is intentionally scoped to the default inputs (the happy path): it verifies
that value pass-through from inputs through the refactored internals to the
public ``compute_*`` callers is still numerically correct.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import DynamicRefConfig, create_dependency_graph

from dist.tiny_dsa import api
from dist.tiny_dsa._api_helpers import Record, Records
from dist.tiny_dsa.data import CONSTANTS, DEFAULT_INPUTS

ATOL = 1e-6

OutputLeaves = list[tuple[str, Record]]
ComputeFn = Callable[..., Records]

OUTPUT_RANGES: tuple[tuple[ComputeFn, OutputLeaves], ...] = (
    (api.compute_output_baseline, api._OUTPUT_LEAVES_OUTPUT_BASELINE),
    (api.compute_output_shocked, api._OUTPUT_LEAVES_OUTPUT_SHOCKED),
    (api.compute_output_delta, api._OUTPUT_LEAVES_OUTPUT_DELTA),
)


@pytest.fixture(scope="module")
def workbook_oracle(tiny_dsa_configured_pipeline) -> FormulaEvaluator:
    """Evaluate the workbook's formula graph under the library's default inputs."""
    pipeline = tiny_dsa_configured_pipeline
    config = DynamicRefConfig.from_constraints(pipeline.config.constraints, {})
    graph = create_dependency_graph(
        pipeline.config.workbook_path,
        list(pipeline.config.targets),
        load_values=True,
        dynamic_refs=config,
    )
    known_cells = frozenset(graph.leaf_keys()) | frozenset(graph.formula_keys())
    for address, value in {**DEFAULT_INPUTS, **CONSTANTS}.items():
        if address in known_cells:
            graph.set_node_value(address, value)
    return FormulaEvaluator(graph)


def test_default_inputs_callers_match_workbook(
    workbook_oracle: FormulaEvaluator,
) -> None:
    ctx = api.make_context()
    mismatches: list[str] = []
    for compute_fn, output_leaves in OUTPUT_RANGES:
        records = compute_fn(ctx=ctx)
        addresses = [address for address, _static_record in output_leaves]
        assert len(records) == len(addresses)
        for record, address in zip(records, addresses, strict=True):
            library_obs = record["OBS_VALUE"]
            assert isinstance(library_obs, (int, float)), (
                f"{address}: expected a numeric library value, got "
                f"{type(library_obs).__name__}={library_obs!r}"
            )
            library_value = float(library_obs)
            oracle_value = workbook_oracle.evaluate(address)
            assert isinstance(oracle_value, (int, float)), (
                f"{address}: expected a numeric workbook value, got "
                f"{type(oracle_value).__name__}={oracle_value!r}"
            )
            workbook_value = float(oracle_value)
            abs_diff = abs(library_value - workbook_value)
            if abs_diff > ATOL:
                mismatches.append(
                    f"{address}: library={library_value!r} "
                    f"workbook={workbook_value!r} abs_diff={abs_diff:.3e}"
                )
    assert not mismatches, (
        "Exported library diverges from the workbook for default inputs:\n"
        + "\n".join(mismatches)
    )
