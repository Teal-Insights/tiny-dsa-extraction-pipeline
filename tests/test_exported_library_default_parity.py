"""Happy-path value-parity gate for the exported library's public callers.

For the default workbook inputs, every public ``compute_*`` entrypoint in the
exported ``dist`` library must reproduce the values the workbook graph computes.

The reference oracle is excel-grapher's ``FormulaEvaluator`` on the extraction
graph from ``data/tiny-dsa.xlsx``. This is runnable without Microsoft Excel.
"""

from __future__ import annotations

import pytest
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import DynamicRefConfig, create_dependency_graph

from dist.tiny_dsa import api, data

ATOL = 1e-6

_BASELINE_ADDRS = tuple(f"Outputs!{col}12" for col in "BCDEF")
_SHOCKED_ADDRS = tuple(f"Outputs!{col}13" for col in "BCDEF")
_DELTA_ADDRS = tuple(f"Outputs!{col}14" for col in "BCDEF")


@pytest.fixture(scope="module")
def workbook_oracle(tiny_dsa_configured_pipeline) -> FormulaEvaluator:
    """Evaluate the workbook's formula graph under stored default inputs."""
    pipeline = tiny_dsa_configured_pipeline
    config = DynamicRefConfig.from_constraints(pipeline.config.constraints, {})
    graph = create_dependency_graph(
        pipeline.config.workbook_path,
        list(pipeline.config.targets),
        load_values=True,
        dynamic_refs=config,
    )
    return FormulaEvaluator(graph)


def _compare(
    *,
    observed: tuple[float, ...],
    addresses: tuple[str, ...],
    workbook_oracle: FormulaEvaluator,
    mismatches: list[str],
) -> None:
    assert len(observed) == len(addresses)
    for value, address in zip(observed, addresses, strict=True):
        oracle_value = workbook_oracle.evaluate(address)
        assert isinstance(oracle_value, (int, float)), (
            f"{address}: expected a numeric workbook value, got "
            f"{type(oracle_value).__name__}={oracle_value!r}"
        )
        abs_diff = abs(float(value) - float(oracle_value))
        if abs_diff > ATOL:
            mismatches.append(
                f"{address}: library={float(value)!r} "
                f"workbook={float(oracle_value)!r} abs_diff={abs_diff:.3e}"
            )


def test_default_inputs_callers_match_workbook(
    workbook_oracle: FormulaEvaluator,
) -> None:
    mismatches: list[str] = []
    _compare(
        observed=api.compute_output_baseline(
            country_name=data.COUNTRY_NAME_DEFAULT,
            country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
            growth_baseline=data.GROWTH_BASELINE_DEFAULT,
            interest_baseline=data.INTEREST_BASELINE_DEFAULT,
            primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
        ),
        addresses=_BASELINE_ADDRS,
        workbook_oracle=workbook_oracle,
        mismatches=mismatches,
    )
    _compare(
        observed=api.compute_output_shocked(
            country_name=data.COUNTRY_NAME_DEFAULT,
            country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
            growth_baseline=data.GROWTH_BASELINE_DEFAULT,
            interest_baseline=data.INTEREST_BASELINE_DEFAULT,
            primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
            shock_year=data.SHOCK_YEAR_DEFAULT,
            shock_type=data.SHOCK_TYPE_DEFAULT,
            shock_magnitudes=data.SHOCK_MAGNITUDES_DEFAULT,
        ),
        addresses=_SHOCKED_ADDRS,
        workbook_oracle=workbook_oracle,
        mismatches=mismatches,
    )
    _compare(
        observed=api.compute_output_delta(
            country_name=data.COUNTRY_NAME_DEFAULT,
            country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
            growth_baseline=data.GROWTH_BASELINE_DEFAULT,
            interest_baseline=data.INTEREST_BASELINE_DEFAULT,
            primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
            shock_year=data.SHOCK_YEAR_DEFAULT,
            shock_type=data.SHOCK_TYPE_DEFAULT,
            shock_magnitudes=data.SHOCK_MAGNITUDES_DEFAULT,
        ),
        addresses=_DELTA_ADDRS,
        workbook_oracle=workbook_oracle,
        mismatches=mismatches,
    )
    assert not mismatches, (
        "Exported library diverges from the workbook for default inputs:\n"
        + "\n".join(mismatches)
    )
