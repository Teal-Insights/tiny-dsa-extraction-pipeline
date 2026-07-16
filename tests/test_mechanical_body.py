"""Unit tests for mechanical cluster-body synthesis (issue #45)."""

from __future__ import annotations

import ast

import pytest

from src.internals_refactor import MemberContext
from src.mechanical_body import (
    MechanicalBodyDraft,
    MechanicalSynthesisError,
    parse_inlinable_wrapper,
    synthesize_cluster_body,
    synthesize_singleton_body,
)
from src.refactor_bindings import KeyConceptSpec
from src.refactor_fingerprints import build_cluster_fingerprint_summary

TIME_PERIOD_VOCAB = (
    KeyConceptSpec(
        dimension_id="TIME_PERIOD",
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="time_period",
    ),
)


def _member(address: str, formula: str, python_body: str) -> MemberContext:
    sheet, colrow = address.split("!", 1)
    column = "".join(c for c in colrow if c.isalpha())
    row = int("".join(c for c in colrow if c.isdigit()))
    function_name = f"cell_{sheet.lower()}_{column.lower()}{row}"
    indented = "\n".join(f"    {line}" for line in python_body.splitlines())
    return MemberContext(
        address=address,
        function_name=function_name,
        engine_column=column,
        normalized_formula=formula,
        python_source=f"def {function_name}(ctx):\n{indented}\n",
        dependency_addresses=(),
        dependency_functions=(),
    )


def _assert_body_compiles(draft: MechanicalBodyDraft, params: tuple[str, ...]) -> None:
    body = "\n".join(f"    {line}" for line in draft.body.splitlines())
    ast.parse(f"def _draft(ctx, {', '.join(params)}):\n{body}\n")


def test_identity_sweep_xl_cell_reads_become_column_lookup() -> None:
    members = (
        _member("Data!E20", "=Data!E4", "return xl_number(xl_cell(ctx, 'Data!E4'))"),
        _member("Data!F20", "=Data!F4", "return xl_number(xl_cell(ctx, 'Data!F4'))"),
        _member("Data!G20", "=Data!G4", "return xl_number(xl_cell(ctx, 'Data!G4'))"),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!G20": {"TIME_PERIOD": 6},
        "Data!E4": {"TIME_PERIOD": 4},
        "Data!F4": {"TIME_PERIOD": 5},
        "Data!G4": {"TIME_PERIOD": 6},
    }
    expected = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!G20": {"TIME_PERIOD": 6},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="observed_value",
    )
    assert "column_by_time_period = {4: 'E', 5: 'F', 6: 'G'}" in draft.body
    assert "xl_cell(ctx, f'Data!{column_by_time_period[time_period]}4')" in draft.body
    assert "column_by_time_period" in draft.lookup_table_names
    _assert_body_compiles(draft, ("time_period",))


def test_accessor_offset_read_becomes_derived_argument() -> None:
    members = (
        _member(
            "Data!E20",
            "=Data!D4",
            "_t1 = read_price(ctx, time_period=3)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!F20",
            "=Data!E4",
            "_t1 = read_price(ctx, time_period=4)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!G20",
            "=Data!F4",
            "_t1 = read_price(ctx, time_period=5)\nreturn xl_number(_t1)",
        ),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!G20": {"TIME_PERIOD": 6},
        "Data!D4": {"TIME_PERIOD": 3},
        "Data!E4": {"TIME_PERIOD": 4},
        "Data!F4": {"TIME_PERIOD": 5},
    }
    expected = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!G20": {"TIME_PERIOD": 6},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={
            "Data!D4": "price",
            "Data!E4": "price",
            "Data!F4": "price",
        },
    )
    assert summary.fallback_reason is None
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="lagged_price",
    )
    assert "read_price(ctx, time_period=time_period - 1)" in draft.body
    assert "_t1" in draft.renameable_locals
    _assert_body_compiles(draft, ("time_period",))


