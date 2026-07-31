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
from src.refactor_bindings import BindingKeyValue, KeyConceptSpec
from src.refactor_fingerprints import (
    SemanticDependencyRef,
    build_cluster_fingerprint_summary,
)

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


def test_identity_sweep_xl_eval_reads_become_column_lookup() -> None:
    """Varying xl_eval addresses use the same templates as xl_cell (issue #67).

    The per-member ``cell_*`` callback cannot be kept as a single exemplar name,
    so the rewrite evaluates through ``xl_cell`` (resolver) with the templated
    address — same verification against recorded ref addresses.
    """
    members = (
        _member(
            "Data!E20",
            "=Data!E4",
            "_t1 = xl_eval(ctx, 'Data!E4', cell_data_e4)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!F20",
            "=Data!F4",
            "_t1 = xl_eval(ctx, 'Data!F4', cell_data_f4)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!G20",
            "=Data!G4",
            "_t1 = xl_eval(ctx, 'Data!G4', cell_data_g4)\nreturn xl_number(_t1)",
        ),
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
    assert "xl_eval(" not in draft.body
    assert "column_by_time_period" in draft.lookup_table_names
    _assert_body_compiles(draft, ("time_period",))


def test_constant_address_xl_eval_dependency_is_left_alone() -> None:
    """Shared xl_eval dependency address stays an xl_eval call (issue #67)."""
    members = (
        _member(
            "Data!E20",
            "=Data!A1",
            "_t1 = xl_eval(ctx, 'Data!A1', cell_data_a1)\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!F20",
            "=Data!A1",
            "_t1 = xl_eval(ctx, 'Data!A1', cell_data_a1)\nreturn xl_number(_t1)",
        ),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!A1": {"TIME_PERIOD": 1},
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
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="const_dep",
    )
    assert "xl_eval(ctx, 'Data!A1', cell_data_a1)" in draft.body
    assert "xl_cell(" not in draft.body
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


def test_tuple_lookup_accessor_read_becomes_tuple_subscript() -> None:
    """A ref key jointly determined by two member dims routes via a tuple table."""
    vocabulary = (
        KeyConceptSpec(
            dimension_id="INDICATOR",
            concept="INDICATOR",
            dtype="str",
            suggested_param_name="indicator",
        ),
        KeyConceptSpec(
            dimension_id="TIME_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="time_period",
        ),
    )
    members = (
        _member(
            "Data!B10",
            "=Hist!B2",
            "_t1 = read_hist(ctx, indicator_year='1991_nominal')\n"
            "return xl_number(_t1)",
        ),
        _member(
            "Data!B11",
            "=Hist!B3",
            "_t1 = read_hist(ctx, indicator_year='1991_real')\nreturn xl_number(_t1)",
        ),
        _member(
            "Data!C10",
            "=Hist!C2",
            "_t1 = read_hist(ctx, indicator_year='1992_nominal')\n"
            "return xl_number(_t1)",
        ),
        _member(
            "Data!C11",
            "=Hist!C3",
            "_t1 = read_hist(ctx, indicator_year='1992_real')\nreturn xl_number(_t1)",
        ),
    )
    bound_keys = {
        "Data!B10": {"INDICATOR": "nominal_gdp", "TIME_PERIOD": 1},
        "Data!B11": {"INDICATOR": "real_gdp", "TIME_PERIOD": 1},
        "Data!C10": {"INDICATOR": "nominal_gdp", "TIME_PERIOD": 2},
        "Data!C11": {"INDICATOR": "real_gdp", "TIME_PERIOD": 2},
        "Hist!B2": {"INDICATOR_YEAR": "1991_nominal"},
        "Hist!B3": {"INDICATOR_YEAR": "1991_real"},
        "Hist!C2": {"INDICATOR_YEAR": "1992_nominal"},
        "Hist!C3": {"INDICATOR_YEAR": "1992_real"},
    }
    expected = {
        address: bound_keys[address]
        for address in ("Data!B10", "Data!B11", "Data!C10", "Data!C11")
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={
            "Hist!B2": "hist",
            "Hist!B3": "hist",
            "Hist!C2": "hist",
            "Hist!C3": "hist",
        },
    )
    assert summary.fallback_reason is None
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=vocabulary,
        expected_member_keys=expected,
        helper_name="historical_value",
    )
    assert (
        "indicator_year_by_indicator_time_period = "
        "{('nominal_gdp', 1): '1991_nominal', ('nominal_gdp', 2): '1992_nominal', "
        "('real_gdp', 1): '1991_real', ('real_gdp', 2): '1992_real'}"
    ) in draft.body
    assert (
        "read_hist(ctx, indicator_year="
        "indicator_year_by_indicator_time_period[indicator, time_period])"
    ) in draft.body
    assert "indicator_year_by_indicator_time_period" in draft.lookup_table_names
    _assert_body_compiles(draft, ("indicator", "time_period"))


