"""Adversarial tests for key-dispatch Pass-1 wiring.

These cases try to break planning, synthesis, and Pass-1 rescue — crashes,
silent wrong answers, and false eligibility.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from src.formula_clustering import FormulaCluster
from src.internals_refactor import (
    ClusterRefactorContext,
    MemberContext,
    _synthesize_key_dispatch_cluster_body,
    _try_synthesize_cluster_body,
    build_cluster_refactor_context,
)
from src.key_dispatch_synthesis import (
    FormulaRegime,
    KeyDispatchPlan,
    is_difference_composition_formula,
    plan_key_dispatch,
    synthesize_key_dispatch_body,
)
from src.mechanical_body import MechanicalSynthesisError
from src.refactor_bindings import BindingKeyValue, KeyConceptSpec

FISCAL_VOCAB = (
    KeyConceptSpec(
        dimension_id="TIME_PERIOD",
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="time_period",
    ),
    KeyConceptSpec(
        dimension_id="FISCAL_MEASURE",
        concept="FISCAL_MEASURE",
        dtype="str",
        suggested_param_name="fiscal_measure",
    ),
)


def test_plan_rejects_gap_members_with_swapped_subtraction_order() -> None:
    """Same CELL-CELL skeleton with opposite minuend/subtrahend is not one regime.

    If we accept both under one Gap regime, the canonical formula's sign is
    applied to every time period — silent wrong answers for half the series.
    """
    cluster = FormulaCluster(
        cluster_id=1,
        members=(
            "Engine!B10",
            "Engine!C10",
            "Engine!D10",
            "Engine!E10",
            "Engine!F10",
            "Engine!G10",
        ),
        canonical_template="=Inputs!B1",
        row=10,
    )
    formulas = {
        "Engine!B10": "=Inputs!B1",  # PB
        "Engine!C10": "=Paris!AS36",  # PB*
        "Engine!D10": "=Engine!C10-Engine!B10",  # Gap = PB* - PB
        "Engine!E10": "=Inputs!C1",  # PB
        "Engine!F10": "=Paris!BR36",  # PB*
        # Swapped order vs D10 — same skeleton, opposite meaning.
        "Engine!G10": "=Engine!E10-Engine!F10",  # Gap = PB - PB*
    }
    keys = {
        "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
        "Engine!C10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB*"},
        "Engine!D10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB Gap"},
        "Engine!E10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB"},
        "Engine!F10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB*"},
        "Engine!G10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB Gap"},
    }
    plan = plan_key_dispatch(
        cluster,
        formulas,
        keys,
        helper_name="dspb",
        dispatch_dimension_candidates=("FISCAL_MEASURE",),
    )
    # Must not invent a single Gap regime that picks one canonical order for both.
    assert plan is None


def test_gap_operand_outside_plan_raises_in_body_synth_but_pass1_absorbs() -> None:
    """Composition operand not in any regime must not abort Pass-1 with ValueError."""
    plan = KeyDispatchPlan(
        helper_name="dspb",
        dispatch_dimension_id="FISCAL_MEASURE",
        sweep_dimension_ids=("TIME_PERIOD",),
        regimes=(
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB"},
                members=("Engine!B10",),
                canonical_formula="=Inputs!B1",
            ),
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB Gap"},
                members=("Engine!D10",),
                canonical_formula="=External!Z1-Engine!B10",
            ),
        ),
    )
    with pytest.raises(ValueError, match="not a member of the key-dispatch plan"):
        synthesize_key_dispatch_body(
            plan,
            regime_callees={
                (("FISCAL_MEASURE", "PB"),): "leaf_pb",
                (("FISCAL_MEASURE", "PB Gap"),): "",
            },
            include_ctx=True,
        )

    ctx = ClusterRefactorContext(
        cluster_id=1,
        canonical_template="=Inputs!B1",
        row=10,
        members=(
            MemberContext(
                address="Engine!B10",
                function_name="cell_engine_b10",
                engine_column="B",
                normalized_formula="=Inputs!B1",
                python_source=(
                    "def cell_engine_b10(ctx):\n"
                    "    return xl_number(xl_cell(ctx, 'Inputs!B1'))\n"
                ),
                dependency_addresses=("Inputs!B1",),
                dependency_functions=(),
                binding_keys=None,
                binding_record=None,
            ),
            MemberContext(
                address="Engine!D10",
                function_name="cell_engine_d10",
                engine_column="D",
                normalized_formula="=External!Z1-Engine!B10",
                python_source=(
                    "def cell_engine_d10(ctx):\n"
                    "    return xl_number(xl_cell(ctx, 'External!Z1')) - "
                    "xl_number(xl_cell(ctx, 'Inputs!B1'))\n"
                ),
                dependency_addresses=("External!Z1", "Engine!B10"),
                dependency_functions=("cell_engine_b10",),
                binding_keys=None,
                binding_record=None,
            ),
        ),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        first_year_column="B",
        allowed_runtime_symbols=("ctx", "xl_cell", "xl_number"),
        key_vocabulary=FISCAL_VOCAB,
        expected_member_keys={
            "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
            "Engine!D10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB Gap"},
        },
        naming_hints={},
        expected_helper_name="dspb",
        contract="key_dispatch",
        key_dispatch_plan=plan,
        key_dispatch_bound_keys={
            "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
            "Engine!D10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB Gap"},
            "Inputs!B1": {"TIME_PERIOD": 2050},
            "External!Z1": {"TIME_PERIOD": 2050},
        },
    )
    draft = _try_synthesize_cluster_body(ctx)
    assert draft is None


def test_regime_local_name_avoids_python_keywords() -> None:
    plan = KeyDispatchPlan(
        helper_name="helper",
        dispatch_dimension_id="KIND",
        sweep_dimension_ids=("TIME_PERIOD",),
        regimes=(
            FormulaRegime(
                dispatch_key_values={"KIND": "from"},
                members=("Engine!B1",),
                canonical_formula="=Inputs!B1",
            ),
            FormulaRegime(
                dispatch_key_values={"KIND": "gap"},
                members=("Engine!D1",),
                canonical_formula="=Engine!C1-Engine!B1",
            ),
            FormulaRegime(
                dispatch_key_values={"KIND": "other"},
                members=("Engine!C1",),
                canonical_formula="=Paris!A1",
            ),
        ),
    )
    # Force composition path for gap using sibling "from" as an operand.
    plan = KeyDispatchPlan(
        helper_name="helper",
        dispatch_dimension_id="KIND",
        sweep_dimension_ids=("TIME_PERIOD",),
        regimes=(
            FormulaRegime(
                dispatch_key_values={"KIND": "from"},
                members=("Engine!B1",),
                canonical_formula="=Inputs!B1",
            ),
            FormulaRegime(
                dispatch_key_values={"KIND": "other"},
                members=("Engine!C1",),
                canonical_formula="=Paris!A1",
            ),
            FormulaRegime(
                dispatch_key_values={"KIND": "gap"},
                members=("Engine!D1",),
                canonical_formula="=Engine!C1-Engine!B1",
            ),
        ),
    )
    body = synthesize_key_dispatch_body(
        plan,
        regime_callees={
            (("KIND", "from"),): "leaf_from",
            (("KIND", "other"),): "leaf_other",
            (("KIND", "gap"),): "",
        },
        include_ctx=True,
    )
    module = ast.parse(
        "def helper(ctx, time_period, kind):\n" + textwrap.indent(body, "    ")
    )
    assert isinstance(module.body[0], ast.FunctionDef)


def test_partial_regime_membership_fails_loudly() -> None:
    """If a planned regime member lacks a MemberContext, do not synthesize a partial body."""
    plan = KeyDispatchPlan(
        helper_name="dspb",
        dispatch_dimension_id="FISCAL_MEASURE",
        sweep_dimension_ids=("TIME_PERIOD",),
        regimes=(
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB"},
                members=("Engine!B10", "Engine!E10"),  # E10 missing from ctx.members
                canonical_formula="=Inputs!B1",
            ),
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB*"},
                members=("Engine!C10",),
                canonical_formula="=Paris!AS36",
            ),
        ),
    )
    ctx = ClusterRefactorContext(
        cluster_id=2,
        canonical_template="=Inputs!B1",
        row=10,
        members=(
            MemberContext(
                address="Engine!B10",
                function_name="cell_engine_b10",
                engine_column="B",
                normalized_formula="=Inputs!B1",
                python_source=(
                    "def cell_engine_b10(ctx):\n"
                    "    return xl_number(xl_cell(ctx, 'Inputs!B1'))\n"
                ),
                dependency_addresses=("Inputs!B1",),
                dependency_functions=(),
                binding_keys=None,
                binding_record=None,
            ),
            MemberContext(
                address="Engine!C10",
                function_name="cell_engine_c10",
                engine_column="C",
                normalized_formula="=Paris!AS36",
                python_source=(
                    "def cell_engine_c10(ctx):\n"
                    "    return xl_number(xl_cell(ctx, 'Paris!AS36'))\n"
                ),
                dependency_addresses=("Paris!AS36",),
                dependency_functions=(),
                binding_keys=None,
                binding_record=None,
            ),
        ),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        first_year_column="B",
        allowed_runtime_symbols=("ctx", "xl_cell", "xl_number"),
        key_vocabulary=FISCAL_VOCAB,
        expected_member_keys={
            "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
            "Engine!C10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB*"},
            "Engine!E10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB"},
        },
        naming_hints={},
        expected_helper_name="dspb",
        contract="key_dispatch",
        key_dispatch_plan=plan,
        key_dispatch_bound_keys={
            "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
            "Engine!C10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB*"},
            "Engine!E10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB"},
            "Inputs!B1": {"TIME_PERIOD": 2050},
            "Inputs!C1": {"TIME_PERIOD": 2075},
            "Paris!AS36": {"TIME_PERIOD": 2050},
        },
    )
    with pytest.raises(MechanicalSynthesisError, match="partial|missing|members"):
        _synthesize_key_dispatch_cluster_body(ctx)


def test_quoted_sheet_name_difference_is_recognized() -> None:
    assert is_difference_composition_formula(
        "='Output Scenarios'!H26-'Output Scenarios'!F26"
    )


def test_unquoted_hyphenated_sheet_difference_is_recognized_or_rejected_loudly() -> (
    None
):
    """Hyphenated unquoted sheets must not be misclassified as non-composition sweeps."""
    formula = "=Sheet-Out!C10-Sheet-Out!B10"
    # Either parse as composition, or refuse key-dispatch for that regime shape.
    # Silent treatment as a normal sweep body is the failure mode.
    assert (
        is_difference_composition_formula(formula)
        or plan_key_dispatch(
            FormulaCluster(
                cluster_id=3,
                members=("Engine!B1", "Engine!C1", "Engine!D1"),
                canonical_template="=Sheet-Out!B1",
                row=1,
            ),
            {
                "Engine!B1": "=Sheet-Out!B1",
                "Engine!C1": "=Sheet-Out!C1",
                "Engine!D1": formula,
            },
            {
                "Engine!B1": {"TIME_PERIOD": 1, "FISCAL_MEASURE": "PB"},
                "Engine!C1": {"TIME_PERIOD": 1, "FISCAL_MEASURE": "PB*"},
                "Engine!D1": {"TIME_PERIOD": 1, "FISCAL_MEASURE": "PB Gap"},
            },
            helper_name="hyphen_sheet",
            dispatch_dimension_candidates=("FISCAL_MEASURE",),
        )
        is None
    )


def test_trade_balance_unroutable_shape_not_key_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False-positive guard: bilateral REF_AREA trade-balance must not become key_dispatch.

    Since #132 the routing gate's rejection no longer skips the cluster outright
    -- mechanical synthesis rescues it as ``member_sweep`` via a ``REF_AREA``
    lookup. The guard this test protects is unchanged: key-dispatch planning must
    not spuriously claim this shape.
    """
    from tests.test_internals_refactor import (
        ALLOWED_RUNTIME_SYMBOLS,
        KEY_VOCABULARY,
        REF_AREA_SPEC,
        TRADE_BALANCE_CLUSTER,
        TRADE_BALANCE_SERIES_MAP,
        VARIABLE_PAIR_OPERAND_KEYS,
        _trade_balance_projection,
        _write_trade_balance_internals,
    )

    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
    )
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        **VARIABLE_PAIR_OPERAND_KEYS,
        "Engine!B5": {"REF_AREA": "US"},
        "Engine!C5": {"REF_AREA": "DE"},
        "Engine!D5": {"REF_AREA": "JP"},
    }
    ctx = build_cluster_refactor_context(
        _trade_balance_projection(),
        TRADE_BALANCE_CLUSTER,
        _write_trade_balance_internals(tmp_path),
        bound_address_keys=bound_address_keys,
        key_vocabulary=(KEY_VOCABULARY[0], REF_AREA_SPEC),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.contract != "key_dispatch"


def test_empty_sweep_dims_do_not_crash_pass1() -> None:
    plan = KeyDispatchPlan(
        helper_name="constants",
        dispatch_dimension_id="FISCAL_MEASURE",
        sweep_dimension_ids=(),
        regimes=(
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB"},
                members=("Engine!B10",),
                canonical_formula="=1",
            ),
            FormulaRegime(
                dispatch_key_values={"FISCAL_MEASURE": "PB*"},
                members=("Engine!C10",),
                canonical_formula="=2",
            ),
        ),
    )
    ctx = ClusterRefactorContext(
        cluster_id=4,
        canonical_template="=1",
        row=10,
        members=(
            MemberContext(
                address="Engine!B10",
                function_name="cell_engine_b10",
                engine_column="B",
                normalized_formula="=1",
                python_source="def cell_engine_b10(ctx):\n    return 1.0\n",
                dependency_addresses=(),
                dependency_functions=(),
                binding_keys=None,
                binding_record=None,
            ),
            MemberContext(
                address="Engine!C10",
                function_name="cell_engine_c10",
                engine_column="C",
                normalized_formula="=2",
                python_source="def cell_engine_c10(ctx):\n    return 2.0\n",
                dependency_addresses=(),
                dependency_functions=(),
                binding_keys=None,
                binding_record=None,
            ),
        ),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        first_year_column="B",
        allowed_runtime_symbols=("ctx",),
        key_vocabulary=FISCAL_VOCAB,
        expected_member_keys={
            "Engine!B10": {"FISCAL_MEASURE": "PB"},
            "Engine!C10": {"FISCAL_MEASURE": "PB*"},
        },
        naming_hints={},
        expected_helper_name="constants",
        contract="key_dispatch",
        key_dispatch_plan=plan,
        key_dispatch_bound_keys={
            "Engine!B10": {"FISCAL_MEASURE": "PB"},
            "Engine!C10": {"FISCAL_MEASURE": "PB*"},
        },
    )
    draft = _try_synthesize_cluster_body(ctx)
    assert draft is None