def test_self_recurrence_with_anchor_group_routes_by_period() -> None:
    members = (
        _member("Data!C20", "=Data!C4", "return xl_number(xl_cell(ctx, 'Data!C4'))"),
        _member(
            "Data!D20",
            "=Data!C20+1",
            "_t1 = xl_eval(ctx, 'Data!C20', cell_data_c20)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
        _member(
            "Data!E20",
            "=Data!D20+1",
            "_t1 = xl_eval(ctx, 'Data!D20', cell_data_d20)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
        _member(
            "Data!F20",
            "=Data!E20+1",
            "_t1 = xl_eval(ctx, 'Data!E20', cell_data_e20)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
    )
    bound_keys = {
        "Data!C20": {"TIME_PERIOD": 1},
        "Data!D20": {"TIME_PERIOD": 2},
        "Data!E20": {"TIME_PERIOD": 3},
        "Data!F20": {"TIME_PERIOD": 4},
        "Data!C4": {"TIME_PERIOD": 1},
    }
    expected = {
        "Data!C20": {"TIME_PERIOD": 1},
        "Data!D20": {"TIME_PERIOD": 2},
        "Data!E20": {"TIME_PERIOD": 3},
        "Data!F20": {"TIME_PERIOD": 4},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 2
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="debt_path",
    )
    assert "if time_period == 1:" in draft.body
    assert "debt_path(ctx, time_period=time_period - 1)" in draft.body
    # Group-local temporaries are disambiguated across fingerprint branches.
    assert any(name.startswith("_f") for name in draft.renameable_locals)
    _assert_body_compiles(draft, ("time_period",))


def test_unmatched_read_site_fails_synthesis() -> None:
    members = (
        _member(
            "Data!E20",
            "=Data!D4",
            "_t1 = read_price(ctx, time_period=99)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!F20",
            "=Data!E4",
            "_t1 = read_price(ctx, time_period=4)\nreturn xl_number(_t1)",
        ),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!D4": {"TIME_PERIOD": 3},
        "Data!E4": {"TIME_PERIOD": 4},
    }
    expected = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={"Data!D4": "price", "Data!E4": "price"},
    )
    assert summary.fallback_reason is None
    with pytest.raises(MechanicalSynthesisError):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="lagged_price",
        )


def test_range_reads_fail_synthesis() -> None:
    members = (
        _member(
            "Data!E20",
            "=Data!E4",
            "return xl_number(xl_range(ctx, 'Data!A1:B2'))",
        ),
        _member(
            "Data!F20",
            "=Data!F4",
            "return xl_number(xl_range(ctx, 'Data!A1:B2'))",
        ),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!E4": {"TIME_PERIOD": 4},
        "Data!F4": {"TIME_PERIOD": 5},
    }
    expected = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    with pytest.raises(MechanicalSynthesisError):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="observed_value",
        )


# --- Singleton body assembly (issue #45, phase 3) ---------------------------


def _wrapper_def(source: str) -> ast.FunctionDef:
    module = ast.parse(source)
    function_def = module.body[0]
    assert isinstance(function_def, ast.FunctionDef)
    return function_def


def test_parse_inlinable_wrapper_accepts_semantic_helper_with_literal_kwargs() -> None:
    wrapper = _wrapper_def(
        "def cell_engine_c10(ctx):\n"
        "    '''Formula: =IF(...)'''\n"
        "    return shock_active(ctx, time_period=1)\n"
    )
    assert parse_inlinable_wrapper(wrapper) == "shock_active(ctx, time_period=1)"


def test_parse_inlinable_wrapper_accepts_no_kwarg_helper_and_xl_cell() -> None:
    helper = _wrapper_def(
        "def cell_engine_b9(ctx):\n    return shock_magnitude_resolved(ctx)\n"
    )
    assert parse_inlinable_wrapper(helper) == "shock_magnitude_resolved(ctx)"
    reader = _wrapper_def(
        "def cell_inputs_b5(ctx):\n    return xl_cell(ctx, 'Inputs!B5')\n"
    )
    assert parse_inlinable_wrapper(reader) == "xl_cell(ctx, 'Inputs!B5')"


