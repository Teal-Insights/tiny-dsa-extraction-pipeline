"""Behavior tests for key-dispatch synthesis of multi-regime series."""

from __future__ import annotations

import ast
import re
import textwrap

from src.formula_clustering import FormulaCluster
from src.key_dispatch_synthesis import (
    plan_key_dispatch,
    synthesize_key_dispatch_body,
)

# Paris-like DSPB milestone triplet: three formula regimes keyed by FISCAL_MEASURE.
PB_TRIPLET_CLUSTER = FormulaCluster(
    cluster_id=0,
    members=(
        "Output!F10",
        "Output!G10",
        "Output!H10",
        "Output!I10",
        "Output!J10",
        "Output!K10",
    ),
    canonical_template="=Output!D1",
    row=10,
)

PB_TRIPLET_FORMULAS = {
    "Output!F10": "=Output!D1",  # PB @ 2050
    "Output!G10": "=Paris!AS36",  # PB* @ 2050
    "Output!H10": "=Output!G10-Output!F10",  # Gap @ 2050
    "Output!I10": "=Output!E1",  # PB @ 2075
    "Output!J10": "=Paris!BR36",  # PB* @ 2075
    "Output!K10": "=Output!J10-Output!I10",  # Gap @ 2075
}

PB_TRIPLET_MEMBER_KEYS = {
    "Output!F10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
    "Output!G10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB*"},
    "Output!H10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB Gap"},
    "Output!I10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB"},
    "Output!J10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB*"},
    "Output!K10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB Gap"},
}

# Uniform single-skeleton sweep — must not become key-dispatch.
UNIFORM_SWEEP_CLUSTER = FormulaCluster(
    cluster_id=1,
    members=("Engine!B2", "Engine!C2", "Engine!D2"),
    canonical_template="=Inputs!B1",
    row=2,
)

UNIFORM_SWEEP_FORMULAS = {
    "Engine!B2": "=Inputs!B1",
    "Engine!C2": "=Inputs!C1",
    "Engine!D2": "=Inputs!D1",
}

UNIFORM_SWEEP_MEMBER_KEYS = {
    "Engine!B2": {"TIME_PERIOD": 2025},
    "Engine!C2": {"TIME_PERIOD": 2026},
    "Engine!D2": {"TIME_PERIOD": 2027},
}


def _if_compare_value(node: ast.If) -> object:
    test = node.test
    assert isinstance(test, ast.Compare)
    assert len(test.comparators) == 1
    comparator = test.comparators[0]
    assert isinstance(comparator, ast.Constant)
    return comparator.value


def test_plan_key_dispatch_detects_fiscal_measure_regimes() -> None:
    plan = plan_key_dispatch(
        PB_TRIPLET_CLUSTER,
        PB_TRIPLET_FORMULAS,
        PB_TRIPLET_MEMBER_KEYS,
        helper_name="output_scenarios_dspb_milestones_paris",
        dispatch_dimension_candidates=("FISCAL_MEASURE",),
    )

    assert plan is not None
    assert plan.helper_name == "output_scenarios_dspb_milestones_paris"
    assert plan.dispatch_dimension_id == "FISCAL_MEASURE"
    assert plan.sweep_dimension_ids == ("TIME_PERIOD",)
    assert len(plan.regimes) == 3

    by_measure = {
        regime.dispatch_key_values["FISCAL_MEASURE"]: regime for regime in plan.regimes
    }
    assert set(by_measure) == {"PB", "PB*", "PB Gap"}
    assert set(by_measure["PB"].members) == {"Output!F10", "Output!I10"}
    assert set(by_measure["PB*"].members) == {"Output!G10", "Output!J10"}
    assert set(by_measure["PB Gap"].members) == {"Output!H10", "Output!K10"}
    assert by_measure["PB"].canonical_formula in {"=Output!D1", "=Output!E1"}
    assert "=Paris!" in by_measure["PB*"].canonical_formula
    assert "-" in by_measure["PB Gap"].canonical_formula