def test_self_recurrence_with_tuple_derived_period_routes_by_pair() -> None:
    """Self-recurrence whose target period depends on (scenario, period) pairs."""
    vocabulary = (
        KeyConceptSpec(
            dimension_id="SCENARIO",
            concept="SCENARIO",
            dtype="str",
            suggested_param_name="scenario",
        ),
        KeyConceptSpec(
            dimension_id="TIME_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="time_period",
        ),
    )
    members = (
        _member("Data!C20", "=Data!C4", "return xl_number(xl_cell(ctx, 'Data!C4'))"),
        _member("Data!C21", "=Data!C5", "return xl_number(xl_cell(ctx, 'Data!C5'))"),
        _member(
            "Data!D20",
            "=Data!C20+1",
            "_t1 = xl_eval(ctx, 'Data!C20', cell_data_c20)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
        _member(
            "Data!D21",
            "=Data!C21+1",
            "_t1 = xl_eval(ctx, 'Data!C21', cell_data_c21)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
        _member(
            "Data!E20",
            "=Data!D20+1",
            "_t1 = xl_eval(ctx, 'Data!D20', cell_data_d20)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
        _member(
            "Data!E21",
            "=Data!C21+1",
            "_t1 = xl_eval(ctx, 'Data!C21', cell_data_c21)\n"
            "return (xl_number(_t1) + xl_number(1.0))",
        ),
    )
    bound_keys = {
        "Data!C20": {"SCENARIO": "A", "TIME_PERIOD": 1},
        "Data!C21": {"SCENARIO": "B", "TIME_PERIOD": 1},
        "Data!D20": {"SCENARIO": "A", "TIME_PERIOD": 2},
        "Data!D21": {"SCENARIO": "B", "TIME_PERIOD": 2},
        "Data!E20": {"SCENARIO": "A", "TIME_PERIOD": 3},
        "Data!E21": {"SCENARIO": "B", "TIME_PERIOD": 3},
        "Data!C4": {"SCENARIO": "A", "TIME_PERIOD": 1},
        "Data!C5": {"SCENARIO": "B", "TIME_PERIOD": 1},
    }
    expected = {
        address: bound_keys[address]
        for address in (
            "Data!C20",
            "Data!C21",
            "Data!D20",
            "Data!D21",
            "Data!E20",
            "Data!E21",
        )
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
        key_vocabulary=vocabulary,
        expected_member_keys=expected,
        helper_name="debt_path",
    )
    assert "if time_period == 1:" in draft.body
    assert (
        "time_period_by_scenario_time_period = "
        "{('A', 2): 1, ('A', 3): 2, ('B', 2): 1, ('B', 3): 1}"
    ) in draft.body
    assert (
        "debt_path(ctx, scenario=scenario, "
        "time_period=time_period_by_scenario_time_period[scenario, time_period])"
    ) in draft.body
    assert "time_period_by_scenario_time_period" in draft.lookup_table_names
    _assert_body_compiles(draft, ("scenario", "time_period"))


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


# --- Constant-range INDEX/MATCH lookup family (issue #170) -------------------


def _constant_range_summary(bodies: tuple[str, str]):
    """Two members sharing one =SUM(Data!A1:B2) formula with custom bodies."""
    members = (
        _member("Data!E20", "=SUM(Data!A1:B2)", bodies[0]),
        _member("Data!F20", "=SUM(Data!A1:B2)", bodies[1]),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!A1": {"LABEL_ROW": 1},
        "Data!B2": {"LABEL_ROW": 2},
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
    return summary, expected


def test_constant_range_read_passes_through_verbatim() -> None:
    body = "return xl_number(xl_range(ctx, 'Data!A1:B2'))"
    summary, expected = _constant_range_summary((body, body))
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="observed_value",
    )
    assert "xl_range(ctx, 'Data!A1:B2')" in draft.body
    assert draft.lookup_table_names == ()
    _assert_body_compiles(draft, ("time_period",))


def test_non_literal_range_address_fails_synthesis() -> None:
    body = "_t1 = read_label(ctx)\nreturn xl_number(xl_range(ctx, _t1))"
    summary, expected = _constant_range_summary((body, body))
    with pytest.raises(MechanicalSynthesisError, match="non_literal_range_address"):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="observed_value",
        )


def test_range_rows_reads_stay_unsupported() -> None:
    body = "return xl_range_rows(ctx, 'Data!A1:B2')"
    summary, expected = _constant_range_summary((body, body))
    with pytest.raises(
        MechanicalSynthesisError, match="unsupported_read_callee:xl_range_rows"
    ):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="observed_value",
        )


def test_varying_range_endpoints_fail_synthesis() -> None:
    members = (
        _member(
            "Data!E20",
            "=SUM(Data!A1:B2)",
            "return xl_number(xl_range(ctx, 'Data!A1:B2'))",
        ),
        _member(
            "Data!F20",
            "=SUM(Data!C1:D2)",
            "return xl_number(xl_range(ctx, 'Data!C1:D2'))",
        ),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        "Data!A1": {"TIME_PERIOD": 4},
        "Data!B2": {"TIME_PERIOD": 4},
        "Data!C1": {"TIME_PERIOD": 5},
        "Data!D2": {"TIME_PERIOD": 5},
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
    with pytest.raises(MechanicalSynthesisError, match="range_endpoints_vary"):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="observed_value",
        )


def _index_family_body(c1: int, c2: int, *, trailing: str = "0.0, 0.0") -> str:
    return (
        "_t1 = read_label(ctx)\n"
        "_t2 = xl_range(ctx, 'DB!A2:A4')\n"
        "_t3 = xl_match(_t1, _t2, 0.0)\n"
        f"return xl_offset(ctx, xl_index_ref(('DB', 2, {c1}, 4, {c2}), _t3, 1.0)"
        f", {trailing})"
    )


def _index_family_summary(
    formulas: tuple[str, str, str],
    bodies: tuple[str, str, str],
    *,
    base_bound_keys: dict[str, dict[str, BindingKeyValue]],
):
    members = tuple(
        _member(address, formula, body)
        for address, formula, body in zip(
            ("Data!B10", "Data!C10", "Data!D10"), formulas, bodies, strict=True
        )
    )
    bound_keys: dict[str, dict[str, BindingKeyValue]] = {
        "Data!B10": {"TIME_PERIOD": 1},
        "Data!C10": {"TIME_PERIOD": 2},
        "Data!D10": {"TIME_PERIOD": 3},
        "Data!A1": {"COUNTRY": "chile"},
        "DB!A2": {"LABEL_ROW": 2},
        "DB!A4": {"LABEL_ROW": 4},
    }
    bound_keys.update(base_bound_keys)
    expected = {
        "Data!B10": {"TIME_PERIOD": 1},
        "Data!C10": {"TIME_PERIOD": 2},
        "Data!D10": {"TIME_PERIOD": 3},
    }
    # Bind INDEX corners so same-sheet column sweeps stay one group under the
    # unbound sheet/row geometry guard (mixed unbound geometry falls back).
    address_to_series_id = {
        "Data!A1": "label",
        "DB!A2": "label_range",
        "DB!A4": "label_range",
        "Data!B10": "climate_lookup",
        "Data!C10": "climate_lookup",
        "Data!D10": "climate_lookup",
    }
    for address in base_bound_keys:
        address_to_series_id.setdefault(address, "index_range")
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id=address_to_series_id,
    )
    assert summary.fallback_reason is None
    return summary, expected


