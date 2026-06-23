from __future__ import annotations

import ast
from dataclasses import replace
from typing import Any, cast

from src.internals_refactor import (
    ClusterRefactorResponse,
    MemberBinding,
    PROJECTION_ALIASES_MARKER,
    SemanticDependency,
    apply_phase_b_final_pass,
    apply_phase_c,
    apply_refactor_plan,
    apply_singleton_refactor_plan,
    build_cluster_refactor_context,
    build_singleton_refactor_context,
    deterministic_helper_name,
    prompt_payload,
    refactor_cache_key,
    resolve_semantic_dependencies,
    validate_cluster_refactor_response,
    validate_google_style_docstring,
    validate_refactored_internals,
    validate_semantic_local_names,
    validate_singleton_refactor_response,
    wrapper_source,
    _align_singleton_response_docstring,
    _normalize_google_docstring,
    _prepare_cluster_refactor_response,
)
from src.tiny_dsa_phase_b_plugins import TINY_DSA_PHASE_B_PLUGINS

from tests.fixtures.cluster_refactor_golden import (
    GOLDEN_CLUSTER_REFACTOR_RESPONSES,
    SHOCK_ACTIVE_DOCSTRING,
    SHOCK_ACTIVE_HELPER,
)
from tests.fixtures.singleton_refactor_golden import GOLDEN_SINGLETON_REFACTOR_RESPONSES

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
_SYMBOL_DISPATCH = {}

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


def test_validate_runnable_cell_imports_rejects_workbook() -> None:
    from src.qmd_python_validation import validate_runnable_cell_imports

    source = (
        "from tiny_dsa.api import Workbook, compute_output_baseline\n"
        "compute_output_baseline(ctx=ctx)\n"
    )
    try:
        validate_runnable_cell_imports(source)
    except ValueError as error:
        assert "Workbook" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_validate_no_cell_function_references_rejects_cell_helpers() -> None:
    from src.internals_refactor import validate_no_cell_function_references

    source = """
def debt_to_gdp(ctx, col):
    return xl_eval(ctx, 'Engine!C10', cell_engine_c10)
"""
    function_def = ast.parse(source).body[0]
    assert isinstance(function_def, ast.FunctionDef)
    try:
        validate_no_cell_function_references(function_def)
    except ValueError as error:
        assert "cell_engine_c10" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_validate_semantic_local_names_rejects_excel_shaped_names() -> None:
    source = """
def example(ctx, col):
    t1 = xl_cell(ctx, "Inputs!B21")
    return t1
"""
    function_def = ast.parse(source).body[0]
    assert isinstance(function_def, ast.FunctionDef)
    try:
        validate_semantic_local_names(function_def)
    except ValueError as error:
        assert "excel-shaped" in str(error)
        assert "t1" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_validate_google_style_docstring_accepts_complete_docstring() -> None:
    validate_google_style_docstring(SHOCK_ACTIVE_DOCSTRING)


def test_validate_google_style_docstring_accepts_returns_before_args() -> None:
    docstring = """Resolve the shock magnitude.

Returns:
    Shock magnitude from the shock table.

Args:
    ctx: Workbook evaluation context.
"""
    validate_google_style_docstring(docstring)


def test_normalize_google_docstring_strips_interstitial_prose() -> None:
    docstring = """Look up the initial debt-to-GDP ratio.

This helper reads the country profile table in Inputs.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio.
"""
    normalized = _normalize_google_docstring(docstring)
    validate_google_style_docstring(normalized)
    assert normalized.startswith("Look up the initial debt-to-GDP ratio.\n\nArgs:\n")


