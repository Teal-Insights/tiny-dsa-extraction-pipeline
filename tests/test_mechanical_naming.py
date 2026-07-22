"""Unit tests for the naming-only cluster contract (issue #45)."""

from __future__ import annotations

import ast
from textwrap import dedent

import pytest

from src.mechanical_body import MechanicalBodyDraft
from src.mechanical_naming import (
    ClusterNamingLLMResponse,
    LocalRename,
    NamingUnit,
    apply_cluster_naming_response,
    apply_naming_responses_to_module,
)


def _draft() -> MechanicalBodyDraft:
    return MechanicalBodyDraft(
        body=(
            "column_by_time_period = {1: 'C', 2: 'D'}\n"
            "_t1 = xl_cell(ctx, f'Engine!{column_by_time_period[time_period]}5')\n"
            "_t2 = read_shock_year(ctx)\n"
            "return 1.0 if xl_bool(xl_compare('>=', _t1, _t2)) else 0.0"
        ),
        renameable_locals=("_t1", "_t2"),
        lookup_table_names=("column_by_time_period",),
        group_count=1,
    )


def _response(renames: tuple[LocalRename, ...]) -> ClusterNamingLLMResponse:
    return ClusterNamingLLMResponse(
        symbol_docstring="Doc.\n\nArgs:\n    ctx: Context.\n\nReturns:\n    Flag.",
        renames=renames,
        error=None,
        error_reason=None,
    )


def test_apply_renames_every_mechanical_local() -> None:
    response = _response(
        (
            LocalRename(original="_t1", replacement="engine_value"),
            LocalRename(original="_t2", replacement="shock_year"),
        )
    )
    body = apply_cluster_naming_response(
        response,
        _draft(),
        parameter_names=frozenset({"time_period"}),
        forbidden_names=frozenset(
            {"xl_cell", "xl_bool", "xl_compare", "read_shock_year"}
        ),
    )
    assert (
        "engine_value = xl_cell(ctx, f'Engine!{column_by_time_period[time_period]}5')"
        in body
    )
    assert "shock_year = read_shock_year(ctx)" in body
    assert "_t1" not in body
    assert "_t2" not in body


def test_missing_rename_is_rejected() -> None:
    response = _response((LocalRename(original="_t1", replacement="engine_value"),))
    with pytest.raises(ValueError, match="missing: _t2"):
        apply_cluster_naming_response(
            response,
            _draft(),
            parameter_names=frozenset({"time_period"}),
            forbidden_names=frozenset(),
        )


def test_collision_with_parameter_is_rejected() -> None:
    response = _response(
        (
            LocalRename(original="_t1", replacement="time_period"),
            LocalRename(original="_t2", replacement="shock_year"),
        )
    )
    with pytest.raises(ValueError):
        apply_cluster_naming_response(
            response,
            _draft(),
            parameter_names=frozenset({"time_period"}),
            forbidden_names=frozenset(),
        )


def test_unknown_original_is_rejected() -> None:
    response = _response(
        (
            LocalRename(original="_t1", replacement="engine_value"),
            LocalRename(original="_t2", replacement="shock_year"),
            LocalRename(original="_t9", replacement="mystery"),
        )
    )
    with pytest.raises(ValueError, match="_t9"):
        apply_cluster_naming_response(
            response,
            _draft(),
            parameter_names=frozenset({"time_period"}),
            forbidden_names=frozenset(),
        )


def test_error_response_requires_null_success_fields() -> None:
    with pytest.raises(ValueError):
        ClusterNamingLLMResponse(
            symbol_docstring="Doc.",
            renames=(),
            error=True,
            error_reason="cannot document",
        )
    response = ClusterNamingLLMResponse(
        symbol_docstring=None,
        renames=None,
        error=True,
        error_reason="cannot document",
    )
    assert response.error is True


def _xl_memoize_decorator_count(source: str, function_name: str) -> int:
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return sum(
                1
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Name) and decorator.id == "xl_memoize"
            )
    raise AssertionError(f"function {function_name!r} not found")


def test_naming_applier_preserves_single_xl_memoize_decorator() -> None:
    """Pass-2 rewrite must not stack a second @xl_memoize on mechanical helpers.

    ``FunctionDef.lineno`` points at the ``def`` line, so a slice that starts
    there leaves the existing decorator in place while ``ast.unparse`` emits
    another, matching the duplicates on synthesized helpers in internals.py.
    """
    module = dedent(
        '''
        from __future__ import annotations

        @xl_memoize
        def helper_a(ctx: EvalContext) -> float:
            """Mechanically synthesized helper.

            Args:
                ctx: Workbook evaluation context.

            Returns:
                Cell value.
            """
            _t1 = xl_number(ctx)
            return _t1
        '''
    ).strip()
    assert _xl_memoize_decorator_count(module, "helper_a") == 1

    named = apply_naming_responses_to_module(
        module,
        (
            NamingUnit(
                helper_name="helper_a",
                draft=MechanicalBodyDraft(
                    body="_t1 = xl_number(ctx)\nreturn _t1",
                    renameable_locals=("_t1",),
                    lookup_table_names=(),
                    group_count=1,
                ),
                response=_response(
                    (LocalRename(original="_t1", replacement="numeric_value"),)
                ),
                parameter_names=frozenset(),
                forbidden_names=frozenset({"xl_number", "helper_a"}),
            ),
        ),
    )

    assert "numeric_value = xl_number(ctx)" in named
    assert _xl_memoize_decorator_count(named, "helper_a") == 1