def test_index_match_column_sweep_synthesizes_tuple_lookup_tables() -> None:
    """The qcraft member shape: constant label range + per-member column sweep."""
    summary, expected = _index_family_summary(
        (
            "=INDEX(DB!C2:E4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!D2:F4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!E2:G4,MATCH(Data!A1,DB!A2:A4,0),1)",
        ),
        (
            _index_family_body(3, 5),
            _index_family_body(4, 6),
            _index_family_body(5, 7),
        ),
        base_bound_keys={
            "DB!C2": {"TIME_PERIOD": 1},
            "DB!D2": {"TIME_PERIOD": 2},
            "DB!E2": {"TIME_PERIOD": 3},
            "DB!E4": {"TIME_PERIOD": 1},
            "DB!F4": {"TIME_PERIOD": 2},
            "DB!G4": {"TIME_PERIOD": 3},
        },
    )
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="climate_lookup",
    )
    assert "xl_range(ctx, 'DB!A2:A4')" in draft.body
    assert "xl_match(_t1, _t2, 0.0)" in draft.body
    assert "col_start_index_by_time_period = {1: 3, 2: 4, 3: 5}" in draft.body
    assert "col_end_index_by_time_period = {1: 5, 2: 6, 3: 7}" in draft.body
    assert (
        "xl_offset(ctx, xl_index_ref(('DB', 2, "
        "col_start_index_by_time_period[time_period], 4, "
        "col_end_index_by_time_period[time_period]), _t3, 1.0), 0.0, 0.0)"
    ) in draft.body
    assert "col_start_index_by_time_period" in draft.lookup_table_names
    assert "col_end_index_by_time_period" in draft.lookup_table_names
    _assert_body_compiles(draft, ("time_period",))