def test_plan_key_dispatch_returns_none_for_uniform_skeleton_sweep() -> None:
    plan = plan_key_dispatch(
        UNIFORM_SWEEP_CLUSTER,
        UNIFORM_SWEEP_FORMULAS,
        UNIFORM_SWEEP_MEMBER_KEYS,
        helper_name="engine_row",
        dispatch_dimension_candidates=("TIME_PERIOD",),
    )
    assert plan is None


def test_synthesize_key_dispatch_body_matches_golden_control_flow() -> None:
    plan = plan_key_dispatch(
        PB_TRIPLET_CLUSTER,
        PB_TRIPLET_FORMULAS,
        PB_TRIPLET_MEMBER_KEYS,
        helper_name="output_scenarios_dspb_milestones_paris",
        dispatch_dimension_candidates=("FISCAL_MEASURE",),
    )
    assert plan is not None

    body = synthesize_key_dispatch_body(
        plan,
        regime_callees={
            (("FISCAL_MEASURE", "PB"),): "output_scenarios_pb_anchor",
            (("FISCAL_MEASURE", "PB*"),): "paris_debt_stabilizing_primary_balance",
            # Gap: empty callee → compose sibling regimes in the dispatcher.
            (("FISCAL_MEASURE", "PB Gap"),): "",
        },
    )

    assert "fiscal_measure" in body
    assert "time_period" in body

    module = ast.parse(
        "def helper(time_period, fiscal_measure):\n" + textwrap.indent(body, "    ")
    )
    func = module.body[0]
    assert isinstance(func, ast.FunctionDef)
    if_nodes = [node for node in func.body if isinstance(node, ast.If)]
    assert len(if_nodes) >= 3

    branches = {_if_compare_value(node): node for node in if_nodes}
    assert set(branches) >= {"PB", "PB*", "PB Gap"}

    pb_branch_src = ast.unparse(branches["PB"])
    assert "output_scenarios_pb_anchor" in pb_branch_src
    assert "time_period" in pb_branch_src

    pb_star_branch_src = ast.unparse(branches["PB*"])
    assert "paris_debt_stabilizing_primary_balance" in pb_star_branch_src

    gap_branch_src = ast.unparse(branches["PB Gap"])
    # Gap composes the other two regimes (sibling calls or recursive self-calls).
    assert re.search(r"PB\*|pb_star|fiscal_measure\s*==\s*['\"]PB\*", gap_branch_src)
    assert "PB" in gap_branch_src
    assert "-" in gap_branch_src or "Sub" in type(branches["PB Gap"]).__name__


def test_synthesize_key_dispatch_body_gap_returns_difference_of_regimes() -> None:
    plan = plan_key_dispatch(
        PB_TRIPLET_CLUSTER,
        PB_TRIPLET_FORMULAS,
        PB_TRIPLET_MEMBER_KEYS,
        helper_name="output_scenarios_dspb_milestones_paris",
    )
    assert plan is not None

    body = synthesize_key_dispatch_body(
        plan,
        regime_callees={
            (("FISCAL_MEASURE", "PB"),): "output_scenarios_pb_anchor",
            (("FISCAL_MEASURE", "PB*"),): "paris_debt_stabilizing_primary_balance",
            (("FISCAL_MEASURE", "PB Gap"),): "",
        },
    )
    module = ast.parse(
        "def helper(time_period, fiscal_measure):\n" + textwrap.indent(body, "    ")
    )
    func = module.body[0]
    assert isinstance(func, ast.FunctionDef)

    gap_if = next(
        node
        for node in func.body
        if isinstance(node, ast.If) and _if_compare_value(node) == "PB Gap"
    )
    gap_src = ast.unparse(gap_if)
    # Prefer an explicit binary subtract of the two regime results.
    assert (
        "paris_debt_stabilizing_primary_balance" in gap_src
        or 'fiscal_measure="PB*"' in gap_src
        or "fiscal_measure='PB*'" in gap_src
    )
    assert (
        "output_scenarios_pb_anchor" in gap_src
        or 'fiscal_measure="PB"' in gap_src
        or "fiscal_measure='PB'" in gap_src
    )
    assert "-" in gap_src