def test_build_singleton_refactor_context_inputs_b6(
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> None:
    from src.formula_clustering import cluster_graph_formulas

    cluster = next(
        cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if cluster.members == ("Inputs!B6",)
    )
    ctx = build_singleton_refactor_context(
        tiny_dsa_refactor_projection,
        cluster,
        codegen_internals_path,
    )
    assert ctx is not None
    assert ctx.address == "Inputs!B6"
    assert ctx.function_name == "cell_inputs_b6"
    assert ctx.symbol_name == "initial_debt_to_gdp"
    assert "def cell_inputs_b6" in ctx.python_source


def test_apply_singleton_rename_rewrites_xl_eval_call_sites(
    codegen_internals_source,
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> None:
    from src.formula_clustering import cluster_graph_formulas

    cluster = next(
        cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if cluster.members == ("Inputs!B6",)
    )
    ctx = build_singleton_refactor_context(
        tiny_dsa_refactor_projection,
        cluster,
        codegen_internals_path,
    )
    assert ctx is not None
    response = GOLDEN_SINGLETON_REFACTOR_RESPONSES["Inputs!B6"]
    validate_singleton_refactor_response(
        ctx,
        response,
        existing_names=_function_names(codegen_internals_source),
    )
    updated, rewrite_count = apply_singleton_refactor_plan(
        codegen_internals_source,
        response,
        ctx,
    )
    validate_refactored_internals(updated)

    assert "def initial_debt_to_gdp(ctx)" in updated
    assert "def cell_inputs_b6(ctx)" not in updated
    assert "cell_inputs_b6" not in updated
    assert rewrite_count > 0
    assert "'Inputs!B6': 'initial_debt_to_gdp'" in updated


def test_golden_singleton_refactor_responses_validate(
    codegen_internals_source,
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> None:
    from src.formula_clustering import cluster_graph_formulas

    singleton_clusters = {
        cluster.members[0]: cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if len(cluster.members) == 1
    }
    existing_names = _function_names(codegen_internals_source)
    for address in ("Inputs!B6", "Engine!B9"):
        ctx = build_singleton_refactor_context(
            tiny_dsa_refactor_projection,
            singleton_clusters[address],
            codegen_internals_path,
        )
        assert ctx is not None
        response = GOLDEN_SINGLETON_REFACTOR_RESPONSES[address]
        validate_singleton_refactor_response(
            ctx,
            response,
            existing_names=existing_names,
        )
        existing_names = existing_names | {response.symbol_name}


def test_golden_cluster_refactor_responses_validate(
    singleton_refactored_internals_source,
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> None:
    from src.formula_clustering import cluster_graph_formulas

    from src.refactor_order import compute_multi_member_cluster_refactor_order

    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    existing_names = _function_names(singleton_refactored_internals_source)
    for cluster in compute_multi_member_cluster_refactor_order(
        tiny_dsa_refactor_projection, clusters
    ):
        if cluster.row is None:
            continue
        ctx = build_cluster_refactor_context(
            tiny_dsa_refactor_projection,
            cluster,
            codegen_internals_path,
        )
        assert ctx is not None
        response = GOLDEN_CLUSTER_REFACTOR_RESPONSES[cluster.row]
        validate_cluster_refactor_response(
            ctx,
            response,
            existing_names=existing_names,
        )
        existing_names = existing_names | {response.helper_name}


def test_validate_google_style_docstring_rejects_missing_args() -> None:
    docstring = """Return the shocked path.

Returns:
    Debt-to-GDP ratio.
"""
    try:
        validate_google_style_docstring(docstring)
    except ValueError as error:
        assert "Args" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_validate_cluster_refactor_response_rejects_docstring_mismatch(
    shock_cluster_context,
    codegen_internals_source,
) -> None:
    response = _shock_active_response(shock_cluster_context)
    mismatched = response.model_copy(
        update={
            "helper_docstring": "Different docstring.\n\nArgs:\n    ctx: x.\n\nReturns:\n    y."
        }
    )
    try:
        validate_cluster_refactor_response(
            shock_cluster_context,
            mismatched,
            existing_names=_function_names(codegen_internals_source),
        )
    except ValueError as error:
        assert "must match" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_align_singleton_response_docstring_recovers_mismatch() -> None:
    response = GOLDEN_SINGLETON_REFACTOR_RESPONSES["Inputs!B6"]
    mismatched = response.model_copy(update={"symbol_docstring": "Stale duplicate."})
    aligned = _align_singleton_response_docstring(mismatched)
    assert aligned.symbol_docstring == response.symbol_docstring


def test_prepare_cluster_refactor_response_aligns_docstring(
    shock_cluster_context,
    codegen_internals_source,
) -> None:
    response = _shock_active_response(shock_cluster_context)
    mismatched = response.model_copy(
        update={
            "helper_docstring": "Different docstring.\n\nArgs:\n    ctx: x.\n\nReturns:\n    y."
        }
    )
    prepared = _prepare_cluster_refactor_response(mismatched, shock_cluster_context)
    assert prepared.helper_docstring.rstrip("\n") == response.helper_docstring.rstrip(
        "\n"
    )
    validate_cluster_refactor_response(
        shock_cluster_context,
        prepared,
        existing_names=_function_names(codegen_internals_source),
    )


def test_prompt_payload_includes_domain_glossary(shock_cluster_context) -> None:
    payload = prompt_payload(shock_cluster_context)
    glossary = cast(list[dict[str, str]], payload["domain_glossary"])
    symbols = {entry["symbol"] for entry in glossary}
    constraints = cast(dict[str, Any], payload["constraints"])
    assert "shock_magnitude_resolved" in symbols
    assert "shock_active" in symbols
    assert "primary_balance_shocked" in symbols
    assert constraints["docstring_style"] == "google"
    assert constraints["require_semantic_locals"] is True


def test_resolve_semantic_dependencies_maps_thin_wrappers() -> None:
    source = (
        "def shock_active(ctx, col):\n"
        "    return 1.0\n\n"
        "def cell_engine_c10(ctx):\n"
        '    return shock_active(ctx, "C")\n\n'
        "def cell_engine_d10(ctx):\n"
        '    return shock_active(ctx, "D")\n'
    )
    semantic, unresolved = resolve_semantic_dependencies(
        source, ["Engine!D10", "Engine!C10"]
    )
    assert unresolved == ()
    assert len(semantic) == 1
    dependency = semantic[0]
    assert isinstance(dependency, SemanticDependency)
    assert dependency.helper_name == "shock_active"
    assert dependency.call_form == "shock_active(ctx, col)"
    assert dependency.address_template == "Engine!{col}10"
    assert dependency.columns == ("C", "D")


def test_resolve_semantic_dependencies_reports_unresolved_cell_helpers() -> None:
    source = "def cell_engine_c10(ctx):\n    return xl_cell(ctx, 'Engine!C10')\n"
    semantic, unresolved = resolve_semantic_dependencies(source, ["Engine!C10"])
    assert semantic == ()
    assert unresolved == ("cell_engine_c10",)


def test_build_cluster_context_row_16_resolves_shock_active(
    codegen_internals_source,
    tiny_dsa_refactor_projection,
    tmp_path,
) -> None:
    from src.formula_clustering import cluster_graph_formulas

    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    by_row = {
        cluster.row: cluster
        for cluster in clusters
        if cluster.row is not None and len(cluster.members) >= 2
    }
    singleton_by_address = {
        cluster.members[0]: cluster for cluster in clusters if len(cluster.members) == 1
    }

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(codegen_internals_source, encoding="utf-8")

    for address in ("Inputs!B6", "Engine!B9"):
        singleton_ctx = build_singleton_refactor_context(
            tiny_dsa_refactor_projection,
            singleton_by_address[address],
            internals_path,
        )
        assert singleton_ctx is not None
        updated, _ = apply_singleton_refactor_plan(
            internals_path.read_text(encoding="utf-8"),
            GOLDEN_SINGLETON_REFACTOR_RESPONSES[address],
            singleton_ctx,
        )
        internals_path.write_text(updated, encoding="utf-8")

    cluster_ctx_10 = build_cluster_refactor_context(
        tiny_dsa_refactor_projection,
        by_row[10],
        internals_path,
    )
    assert cluster_ctx_10 is not None
    updated = apply_refactor_plan(
        internals_path.read_text(encoding="utf-8"),
        GOLDEN_CLUSTER_REFACTOR_RESPONSES[10],
        cluster_ctx_10,
        phase_b=False,
    )
    internals_path.write_text(updated, encoding="utf-8")

    cluster_ctx_16 = build_cluster_refactor_context(
        tiny_dsa_refactor_projection,
        by_row[16],
        internals_path,
    )
    assert cluster_ctx_16 is not None
    resolved_helpers = {dep.helper_name for dep in cluster_ctx_16.semantic_dependencies}
    assert "shock_active" in resolved_helpers
    assert all(
        not name.startswith("cell_") for name in cluster_ctx_16.external_dependencies
    )

    payload = prompt_payload(cluster_ctx_16)
    semantic_payload = cast(list[dict[str, Any]], payload["semantic_dependencies"])
    assert any(entry["helper_name"] == "shock_active" for entry in semantic_payload)


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


def test_apply_phase_b_without_plugins_performs_binding_rewrites_only(
    phase_a_internals_source,
    cluster_refactor_responses,
) -> None:
    updated, rewrite_count = apply_phase_b_final_pass(
        phase_a_internals_source,
        cluster_refactor_responses,
    )
    validate_refactored_internals(updated)
    assert rewrite_count > 0

    assert 'return baseline_debt(ctx, "C")' in _extract_function(
        updated, "cell_outputs_b12"
    )
    debt_to_gdp = _extract_function(updated, "debt_to_gdp")
    assert "xl_eval(ctx, 'Engine!C10', cell_engine_c10)" not in debt_to_gdp


def test_apply_phase_b_rewrites_helper_and_projection_call_sites(
    phase_a_internals_source,
    cluster_refactor_responses,
) -> None:
    updated, rewrite_count = apply_phase_b_final_pass(
        phase_a_internals_source,
        cluster_refactor_responses,
        plugins=TINY_DSA_PHASE_B_PLUGINS,
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
        plugins=TINY_DSA_PHASE_B_PLUGINS,
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
        plugins=TINY_DSA_PHASE_B_PLUGINS,
    )
    updated, pruned = apply_phase_c(phase_b_source)
    validate_refactored_internals(updated)

    assert pruned > 0
    assert "def cell_engine_c16(ctx)" not in updated
    assert "def cell_outputs_b14(ctx)" not in updated
    assert "def cell_outputs_b12(ctx)" not in updated
    assert "def initial_debt_to_gdp(ctx)" in updated
    assert "def shock_magnitude_resolved(ctx)" in updated
    assert "def cell_inputs_b6(ctx)" not in updated
    assert "def cell_engine_b9(ctx)" not in updated
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
        helper_docstring=SHOCK_ACTIVE_DOCSTRING,
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
