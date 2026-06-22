from __future__ import annotations

from pathlib import Path

import pytest

from src.extraction_pipeline import graph
from src.formula_clustering import cluster_graph_formulas
from src.internals_refactor import (
    ClusterRefactorResponse,
    MemberBinding,
    apply_refactor_plan,
    build_cluster_refactor_context,
    deterministic_helper_name,
    refactor_cache_key,
    validate_cluster_refactor_response,
    validate_refactored_internals,
    wrapper_source,
)
from src.subgraph_projection import build_tiny_dsa_refactor_projection

repo_root = Path(__file__).resolve().parents[1]
INTERNALS_PATH = repo_root / "dist/tiny_dsa/internals.py"

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


@pytest.fixture
def shock_cluster_context():
    projection = build_tiny_dsa_refactor_projection(graph)
    cluster = next(
        cluster
        for cluster in cluster_graph_formulas(projection)
        if cluster.row == 10 and len(cluster.members) == 5
    )
    ctx = build_cluster_refactor_context(projection, cluster, INTERNALS_PATH)
    assert ctx is not None
    return ctx


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
    assert len(ctx.call_sites) >= 5
    xl_eval_sites = [site for site in ctx.call_sites if site.pattern == "xl_eval"]
    assert any(site.callee_address == "Engine!C10" for site in xl_eval_sites)


def test_wrapper_source() -> None:
    assert wrapper_source("shock_active", "C") == 'return shock_active(ctx, "C")'


def test_deterministic_helper_name_row_10(shock_cluster_context) -> None:
    assert deterministic_helper_name(shock_cluster_context) == "shock_active"


def test_validate_cluster_refactor_response_row_10(shock_cluster_context) -> None:
    response = _shock_active_response(shock_cluster_context)
    validate_cluster_refactor_response(
        shock_cluster_context,
        response,
        existing_names=_function_names(INTERNALS_PATH.read_text(encoding="utf-8")),
    )


def test_apply_phase_a_row_10(shock_cluster_context) -> None:
    source = INTERNALS_PATH.read_text(encoding="utf-8")
    response = _shock_active_response(shock_cluster_context)
    validate_cluster_refactor_response(
        shock_cluster_context,
        response,
        existing_names=_function_names(source),
    )

    result = apply_refactor_plan(source, response, shock_cluster_context)
    validate_refactored_internals(result)

    assert "def shock_active(ctx, col)" in result
    for member in shock_cluster_context.members:
        assert f'return shock_active(ctx, "{member.engine_column}")' in result
        assert member.function_name in result

    for member in shock_cluster_context.members:
        function_block = _extract_function(result, member.function_name)
        assert function_block.count("return ") == 1
        assert "xl_ge" not in function_block


def test_refactor_cache_key_is_stable(shock_cluster_context) -> None:
    internals_bytes = INTERNALS_PATH.read_bytes()
    schema = ClusterRefactorResponse.model_json_schema()
    key_a = refactor_cache_key(shock_cluster_context, internals_bytes, schema)
    key_b = refactor_cache_key(shock_cluster_context, internals_bytes, schema)
    assert key_a == key_b
    assert len(key_a) == 64


def test_refactor_cache_key_changes_when_member_source_changes(
    shock_cluster_context,
) -> None:
    schema = ClusterRefactorResponse.model_json_schema()
    internals_bytes = INTERNALS_PATH.read_bytes()
    baseline = refactor_cache_key(shock_cluster_context, internals_bytes, schema)

    mutated_member = shock_cluster_context.members[0]
    replacement = shock_cluster_context.members[1]
    mutated_members = tuple(
        replacement if member.address == mutated_member.address else member
        for member in shock_cluster_context.members
    )
    from dataclasses import replace

    mutated_ctx = replace(shock_cluster_context, members=mutated_members)
    assert refactor_cache_key(mutated_ctx, internals_bytes, schema) != baseline


def _function_names(source: str) -> frozenset[str]:
    import ast

    module = ast.parse(source)
    return frozenset(
        node.name for node in module.body if isinstance(node, ast.FunctionDef)
    )


def _extract_function(source: str, function_name: str) -> str:
    import ast

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
