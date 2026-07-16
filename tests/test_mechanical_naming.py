"""Unit tests for the naming-only cluster contract (issue #45)."""

from __future__ import annotations

import pytest

from src.mechanical_body import MechanicalBodyDraft
from src.mechanical_naming import (
    ClusterNamingLLMResponse,
    LocalRename,
    apply_cluster_naming_response,
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
