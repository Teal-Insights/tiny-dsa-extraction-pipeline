"""Contract tests for cluster refactor prompt shape and response assembly."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent
from typing import TypedDict

import pytest

from src.internals_refactor import (
    ClusterRefactorContext,
    ClusterRefactorLLMResponse,
    ClusterRefactorResponse,
    HelperParameter,
    MemberContext,
    MemberKeyEntry,
    MemberKeys,
    _prompt_for_refactor,
    append_cluster_refactor_note_section,
    apply_cluster_collapse,
    assemble_cluster_symbol_source,
    build_cluster_refactor_context_dump,
    ensure_cluster_refactor_imports,
    format_cluster_refactor_context_dump,
    load_cluster_refactor_prompt_fixed_portion,
    parse_cluster_return_type_hint,
    prepare_cluster_refactor_response,
    strip_python_string_delimiters,
    validate_singleton_return_type_hint,
)
from src.refactor_bindings import KeyConceptSpec


class GrowthThresholdMemberMetadata(TypedDict):
    address: str
    function_name: str
    expected_keys: dict[str, int]
    binding_keys: dict[str, int]
    binding_record: dict[str, str | int | float]


FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cluster_refactor_prompt.md"
)
CONTEXT_DUMP_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cluster_refactor_context_growth_threshold.md"
)
EXPECTED_FIXED_PORTION = FIXTURE_PATH.read_text(encoding="utf-8").strip()
EXPECTED_CONTEXT_DUMP = CONTEXT_DUMP_FIXTURE_PATH.read_text(encoding="utf-8").strip()

GROWTH_THRESHOLD_MEMBER_SOURCES = dedent(
    """
    def cell_forecast_b12(ctx):
        return (
            1.0
            if xl_compare(">=", xl_cell(ctx, "Forecast!B4"), xl_cell(ctx, "Assumptions!C2"))
            else 0.0
        )


    def cell_forecast_c12(ctx):
        return (
            1.0
            if xl_compare(">=", xl_cell(ctx, "Forecast!C4"), xl_cell(ctx, "Assumptions!C2"))
            else 0.0
        )


    def cell_forecast_d12(ctx):
        return (
            1.0
            if xl_compare(">=", xl_cell(ctx, "Forecast!D4"), xl_cell(ctx, "Assumptions!C2"))
            else 0.0
        )


    def cell_forecast_e12(ctx):
        return (
            1.0
            if xl_compare(">=", xl_cell(ctx, "Forecast!E4"), xl_cell(ctx, "Assumptions!C2"))
            else 0.0
        )


    def cell_forecast_f12(ctx):
        return (
            1.0
            if xl_compare(">=", xl_cell(ctx, "Forecast!F4"), xl_cell(ctx, "Assumptions!C2"))
            else 0.0
        )
    """
).strip()

GROWTH_THRESHOLD_KEY_VOCABULARY = (
    KeyConceptSpec(
        concept="REPORTING_PERIOD",
        dtype="int",
        suggested_param_name="reporting_period",
    ),
)

GROWTH_THRESHOLD_MEMBER_METADATA: tuple[GrowthThresholdMemberMetadata, ...] = (
    {
        "address": "Forecast!B12",
        "function_name": "cell_forecast_b12",
        "expected_keys": {"REPORTING_PERIOD": 1},
        "binding_keys": {"REPORTING_PERIOD": 1},
        "binding_record": {
            "INDICATOR": "growth_threshold_met",
            "TABLE": "Quarterly Forecast",
        },
    },
    {
        "address": "Forecast!C12",
        "function_name": "cell_forecast_c12",
        "expected_keys": {"REPORTING_PERIOD": 2},
        "binding_keys": {"REPORTING_PERIOD": 2},
        "binding_record": {
            "INDICATOR": "growth_threshold_met",
            "TABLE": "Quarterly Forecast",
        },
    },
    {
        "address": "Forecast!D12",
        "function_name": "cell_forecast_d12",
        "expected_keys": {"REPORTING_PERIOD": 3},
        "binding_keys": {"REPORTING_PERIOD": 3},
        "binding_record": {
            "INDICATOR": "growth_threshold_met",
            "TABLE": "Quarterly Forecast",
        },
    },
    {
        "address": "Forecast!E12",
        "function_name": "cell_forecast_e12",
        "expected_keys": {"REPORTING_PERIOD": 4},
        "binding_keys": {"REPORTING_PERIOD": 4},
        "binding_record": {
            "INDICATOR": "growth_threshold_met",
            "TABLE": "Quarterly Forecast",
        },
    },
    {
        "address": "Forecast!F12",
        "function_name": "cell_forecast_f12",
        "expected_keys": {"REPORTING_PERIOD": 5},
        "binding_keys": {"REPORTING_PERIOD": 5},
        "binding_record": {
            "INDICATOR": "growth_threshold_met",
            "TABLE": "Quarterly Forecast",
        },
    },
)

GROWTH_THRESHOLD_DEPENDENCY_STUBS = dedent(
    '''
    def xl_cell(ctx: EvalContext, address: str) -> CellValue:
        """Read a single workbook cell by address."""
        # ...

    def xl_compare(op: str, left: CellValue, right: CellValue) -> bool:
        """Compare two scalar cell values using an Excel comparison operator."""
        # ...
    '''
).strip()

GROWTH_THRESHOLD_INTERNALS = (
    "from __future__ import annotations\n\n" + GROWTH_THRESHOLD_MEMBER_SOURCES
)

GROWTH_THRESHOLD_RUNTIME_STUB = dedent(
    '''
    def xl_cell(ctx: EvalContext, address: str) -> CellValue:
        """Read a single workbook cell by address."""
        ...

    def xl_compare(op: str, left: CellValue, right: CellValue) -> bool:
        """Compare two scalar cell values using an Excel comparison operator."""
        ...
    '''
).strip()

MINIMAL_PROMPT_PAYLOAD: dict[str, object] = {
    "cluster_id": 6,
    "canonical_template": "=IF(Forecast!{{col}}4>=Assumptions!$C$2,1,0)",
    "key_vocabulary": [
        {
            "concept": "REPORTING_PERIOD",
            "dtype": "int",
            "suggested_param_name": "reporting_period",
        }
    ],
}

GROWTH_THRESHOLD_CLUSTER_CONTEXT = ClusterRefactorContext(
    cluster_id=6,
    canonical_template="=IF(Forecast!{col}4>=Assumptions!$C$2,1,0)",
    row=12,
    members=tuple(
        MemberContext(
            address=entry["address"],
            function_name=entry["function_name"],
            engine_column="B",
            normalized_formula="=IF(Forecast!B4>=Assumptions!$C$2,1,0)",
            python_source=f"def {entry['function_name']}(ctx):\n    return 1.0\n",
            dependency_addresses=(),
            dependency_functions=(),
        )
        for entry in GROWTH_THRESHOLD_MEMBER_METADATA
    ),
    external_dependencies=(),
    semantic_dependencies=(),
    call_sites=(),
    first_year_column="B",
    allowed_runtime_symbols=("xl_cell", "xl_compare"),
    key_vocabulary=GROWTH_THRESHOLD_KEY_VOCABULARY,
    expected_member_keys={
        entry["address"]: dict(entry["expected_keys"])
        for entry in GROWTH_THRESHOLD_MEMBER_METADATA
    },
    naming_hints={},
)

GROWTH_THRESHOLD_LLM_RESPONSE = ClusterRefactorLLMResponse(
    symbol_signature=(
        "def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:"
    ),
    symbol_docstring=(
        "Return 1.0 when the observed value meets or exceeds the growth threshold "
        "for the reporting period.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n"
        "    reporting_period: Reporting period index (1 through 5).\n\n"
        "Returns:\n    1.0 if the observed value is at or above the threshold, else 0.0."
    ),
    symbol_body=(
        "column_by_period = {1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F'}\n"
        "column = column_by_period[reporting_period]\n"
        "observed_value = xl_cell(ctx, f'Forecast!{column}4')\n"
        "threshold = xl_cell(ctx, 'Assumptions!C2')\n"
        "meets_threshold = xl_compare('>=', observed_value, threshold)\n"
        "return 1.0 if meets_threshold else 0.0"
    ),
    parameters=(
        HelperParameter(
            name="reporting_period",
            concept="REPORTING_PERIOD",
            dtype="int",
        ),
    ),
    member_keys=tuple(
        MemberKeys(
            address=entry["address"],
            function_name=entry["function_name"],
            keys=(
                MemberKeyEntry(
                    concept="REPORTING_PERIOD",
                    value=entry["expected_keys"]["REPORTING_PERIOD"],
                ),
            ),
        )
        for entry in GROWTH_THRESHOLD_MEMBER_METADATA
    ),
    uses_first_year_branch=False,
)

INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT = dedent(
    """
    from __future__ import annotations

    from .runtime import xl_cell

    # --- Formula cell functions ---

    def cell_forecast_b12(ctx):
        return xl_cell(ctx, "Forecast!B4")

    def cell_forecast_c12(ctx):
        return xl_cell(ctx, "Forecast!C4")
    """
).strip()

INTERNALS_WITH_PAREN_RUNTIME_IMPORT = dedent(
    """
    from __future__ import annotations

    from .runtime import (
        xl_cell,
        xl_compare,
    )

    def cell_forecast_b12(ctx):
        return xl_cell(ctx, "Forecast!B4")
    """
).strip()

RESOLVER_SECTION = """# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {}
_SYMBOL_DISPATCH = {}