def test_index_ref_tuple_sheet_mismatch_fails_fingerprint() -> None:
    """Cross-sheet unbound INDEX corners fall back under sheet/row geometry policy.

    With a non-empty ``address_to_series_id``, mixed sheet/row among unbound
    ref-slot operands is rejected at fingerprint time (before synthesis).
    """
    members = tuple(
        _member(address, formula, body)
        for address, formula, body in zip(
            ("Data!B10", "Data!C10", "Data!D10"),
            (
                "=INDEX(DB!C2:E4,MATCH(Data!A1,DB!A2:A4,0),1)",
                "=INDEX(Other!D2:F4,MATCH(Data!A1,DB!A2:A4,0),1)",
                "=INDEX(DB!E2:G4,MATCH(Data!A1,DB!A2:A4,0),1)",
            ),
            (
                _index_family_body(3, 5),
                _index_family_body(4, 6),
                _index_family_body(5, 7),
            ),
            strict=True,
        )
    )
    bound_keys: dict[str, dict[str, BindingKeyValue]] = {
        "Data!B10": {"TIME_PERIOD": 1},
        "Data!C10": {"TIME_PERIOD": 2},
        "Data!D10": {"TIME_PERIOD": 3},
        "Data!A1": {"COUNTRY": "chile"},
        "DB!A2": {"LABEL_ROW": 2},
        "DB!A4": {"LABEL_ROW": 4},
        "DB!C2": {"TIME_PERIOD": 1},
        "Other!D2": {"TIME_PERIOD": 2},
        "DB!E2": {"TIME_PERIOD": 3},
        "DB!E4": {"TIME_PERIOD": 1},
        "Other!F4": {"TIME_PERIOD": 2},
        "DB!G4": {"TIME_PERIOD": 3},
    }
    expected = {
        "Data!B10": {"TIME_PERIOD": 1},
        "Data!C10": {"TIME_PERIOD": 2},
        "Data!D10": {"TIME_PERIOD": 3},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={"Data!A1": "label"},
    )
    assert summary.fallback_reason is not None
    assert "unbound_ref_slot_geometry_conflict" in summary.fallback_reason
    assert summary.groups == ()


