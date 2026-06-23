from __future__ import annotations

import ast
from dataclasses import replace

from src.internals_refactor import (
    ClusterRefactorResponse,
    MemberBinding,
    PROJECTION_ALIASES_MARKER,
    apply_phase_b_final_pass,
    apply_phase_c,
    apply_refactor_plan,
    deterministic_helper_name,
    refactor_cache_key,
    validate_cluster_refactor_response,
    validate_refactored_internals,
    wrapper_source,
)

SHOCK_ACTIVE_HELPER = '''
def shock_active(ctx, col):
    """Engine!C10:G10 — 1 if projection year >= shock year."""
    return (
        _t2
        if isinstance(
            (
                _t2 := to_bool(
                    (_t1 := xl_ge(xl_cell(ctx, f"Engine!{col}5"), xl_cell(ctx, "Inputs!B21")))
                )
            ),
            XlError,
        )
        else ((1.0) if _t2 else (0.0))
    )
'''.strip()

TRIM_FIXTURE_SOURCE = """
from .runtime import XlError, xl_cell

def shock_active(ctx, col):
    return 1.0

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Engine!C10': ('shock_active', 'C'),
    'Outputs!B14': ('output_delta', 'C'),
}

def _address_to_func_name(address):
    return "cell_placeholder"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    dispatch = _ADDRESS_DISPATCH.get(address)
    if dispatch is not None:
        helper_name, column = dispatch
        helper = globals()[helper_name]

        def _bound(ctx, _helper=helper, _column=column):
            return _helper(ctx, _column)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    return None
"""


def test_build_cluster_refactor_context_row_10(shock_cluster_context) -> None:
    ctx = shock_cluster_context

    assert ctx.cluster_id >= 0
    assert ctx.row == 10
    assert ctx.canonical_template == "=IF(Engine!{COL}5>=Inputs!B21,1,0)"
    assert ctx.first_year_column == "C"
    assert len(ctx.members) == 5
    assert {member.address for member in ctx.members} == {
        "Engine!C10",
        "Engine!D10",
        "Engine!E10",
        "Engine!F10",
        "Engine!G10",
    }

    c10 = next(member for member in ctx.members if member.address == "Engine!C10")
    assert c10.function_name == "cell_engine_c10"
    assert c10.engine_column == "C"
    assert c10.normalized_formula == "=IF(Engine!C5>=Inputs!B21,1,0)"
    assert "def cell_engine_c10" in c10.python_source
    assert c10.dependency_addresses == ("Engine!C5", "Inputs!B21")
    assert c10.dependency_functions == ()

    assert ctx.external_dependencies == ()
    xl_eval_sites = [site for site in ctx.call_sites if site.pattern == "xl_eval"]
    if xl_eval_sites:
        assert any(site.callee_address == "Engine!C10" for site in xl_eval_sites)


def test_wrapper_source() -> None:
    assert wrapper_source("shock_active", "C") == 'return shock_active(ctx, "C")'


def test_deterministic_helper_name_row_10(shock_cluster_context) -> None:
    assert deterministic_helper_name(shock_cluster_context) == "shock_active"


def test_validate_cluster_refactor_response_row_10(
    shock_cluster_context,
    codegen_internals_source,
) -> None:
    response = _shock_active_response(shock_cluster_context)
    validate_cluster_refactor_response(
        shock_cluster_context,
        response,
        existing_names=_function_names(codegen_internals_source),
    )


def test_apply_phase_a_row_10(
    shock_cluster_context,
    codegen_internals_source,
) -> None:
    response = _shock_active_response(shock_cluster_context)
    validate_cluster_refactor_response(
        shock_cluster_context,
        response,
        existing_names=_function_names(codegen_internals_source),
    )

    result = apply_refactor_plan(
        codegen_internals_source,
        response,
        shock_cluster_context,
    )
    validate_refactored_internals(result)

    assert "def shock_active(ctx, col)" in result
    for member in shock_cluster_context.members:
        assert f'return shock_active(ctx, "{member.engine_column}")' in result
        assert member.function_name in result

    for member in shock_cluster_context.members:
        function_block = _extract_function(result, member.function_name)
        assert function_block.count("return ") == 1
        assert "xl_ge" not in function_block


def test_apply_phase_b_rewrites_helper_and_projection_call_sites(
    phase_a_internals_source,
    cluster_refactor_responses,
) -> None:
    updated, rewrite_count = apply_phase_b_final_pass(
        phase_a_internals_source,
        cluster_refactor_responses,
    )
    validate_refactored_internals(updated)

    debt_to_gdp = _extract_function(updated, "debt_to_gdp")
    assert "xl_eval(ctx, 'Engine!C10', cell_engine_c10)" not in debt_to_gdp
    assert "shock_active(ctx, col)" in debt_to_gdp
    assert "primary_balance_shocked(ctx, col)" in debt_to_gdp

    primary_balance = _extract_function(updated, "primary_balance_shocked")
    assert "ten_func_map" not in primary_balance
    assert "shock_active(ctx, col)" in primary_balance

    output_delta = _extract_function(updated, "output_delta")
    assert "fn20" not in output_delta
    assert "debt_to_gdp(ctx, col)" in output_delta
    assert "baseline_debt(ctx, col)" in output_delta

    assert 'return baseline_debt(ctx, "C")' in _extract_function(
        updated, "cell_outputs_b12"
    )
    assert 'return debt_to_gdp(ctx, "C")' in _extract_function(
        updated, "cell_outputs_b13"
    )

    second_pass, second_rewrite_count = apply_phase_b_final_pass(
        updated,
        cluster_refactor_responses,
    )
    assert second_pass == updated
    assert second_rewrite_count == 0
    assert rewrite_count >= 0