def _address_to_func_name(address):
    return "cell_placeholder"

def _resolve_formula(address):
    return globals().get(_address_to_func_name(address))
"""

INTERNALS_WITH_CLUSTER_CALLERS = (
    dedent(
        """
        from __future__ import annotations

        from .runtime import xl_cell, xl_compare

        def cell_forecast_b12(ctx):
            return 1.0

        def cell_forecast_c12(ctx):
            return xl_cell(ctx, "Forecast!C4")
        """
    ).strip()
    + "\n\n"
    + RESOLVER_SECTION
)


def test_load_cluster_refactor_prompt_fixed_portion_matches_fixture() -> None:
    assert (
        load_cluster_refactor_prompt_fixed_portion().strip() == EXPECTED_FIXED_PORTION
    )


def test_prompt_for_cluster_refactor_starts_with_fixed_portion() -> None:
    prompt = _prompt_for_refactor(
        MINIMAL_PROMPT_PAYLOAD,
        ClusterRefactorLLMResponse.model_json_schema(),
    )
    context_marker = "\n\nCluster context:"
    assert context_marker in prompt
    fixed_portion, _context = prompt.split(context_marker, maxsplit=1)
    assert fixed_portion.strip() == EXPECTED_FIXED_PORTION


def test_prompt_for_cluster_refactor_does_not_require_llm_note_section() -> None:
    prompt = _prompt_for_refactor(
        MINIMAL_PROMPT_PAYLOAD,
        ClusterRefactorLLMResponse.model_json_schema(),
    )
    assert "Include a Note section" not in prompt
    assert '"symbol_signature"' in prompt
    assert '"symbol_body"' in prompt
    assert '"helper_source"' not in prompt.split("Cluster context:", maxsplit=1)[0]


def test_append_cluster_refactor_note_section_covers_address_range() -> None:
    docstring = (
        "Return 1.0 when the observed value meets or exceeds the growth threshold.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n"
        "    reporting_period: Reporting period index.\n\n"
        "Returns:\n    1.0 if the observed value is at or above the threshold, else 0.0."
    )
    updated = append_cluster_refactor_note_section(
        docstring,
        covered_addresses="Forecast!B12:F12",
        formula="=IF(Forecast!{col}4>=Assumptions!$C$2,1,0)",
    )
    assert updated.endswith(
        "Note:\n    Covers Forecast!B12:F12. "
        "Excel: =IF(Forecast!{col}4>=Assumptions!$C$2,1,0)."
    )
    assert "Note:" not in docstring


def test_assemble_cluster_symbol_source_wraps_docstring_and_body() -> None:
    assembled = assemble_cluster_symbol_source(
        signature="def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:",
        docstring=(
            "Return 1.0 when the observed value meets or exceeds the growth threshold.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n"
            "    reporting_period: Reporting period index.\n\n"
            "Returns:\n    1.0 if the observed value is at or above the threshold, else 0.0.\n\n"
            "Note:\n    Covers Forecast!B12:F12. Excel: =1."
        ),
        body="return 1.0",
    )
    assert assembled.startswith(
        "def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:\n"
    )
    assert "    return 1.0\n" in assembled
    assert (
        '    """Return 1.0 when the observed value meets or exceeds the growth threshold.'
        in (assembled)
    )