def test_nonzero_offset_trailing_args_fail_synthesis() -> None:
    bodies = (
        _index_family_body(3, 5, trailing="1.0, 0.0"),
        _index_family_body(4, 6, trailing="1.0, 0.0"),
        _index_family_body(5, 7, trailing="1.0, 0.0"),
    )
    summary, expected = _index_family_summary(
        (
            "=INDEX(DB!C2:E4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!D2:F4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!E2:G4,MATCH(Data!A1,DB!A2:A4,0),1)",
        ),
        bodies,
        base_bound_keys={
            "DB!C2": {"TIME_PERIOD": 1},
            "DB!D2": {"TIME_PERIOD": 2},
            "DB!E2": {"TIME_PERIOD": 3},
            "DB!E4": {"TIME_PERIOD": 1},
            "DB!F4": {"TIME_PERIOD": 2},
            "DB!G4": {"TIME_PERIOD": 3},
        },
    )
    with pytest.raises(MechanicalSynthesisError, match="unsupported_offset_shape"):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="climate_lookup",
        )


def test_bare_index_ref_fails_synthesis() -> None:
    body = (
        "_t1 = read_label(ctx)\n"
        "_t2 = xl_range(ctx, 'DB!A2:A4')\n"
        "_t3 = xl_match(_t1, _t2, 0.0)\n"
        "return xl_index_ref(('DB', 2, 3, 4, 5), _t3, 1.0)"
    )
    summary, expected = _index_family_summary(
        (
            "=INDEX(DB!C2:E4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!D2:F4,MATCH(Data!A1,DB!A2:A4,0),1)",
            "=INDEX(DB!E2:G4,MATCH(Data!A1,DB!A2:A4,0),1)",
        ),
        (body, body, body),
        base_bound_keys={
            "DB!C2": {"TIME_PERIOD": 1},
            "DB!D2": {"TIME_PERIOD": 2},
            "DB!E2": {"TIME_PERIOD": 3},
            "DB!E4": {"TIME_PERIOD": 1},
            "DB!F4": {"TIME_PERIOD": 2},
            "DB!G4": {"TIME_PERIOD": 3},
        },
    )
    with pytest.raises(MechanicalSynthesisError, match="unsupported_index_ref_shape"):
        synthesize_cluster_body(
            summary,
            key_vocabulary=TIME_PERIOD_VOCAB,
            expected_member_keys=expected,
            helper_name="climate_lookup",
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


def test_mixed_ref_series_regimes_split_then_synthesize() -> None:
    """Shared skeleton with mixed ``ref_1`` series → regime groups then synthesize.

    Without the split, fingerprinting collapses three operand series into one
    ``table[TIME_PERIOD]`` and mechanical synthesis fails with
    ``slots_without_read_sites``. After the split each regime synthesizes with
    its own accessor and multi-group routing selects by ``TIME_PERIOD``.

    The inflation_path band uses ≥2 members so synthesis must derive the
    header offset (``+27``) rather than hard-coding the ``2055`` key.
    """
    members = (
        _member(
            "Rate!B19",
            "=(1+Anchor!B5/100)*(1+Macro!AE15/100)*100-100",
            "_t1 = xl_number(xl_cell(ctx, 'Anchor!B5'))\n"
            "_t2 = xl_number("
            "read_macrofiscal_gdp_deflator_growth(ctx, time_period=2029))\n"
            "return (1 + _t1 / 100) * (1 + _t2 / 100) * 100 - 100",
        ),
        _member(
            "Rate!C19",
            "=(1+Anchor!B5/100)*(1+Inflation!B9/100)*100-100",
            "_t1 = xl_number(xl_cell(ctx, 'Anchor!B5'))\n"
            "_t2 = xl_number("
            "read_inflation_convergence_trajectory(ctx, time_period=2002))\n"
            "return (1 + _t1 / 100) * (1 + _t2 / 100) * 100 - 100",
        ),
        _member(
            "Rate!D19",
            "=(1+Anchor!B5/100)*(1+Inflation!C9/100)*100-100",
            "_t1 = xl_number(xl_cell(ctx, 'Anchor!B5'))\n"
            "_t2 = xl_number("
            "read_inflation_convergence_trajectory(ctx, time_period=2003))\n"
            "return (1 + _t1 / 100) * (1 + _t2 / 100) * 100 - 100",
        ),
        _member(
            "Rate!E19",
            "=(1+Anchor!B5/100)*(1+Inflation!BC3/100)*100-100",
            "_t1 = xl_number(xl_cell(ctx, 'Anchor!B5'))\n"
            "_t2 = xl_number(read_inflation_path(ctx, time_period=2055))\n"
            "return (1 + _t1 / 100) * (1 + _t2 / 100) * 100 - 100",
        ),
        _member(
            "Rate!F19",
            "=(1+Anchor!B5/100)*(1+Inflation!BD3/100)*100-100",
            "_t1 = xl_number(xl_cell(ctx, 'Anchor!B5'))\n"
            "_t2 = xl_number(read_inflation_path(ctx, time_period=2056))\n"
            "return (1 + _t1 / 100) * (1 + _t2 / 100) * 100 - 100",
        ),
    )
    bound_keys = {
        "Rate!B19": {"TIME_PERIOD": 2002},
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2004},
        "Rate!E19": {"TIME_PERIOD": 2028},
        "Rate!F19": {"TIME_PERIOD": 2029},
        "Anchor!B5": {},
        "Macro!AE15": {"TIME_PERIOD": 2029},
        "Inflation!B9": {"TIME_PERIOD": 2002},
        "Inflation!C9": {"TIME_PERIOD": 2003},
        "Inflation!BC3": {"TIME_PERIOD": 2055},
        "Inflation!BD3": {"TIME_PERIOD": 2056},
    }
    expected = {
        "Rate!B19": {"TIME_PERIOD": 2002},
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2004},
        "Rate!E19": {"TIME_PERIOD": 2028},
        "Rate!F19": {"TIME_PERIOD": 2029},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={
            "Anchor!B5": "anchor_series",
            "Macro!AE15": "macrofiscal_gdp_deflator_growth",
            "Inflation!B9": "inflation_convergence_trajectory",
            "Inflation!C9": "inflation_convergence_trajectory",
            "Inflation!BC3": "inflation_path",
            "Inflation!BD3": "inflation_path",
        },
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 3
    series_by_members = {
        group.members: group.ref_relations[1].series_id for group in summary.groups
    }
    assert series_by_members[("Rate!B19",)] == "macrofiscal_gdp_deflator_growth"
    assert (
        series_by_members[("Rate!C19", "Rate!D19")]
        == "inflation_convergence_trajectory"
    )
    assert series_by_members[("Rate!E19", "Rate!F19")] == "inflation_path"
    path_group = next(
        group for group in summary.groups if group.members == ("Rate!E19", "Rate!F19")
    )
    assert path_group.ref_relations[1].tier == "offset"
    assert path_group.ref_relations[1].offsets == {"TIME_PERIOD": 27}

    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="interest_rate_long_run_real_interest_rate",
    )
    assert "if time_period == 2002:" in draft.body
    assert "read_macrofiscal_gdp_deflator_growth(" in draft.body
    assert (
        "read_inflation_convergence_trajectory(ctx, time_period=time_period - 1)"
        in draft.body
    )
    assert "read_inflation_path(ctx, time_period=time_period + 27)" in draft.body
    assert "time_period=2055" not in draft.body
    # Must not collapse regimes into one literal 2028→2055 helper key table.
    assert "2028: 2055" not in draft.body
    _assert_body_compiles(draft, ("time_period",))


