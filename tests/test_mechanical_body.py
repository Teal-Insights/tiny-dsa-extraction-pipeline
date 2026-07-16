"""Unit tests for mechanical cluster-body synthesis (issue #45)."""

from __future__ import annotations

import ast

import pytest

from src.internals_refactor import MemberContext
from src.mechanical_body import (
    MechanicalBodyDraft,
    MechanicalSynthesisError,
    synthesize_cluster_body,
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