def test_prepare_cluster_refactor_response_assembles_and_appends_note() -> None:
    prepared = prepare_cluster_refactor_response(
        GROWTH_THRESHOLD_LLM_RESPONSE,
        GROWTH_THRESHOLD_CLUSTER_CONTEXT,
    )

    assert isinstance(prepared, ClusterRefactorResponse)
    assert prepared.helper_name == "growth_threshold_met"
    assert (
        "Note:\n    Covers Forecast!B12:F12. "
        "Excel: =IF(Forecast!{col}4>=Assumptions!$C$2,1,0)."
    ) in prepared.helper_docstring
    assert prepared.helper_docstring in prepared.helper_source
    assert prepared.parameters == GROWTH_THRESHOLD_LLM_RESPONSE.parameters
    assert prepared.member_keys == GROWTH_THRESHOLD_LLM_RESPONSE.member_keys
    assert prepared.uses_first_year_branch is False


def test_format_cluster_refactor_context_dump_matches_fixture() -> None:
    dump = format_cluster_refactor_context_dump(
        member_sources=GROWTH_THRESHOLD_MEMBER_SOURCES,
        key_vocabulary=GROWTH_THRESHOLD_KEY_VOCABULARY,
        member_metadata=GROWTH_THRESHOLD_MEMBER_METADATA,
        dependency_stubs=GROWTH_THRESHOLD_DEPENDENCY_STUBS,
    )
    assert dump.strip() == EXPECTED_CONTEXT_DUMP