def test_peel_boundary_lag_routes_to_sibling_helper_not_xl_cell() -> None:
    """The later peel's first member lags into the sibling helper (issue #138).

    ``paris_engine_indicators`` is peeled into two schedule units; this is the
    later one (``paris_engine_indicators_2``). Its first member's ``t-1`` operand
    was computed by the earlier unit, so its wrapper is already collapsed into a
    literal ``paris_engine_indicators(ctx, time_period=2029)`` call, while every
    other member lags into this unit itself. Both regimes must route through a
    helper — a raw ``xl_cell`` read here defeats the refactor.
    """
    members = (
        _member(
            "Paris!E35",
            "=Paris!D35*(1+Paris!E32/100)",
            "_t1 = paris_engine_indicators(ctx, time_period=2029)\n"
            "_t2 = paris_engine_weighted_interest_rate(ctx, time_period=2030)\n"
            "return xl_number(_t1) * (xl_number(1.0) + xl_number(_t2) / 100.0)",
        ),
        _member(
            "Paris!F35",
            "=Paris!E35*(1+Paris!F32/100)",
            "_t1 = xl_eval(ctx, 'Paris!E35', cell_paris_e35)\n"
            "_t2 = paris_engine_weighted_interest_rate(ctx, time_period=2031)\n"
            "return xl_number(_t1) * (xl_number(1.0) + xl_number(_t2) / 100.0)",
        ),
        _member(
            "Paris!G35",
            "=Paris!F35*(1+Paris!G32/100)",
            "_t1 = xl_eval(ctx, 'Paris!F35', cell_paris_f35)\n"
            "_t2 = paris_engine_weighted_interest_rate(ctx, time_period=2032)\n"
            "return xl_number(_t1) * (xl_number(1.0) + xl_number(_t2) / 100.0)",
        ),
    )
    bound_keys = {
        "Paris!D35": {"TIME_PERIOD": 2029},
        "Paris!E35": {"TIME_PERIOD": 2030},
        "Paris!F35": {"TIME_PERIOD": 2031},
        "Paris!G35": {"TIME_PERIOD": 2032},
        "Paris!E32": {"TIME_PERIOD": 2030},
        "Paris!F32": {"TIME_PERIOD": 2031},
        "Paris!G32": {"TIME_PERIOD": 2032},
    }
    expected = {
        "Paris!E35": {"TIME_PERIOD": 2030},
        "Paris!F35": {"TIME_PERIOD": 2031},
        "Paris!G35": {"TIME_PERIOD": 2032},
    }
    address_to_series_id = {
        address: "paris_engine_indicators"
        for address in ("Paris!D35", "Paris!E35", "Paris!F35", "Paris!G35")
    } | {
        address: "paris_engine_weighted_interest_rate"
        for address in ("Paris!E32", "Paris!F32", "Paris!G32")
    }
    semantic_dependencies = (
        SemanticDependencyRef(
            helper_name="paris_engine_indicators",
            call_form="paris_engine_indicators(ctx, time_period=time_period)",
            address_template="Paris!{col}35",
            addresses=("Paris!D35",),
        ),
        SemanticDependencyRef(
            helper_name="paris_engine_weighted_interest_rate",
            call_form=(
                "paris_engine_weighted_interest_rate(ctx, time_period=time_period)"
            ),
            address_template="Paris!{col}32",
            addresses=("Paris!E32", "Paris!F32", "Paris!G32"),
        ),
    )
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id=address_to_series_id,
        semantic_dependencies=semantic_dependencies,
    )
    assert summary.fallback_reason is None
    draft = synthesize_cluster_body(
        summary,
        key_vocabulary=TIME_PERIOD_VOCAB,
        expected_member_keys=expected,
        helper_name="paris_engine_indicators_2",
    )
    assert "if time_period == 2030:" in draft.body
    assert "paris_engine_indicators(ctx, time_period=2029)" in draft.body
    assert "paris_engine_indicators_2(ctx, time_period=time_period - 1)" in draft.body
    assert (
        "paris_engine_weighted_interest_rate(ctx, time_period=time_period)"
        in draft.body
    )
    assert "xl_cell" not in draft.body
    _assert_body_compiles(draft, ("time_period",))