def test_apply_phase_c_prunes_unreferenced_thin_wrappers(
    phase_a_internals_source,
    cluster_refactor_responses,
) -> None:
    phase_b_source, _phase_b_rewrites = apply_phase_b_final_pass(
        phase_a_internals_source,
        cluster_refactor_responses,
    )
    updated, pruned = apply_phase_c(phase_b_source)
    validate_refactored_internals(updated)

    assert pruned > 0
    assert "def cell_engine_c16(ctx)" not in updated
    assert "def cell_outputs_b14(ctx)" not in updated
    assert "def cell_outputs_b12(ctx)" not in updated
    assert "def cell_inputs_b6(ctx)" in updated
    assert "def cell_engine_b9(ctx)" in updated
    dispatch = _extract_address_dispatch(updated)
    assert "Engine!C16" not in dispatch
    assert dispatch["Outputs!B14"] == ("output_delta", "C")
    assert PROJECTION_ALIASES_MARKER not in updated

    second_pass, second_pruned = apply_phase_c(updated)
    assert second_pass == updated
    assert second_pruned == 0


def test_apply_phase_c_resolver_dispatches_pruned_addresses(
    phase_bc_internals_source,
) -> None:
    dispatch = _extract_address_dispatch(phase_bc_internals_source)
    assert "Engine!C16" not in dispatch
    assert dispatch["Outputs!B12"] == ("baseline_debt", "C")
    assert dispatch["Outputs!B13"] == ("debt_to_gdp", "C")


def test_apply_phase_c_trim_removes_engine_dispatch_entries() -> None:
    updated, removed = apply_phase_c(TRIM_FIXTURE_SOURCE)
    validate_refactored_internals(updated)
    dispatch = _extract_address_dispatch(updated)

    assert removed == 1
    assert "Engine!C10" not in dispatch
    assert dispatch["Outputs!B14"] == ("output_delta", "C")


def test_apply_phase_c_preserves_compute_all(refactored_tiny_dsa_api) -> None:
    results = refactored_tiny_dsa_api.compute_all(
        refactored_tiny_dsa_api.make_context()
    )
    assert len(results) == 3


def test_refactor_cache_key_is_stable(
    shock_cluster_context,
    codegen_internals_path,
) -> None:
    internals_bytes = codegen_internals_path.read_bytes()
    schema = ClusterRefactorResponse.model_json_schema()
    key_a = refactor_cache_key(shock_cluster_context, internals_bytes, schema)
    key_b = refactor_cache_key(shock_cluster_context, internals_bytes, schema)
    assert key_a == key_b
    assert len(key_a) == 64


def test_refactor_cache_key_changes_when_member_source_changes(
    shock_cluster_context,
    codegen_internals_path,
) -> None:
    schema = ClusterRefactorResponse.model_json_schema()
    internals_bytes = codegen_internals_path.read_bytes()
    baseline = refactor_cache_key(shock_cluster_context, internals_bytes, schema)

    mutated_member = shock_cluster_context.members[0]
    replacement = shock_cluster_context.members[1]
    mutated_members = tuple(
        replacement if member.address == mutated_member.address else member
        for member in shock_cluster_context.members
    )
    mutated_ctx = replace(shock_cluster_context, members=mutated_members)
    assert refactor_cache_key(mutated_ctx, internals_bytes, schema) != baseline


def _extract_address_dispatch(source: str) -> dict[str, tuple[str, str]]:
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ADDRESS_DISPATCH":
                    assert isinstance(node.value, ast.Dict)
                    dispatch: dict[str, tuple[str, str]] = {}
                    for key, value in zip(node.value.keys, node.value.values):
                        assert isinstance(key, ast.Constant) and isinstance(
                            key.value, str
                        )
                        assert isinstance(value, ast.Tuple) and len(value.elts) == 2
                        helper = value.elts[0]
                        column = value.elts[1]
                        assert isinstance(helper, ast.Constant) and isinstance(
                            helper.value, str
                        )
                        assert isinstance(column, ast.Constant) and isinstance(
                            column.value, str
                        )
                        dispatch[key.value] = (helper.value, column.value)
                    return dispatch
    raise AssertionError("_ADDRESS_DISPATCH not found")


def _function_names(source: str) -> frozenset[str]:
    module = ast.parse(source)
    return frozenset(
        node.name for node in module.body if isinstance(node, ast.FunctionDef)
    )


def _extract_function(source: str, function_name: str) -> str:
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise KeyError(function_name)


def _shock_active_response(ctx) -> ClusterRefactorResponse:
    return ClusterRefactorResponse(
        helper_name=deterministic_helper_name(ctx),
        helper_docstring="Engine!C10:G10 — 1 if projection year >= shock year.",
        uses_first_year_branch=False,
        helper_source=SHOCK_ACTIVE_HELPER,
        member_bindings=tuple(
            MemberBinding(
                address=member.address,
                function_name=member.function_name,
                engine_column=member.engine_column,
            )
            for member in ctx.members
        ),
    )
