"""Tests for mechanical refactor helper return-type inference."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from src.refactor_return_types import (
    build_callee_return_hints,
    infer_refactor_return_type_hint,
    merge_callee_return_hints,
    merge_callee_return_hints_from_functions,
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


def test_read_runtime_source_includes_readers_annotations_in_callee_hints(
    tmp_path: Path,
) -> None:
    """Pass 1 loads runtime+_readers via _read_runtime_source for callee hints."""
    from src.internals_refactor import _read_runtime_source

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "runtime.py").write_text(RUNTIME_STUB + "\n", encoding="utf-8")
    (package / "_readers.py").write_text(
        dedent(
            """
            def read_demography_scenario(ctx: EvalContext) -> CellValue:
                return xl_cell(ctx, "Dashboard!C17")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (package / "internals.py").write_text("# placeholder\n", encoding="utf-8")

    runtime_source = _read_runtime_source(package / "internals.py")
    hints = build_callee_return_hints(
        runtime_source=runtime_source,
        internals_source="",
    )
    assert hints["read_demography_scenario"] == "CellValue"
    assert hints["xl_cell"] == "CellValue"


def test_merge_callee_return_hints_updates_from_helper_source() -> None:
    hints = build_callee_return_hints(
        runtime_source=RUNTIME_STUB,
        internals_source="",
    )
    merge_callee_return_hints(
        hints,
        source=dedent(
            """
            def demography_total_population_medium(ctx, time_period: int) -> float:
                return 1.0
            """
        ).strip(),
    )
    assert hints["demography_total_population_medium"] == "float"
    assert hints["xl_cell"] == "CellValue"


def test_infer_refactor_return_type_hint_after_upstream_helper_merge() -> None:
    """IF over sibling series helpers plus a string else merges to float | str."""
    hints = build_callee_return_hints(
        runtime_source=RUNTIME_STUB,
        internals_source="",
    )
    for name in (
        "demography_total_population_medium",
        "demography_total_population_high",
        "demography_total_population_low",
    ):
        merge_callee_return_hints(
            hints,
            source=f"def {name}(ctx, time_period: int) -> float:\n    return 1.0\n",
        )
    source = dedent(
        """
        def cell_demography_bi4(ctx):
            return (
                demography_total_population_medium(ctx, time_period=2008)
                if xl_compare("=", xl_cell(ctx, "Demography!B8"), xl_cell(ctx, "Demography!B8"))
                else (
                    demography_total_population_high(ctx, time_period=2008)
                    if xl_compare("=", xl_cell(ctx, "Demography!B9"), xl_cell(ctx, "Demography!B9"))
                    else (
                        demography_total_population_low(ctx, time_period=2008)
                        if xl_compare("=", xl_cell(ctx, "Demography!B10"), xl_cell(ctx, "Demography!B10"))
                        else '"'
                    )
                )
            )
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
            callee_hints=hints,
        )
        == "float | str"
    )


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


def test_infer_refactor_return_type_hint_propagates_union_callee_annotation() -> None:
    """Passthrough of an upstream helper with a union return.

    ``merge_callee_return_hints`` stores allowlisted unions as a single
    ``\"float | CellValue\"`` string. Inference must split that back into
    atomic parts; wrapping the whole string as one set member makes
    ``format_return_type_hint`` emit ``''`` and fail validation.
    """
    hints = build_callee_return_hints(
        runtime_source=RUNTIME_STUB,
        internals_source="",
    )
    merge_callee_return_hints(
        hints,
        source=dedent(
            """
            def baseline_engine_indicators(
                ctx, indicator: str, time_period: int
            ) -> float | CellValue:
                return 0.0
            """
        ).strip(),
    )
    assert hints["baseline_engine_indicators"] == "float | CellValue"
    source = dedent(
        """
        def cell_hot_adapted_aa43(ctx):
            return baseline_engine_indicators(
                ctx, indicator='interest_expenditure_pct_gdp', time_period=2032
            )
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
            callee_hints=hints,
        )
        == "float | CellValue"
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


def test_infer_refactor_return_type_hint_treats_none_literal_as_cellvalue() -> None:
    source = dedent(
        """
        def cell_baseline_x42(ctx):
            return (
                (0.0)
                if (_t2 := xl_compare("=", xl_cell(ctx, "Dashboard!C33"), "No"))
                else (
                    (xl_cell(ctx, "Baseline!X47"))
                    if (_t1 := xl_compare("=", xl_cell(ctx, "Baseline!X46"), xl_cell(ctx, "Baseline!B47")))
                    else (None)
                )
            )
        """
    ).strip()
    assert (
        infer_refactor_return_type_hint(
            python_sources=(source,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
        )
        == "float | CellValue"
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


def test_infer_refactor_return_type_hint_falls_back_to_cellvalue_for_unknown_callee() -> (
    None
):
    """A bare passthrough to a helper with no known return hint yields CellValue.

    Reproduces the cluster-75 hard crash: a verified mechanical draft whose
    member body calls a helper created earlier in the same pass (absent from the
    stale callee-hint map) must not abort the pipeline. The safe opaque supertype
    is used instead of raising.
    """
    source = dedent(
        """
        def cell_output_scenarios_d14(ctx):
            return output_scenarios_debt_to_gdp_baseline_path(ctx, time_period=2050)
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


def test_merge_callee_return_hints_from_functions_enables_intra_pass_callee() -> None:
    """Refreshing hints from resealed defs lets a bare call to a pass-built helper type."""
    member = dedent(
        """
        def cell_output_scenarios_d14(ctx):
            return output_scenarios_debt_to_gdp_baseline_path(ctx, time_period=2050)
        """
    ).strip()
    # Before the refresh the callee is unknown -> opaque fallback.
    hints = build_callee_return_hints(runtime_source=RUNTIME_STUB, internals_source="")
    assert (
        infer_refactor_return_type_hint(
            python_sources=(member,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
            callee_hints=hints,
        )
        == "CellValue"
    )
    # A later reseal exposes the helper's annotation (parsed defs, no re-parse).
    module = ast.parse(
        "def output_scenarios_debt_to_gdp_baseline_path(ctx, time_period: int)"
        " -> float:\n    return 1.0\n"
    )
    functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }
    merge_callee_return_hints_from_functions(hints, functions)
    assert (
        infer_refactor_return_type_hint(
            python_sources=(member,),
            runtime_source=RUNTIME_STUB,
            internals_source="",
            callee_hints=hints,
        )
        == "float"
    )
