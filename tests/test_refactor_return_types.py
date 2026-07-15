"""Tests for mechanical refactor helper return-type inference."""

from __future__ import annotations

from textwrap import dedent

import pytest

from src.refactor_return_types import (
    build_callee_return_hints,
    infer_refactor_return_type_hint,
    narrow_return_type_hint_for_export,
    normalize_return_type_hint_for_allowlist,
    validate_scalar_return_type_hint,
)

RUNTIME_STUB = dedent(
    """
    def xl_cell(ctx, address: str) -> CellValue:
        ...

    def xl_compare(op: str, left, right) -> bool:
        ...

    def xl_number(value) -> float:
        ...
    """
).strip()

INTERNALS_WITH_ANNOTATED_HELPER = dedent(
    """
    def shock_active(ctx) -> bool:
        return True

    def cell_engine_c10(ctx):
        return shock_active(ctx)
    """
).strip()


def test_build_callee_return_hints_uses_allowlisted_runtime_annotations() -> None:
    hints = build_callee_return_hints(
        runtime_source=RUNTIME_STUB,
        internals_source=INTERNALS_WITH_ANNOTATED_HELPER,
    )
    assert hints["xl_compare"] == "bool"
    assert hints["xl_number"] == "float"
    assert hints["xl_cell"] == "CellValue"
    assert hints["shock_active"] == "bool"


def test_infer_refactor_return_type_hint_from_annotated_callee() -> None:
    source = dedent(
        """
        def cell_engine_c10(ctx):
            return shock_active(ctx)
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source=INTERNALS_WITH_ANNOTATED_HELPER,
        )
        == "bool"
    )


def test_infer_refactor_return_type_hint_from_xl_number_call() -> None:
    source = dedent(
        """
        def cell_some_sheet_z22(ctx):
            return xl_number(united_states_total_deaths(ctx))
        """
    ).strip()
    internals = dedent(
        """
        def united_states_total_deaths(ctx):
            return 100.0
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source=internals,
        )
        == "float"
    )


def test_infer_refactor_return_type_hint_from_if_expression_literals() -> None:
    source = dedent(
        """
        def cell_forecast_b12(ctx):
            return (
                1.0
                if xl_compare(">=", xl_cell(ctx, "Forecast!B4"), xl_cell(ctx, "Assumptions!C2"))
                else 0.0
            )
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "float"
    )


def test_infer_refactor_return_type_hint_from_direct_xl_compare_call() -> None:
    source = dedent(
        """
        def cell_x(ctx):
            return xl_compare(">=", xl_cell(ctx, "A!B1"), xl_cell(ctx, "A!B2"))
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "bool"
    )


def test_infer_refactor_return_type_hint_propagates_cellvalue_for_opaque_passthrough() -> (
    None
):
    source = dedent(
        """
        def cell_engine_c6(ctx):
            return xl_cell(ctx, "Inputs!C1")
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "CellValue"
    )


def test_infer_refactor_return_type_hint_narrows_cellvalue_with_binding_dtype() -> None:
    source = dedent(
        """
        def cell_x(ctx):
            return xl_cell(ctx, "Inputs!C1")
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
            naming_hints={"measure_dtype": "float"},
        )
        == "float"
    )


def test_narrow_return_type_hint_for_export_leaves_mixed_unions_unchanged() -> None:
    assert (
        narrow_return_type_hint_for_export(
            "bool | CellValue",
            {"measure_dtype": "float"},
        )
        == "bool | CellValue"
    )


def test_infer_refactor_return_type_hint_unions_across_cluster_members() -> None:
    sources = (
        dedent(
            """
            def cell_a(ctx):
                return True
            """
        ).strip(),
        dedent(
            """
            def cell_b(ctx):
                return False
            """
        ).strip(),
    )
    assert (
        infer_refactor_return_type_hint(
            python_sources=sources,
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "bool"
    )


def test_infer_refactor_return_type_hint_unions_cellvalue_with_scalar_across_members() -> (
    None
):
    sources = (
        dedent(
            """
            def cell_a(ctx):
                return xl_cell(ctx, "Inputs!A1")
            """
        ).strip(),
        dedent(
            """
            def cell_b(ctx):
                return True
            """
        ).strip(),
    )
    assert (
        infer_refactor_return_type_hint(
            python_sources=sources,
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "bool | CellValue"
    )


def test_normalize_return_type_hint_strips_forbidden_parts() -> None:
    assert normalize_return_type_hint_for_allowlist("float | str | bool | None") == (
        "bool | float | str"
    )
    assert normalize_return_type_hint_for_allowlist("CellValue") == "CellValue"
    assert normalize_return_type_hint_for_allowlist("float | XlError") == "float"
    assert (
        normalize_return_type_hint_for_allowlist("bool | CellValue | None")
        == "bool | CellValue"
    )


def test_validate_scalar_return_type_hint_accepts_cellvalue() -> None:
    validate_scalar_return_type_hint("CellValue")
    validate_scalar_return_type_hint("bool | CellValue")


def test_validate_scalar_return_type_hint_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_scalar_return_type_hint("dict[str, float]")