def test_build_cluster_refactor_context_dump_from_sources_matches_fixture() -> None:
    dump = build_cluster_refactor_context_dump(
        member_function_names=tuple(
            str(entry["function_name"]) for entry in GROWTH_THRESHOLD_MEMBER_METADATA
        ),
        internals_source=GROWTH_THRESHOLD_INTERNALS,
        runtime_source=GROWTH_THRESHOLD_RUNTIME_STUB,
        key_vocabulary=GROWTH_THRESHOLD_KEY_VOCABULARY,
        member_metadata=GROWTH_THRESHOLD_MEMBER_METADATA,
    )
    assert dump.strip() == EXPECTED_CONTEXT_DUMP


def test_build_cluster_refactor_context_dump_excludes_legacy_prompt_fields() -> None:
    dump = build_cluster_refactor_context_dump(
        member_function_names=tuple(
            str(entry["function_name"]) for entry in GROWTH_THRESHOLD_MEMBER_METADATA
        ),
        internals_source=GROWTH_THRESHOLD_INTERNALS,
        runtime_source=GROWTH_THRESHOLD_RUNTIME_STUB,
        key_vocabulary=GROWTH_THRESHOLD_KEY_VOCABULARY,
        member_metadata=GROWTH_THRESHOLD_MEMBER_METADATA,
    )
    assert "call_sites" not in dump
    assert "semantic_dependencies" not in dump
    assert "dependency_addresses" not in dump
    assert "naming_hints" not in dump
    assert "engine_column" not in dump
    assert "normalized_formula" not in dump


def test_prompt_for_cluster_refactor_appends_context_dump() -> None:
    context_dump = build_cluster_refactor_context_dump(
        member_function_names=tuple(
            str(entry["function_name"]) for entry in GROWTH_THRESHOLD_MEMBER_METADATA
        ),
        internals_source=GROWTH_THRESHOLD_INTERNALS,
        runtime_source=GROWTH_THRESHOLD_RUNTIME_STUB,
        key_vocabulary=GROWTH_THRESHOLD_KEY_VOCABULARY,
        member_metadata=GROWTH_THRESHOLD_MEMBER_METADATA,
    )
    prompt = _prompt_for_refactor(context_dump)
    assert prompt.startswith(EXPECTED_FIXED_PORTION)
    assert prompt.endswith(context_dump.strip())
    assert "Cluster to refactor:" in prompt
    assert "Key vocabulary:" in prompt
    assert "Member metadata:" in prompt
    assert "Dependencies:" in prompt


def test_parse_cluster_return_type_hint_from_signature() -> None:
    assert (
        parse_cluster_return_type_hint(
            "def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:"
        )
        == "float"
    )


def test_validate_cluster_return_type_hint_rejects_xlerror_sentinel() -> None:
    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_singleton_return_type_hint("XlError")


def test_ensure_cluster_refactor_imports_handles_parenthesized_runtime_import() -> None:
    response = prepare_cluster_refactor_response(
        GROWTH_THRESHOLD_LLM_RESPONSE,
        GROWTH_THRESHOLD_CLUSTER_CONTEXT,
    )
    updated = ensure_cluster_refactor_imports(
        INTERNALS_WITH_PAREN_RUNTIME_IMPORT,
        response,
    )
    ast.parse(updated)
    assert "EvalContext" in updated.split("from .runtime import", maxsplit=1)[1]


def test_ensure_cluster_refactor_imports_injects_eval_context() -> None:
    response = prepare_cluster_refactor_response(
        GROWTH_THRESHOLD_LLM_RESPONSE,
        GROWTH_THRESHOLD_CLUSTER_CONTEXT,
    )
    updated = ensure_cluster_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        response,
    )
    assert "EvalContext" in updated.split("from .runtime import", maxsplit=1)[1]

    applied, _rewrite_count = apply_cluster_collapse(
        updated,
        response,
        GROWTH_THRESHOLD_CLUSTER_CONTEXT,
    )
    assert "EvalContext" in applied.split("from .runtime import", maxsplit=1)[1]
    assert (
        "def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:"
        in (applied)
    )


def test_strip_python_string_delimiters_shared_with_singleton() -> None:
    raw = '''"""
Summary line.

Args:
    ctx: Workbook evaluation context.

Returns:
    A value.
"""'''
    assert strip_python_string_delimiters(raw) == (
        "Summary line.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    A value."
    )