def test_parse_inlinable_wrapper_rejects_unsafe_shapes() -> None:
    multi_statement = _wrapper_def(
        "def cell_a_b1(ctx):\n    value = helper(ctx)\n    return value\n"
    )
    assert parse_inlinable_wrapper(multi_statement) is None
    non_literal = _wrapper_def(
        "def cell_a_b1(ctx):\n    return helper(ctx, time_period=other(ctx))\n"
    )
    assert parse_inlinable_wrapper(non_literal) is None
    extra_param = _wrapper_def("def cell_a_b1(ctx, extra):\n    return helper(ctx)\n")
    assert parse_inlinable_wrapper(extra_param) is None
    cell_target = _wrapper_def("def cell_a_b1(ctx):\n    return cell_a_b2(ctx)\n")
    assert parse_inlinable_wrapper(cell_target) is None


def test_singleton_inlines_cell_dependency_calls() -> None:
    draft = synthesize_singleton_body(
        "def cell_outputs_b12(ctx):\n"
        "    '''Formula: =Engine!C10*Inputs!B5.'''\n"
        "    _t1 = cell_engine_c10(ctx)\n"
        "    _t2 = cell_inputs_b5(ctx)\n"
        "    return xl_number(_t1) * xl_number(_t2)\n",
        inline_replacements={
            "cell_engine_c10": "shock_active(ctx, time_period=1)",
            "cell_inputs_b5": "xl_cell(ctx, 'Inputs!B5')",
        },
    )
    assert draft.body == (
        "_t1 = shock_active(ctx, time_period=1)\n"
        "_t2 = xl_cell(ctx, 'Inputs!B5')\n"
        "return xl_number(_t1) * xl_number(_t2)"
    )
    assert draft.renameable_locals == ("_t1", "_t2")
    assert draft.lookup_table_names == ()
    assert draft.group_count == 1


def test_singleton_preserves_runtime_reads_and_walrus_temporaries() -> None:
    draft = synthesize_singleton_body(
        "def cell_inputs_b6(ctx):\n"
        "    _t3 = read_country_name(ctx)\n"
        "    _t4 = xl_range(ctx, 'Inputs!A10:A12')\n"
        "    _t5 = xl_match(_t3, _t4, 0.0)\n"
        "    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), _t5, 2.0)"
        ", 0.0, 0.0) if (_t6 := xl_number(_t5)) else _t6\n",
        inline_replacements={},
    )
    assert "xl_range(ctx, 'Inputs!A10:A12')" in draft.body
    assert draft.renameable_locals == ("_t3", "_t4", "_t5", "_t6")


def test_singleton_rejects_non_inlinable_cell_dependency() -> None:
    with pytest.raises(MechanicalSynthesisError, match="cell_dependency_not_inlinable"):
        synthesize_singleton_body(
            "def cell_a_b1(ctx):\n    return xl_number(cell_a_b2(ctx))\n",
            inline_replacements={},
        )


def test_singleton_rejects_unexpected_cell_call_shape() -> None:
    with pytest.raises(MechanicalSynthesisError, match="cell_call_shape_unsupported"):
        synthesize_singleton_body(
            "def cell_a_b1(ctx):\n    return cell_a_b2(ctx, 1.0)\n",
            inline_replacements={"cell_a_b2": "helper(ctx)"},
        )


def test_singleton_rejects_bare_cell_reference() -> None:
    with pytest.raises(MechanicalSynthesisError, match="cell_reference_unsupported"):
        synthesize_singleton_body(
            "def cell_a_b1(ctx):\n    return apply(cell_a_b2, ctx)\n",
            inline_replacements={"cell_a_b2": "helper(ctx)"},
        )
