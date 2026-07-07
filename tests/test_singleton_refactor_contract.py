"""Contract tests for singleton refactor prompt shape and response assembly."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from src.internals_refactor import (
    SingletonRefactorContext,
    SingletonRefactorLLMResponse,
    SingletonRefactorResponse,
    _prompt_for_singleton_refactor,
    append_refactor_note_section,
    apply_singleton_refactor_plan,
    assemble_singleton_symbol_source,
    build_singleton_refactor_context_dump,
    ensure_singleton_refactor_imports,
    format_singleton_refactor_context_dump,
    load_singleton_refactor_prompt_fixed_portion,
    parse_singleton_return_type_hint,
    prepare_singleton_refactor_response,
    strip_python_string_delimiters,
    validate_singleton_return_type_hint,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "singleton_refactor_prompt.md"
)
CONTEXT_DUMP_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "singleton_refactor_context_excess_deaths.md"
)
EXPECTED_FIXED_PORTION = FIXTURE_PATH.read_text(encoding="utf-8").strip()
EXPECTED_CONTEXT_DUMP = CONTEXT_DUMP_FIXTURE_PATH.read_text(encoding="utf-8").strip()

ALLOWED_RUNTIME_SYMBOLS = (
    "XlError",
    "xl_cell",
    "xl_eval",
    "xl_number",
)

ALLOWED_SINGLETON_RETURN_TYPE_HINTS = frozenset(
    {
        "bool",
        "float",
        "int",
        "str",
    }
)

EXCESS_DEATHS_FUNCTION_SOURCE = dedent(
    """
    def cell_some_sheet_z22(ctx):
        return (
            xl_number(united_states_total_deaths(ctx))
            - xl_number(united_states_expected_deaths(ctx))
        )
    """
).strip()

EXCESS_DEATHS_CELL_METADATA: dict[str, object] = {
    "address": "SomeSheet!Z22",
    "table_labels": [
        {
            "label": "United States Vital Statistics",
            "concept": "UNITED_STATES_VITAL_STATISTICS",
        }
    ],
    "row_labels": [
        {
            "label": "Excess Deaths",
            "concept": "EXCESS_DEATHS",
        }
    ],
    "column_labels": [],
}

EXCESS_DEATHS_DEPENDENCY_STUBS = dedent(
    '''
    def xl_number(value: CellValue) -> float:
        """Coerce a scalar cell value to a number, raising on Excel errors."""
        # ...

    def united_states_expected_deaths(ctx: EvalContext) -> float:
        """
        Expected deaths for the United States.

        Args:
            ctx: Workbook evaluation context.

        Returns:
            Expected deaths for the United States: baseline number of deaths that
            would have occurred in the absence of the shock.
        """
        # ...

    def united_states_total_deaths(ctx: EvalContext) -> float:
        """
        Total observed deaths in the United States.

        Args:
            ctx: Workbook evaluation context.

        Returns:
            United States total deaths.
        """
        # ...
    '''
).strip()

EXCESS_DEATHS_INTERNALS = dedent(
    '''
    from __future__ import annotations

    from .runtime import XlError, xl_number

    def united_states_total_deaths(ctx):
        """
        Total observed deaths in the United States.

        Args:
            ctx: Workbook evaluation context.

        Returns:
            United States total deaths.

        Note:
            Covers SomeSheet!Z20. Excel: =1.
        """
        return 100.0

    def united_states_expected_deaths(ctx):
        """
        Expected deaths for the United States.

        Args:
            ctx: Workbook evaluation context.

        Returns:
            Expected deaths for the United States: baseline number of deaths that
            would have occurred in the absence of the shock.

        Note:
            Covers SomeSheet!Z6. Excel: =1.
        """
        return 90.0

    def cell_some_sheet_z22(ctx):
        return (
            xl_number(united_states_total_deaths(ctx))
            - xl_number(united_states_expected_deaths(ctx))
        )
    '''
).strip()

EXCESS_DEATHS_RUNTIME_STUB = dedent(
    '''
    def xl_number(value: CellValue) -> float:
        """Coerce a scalar cell value to a number, raising on Excel errors."""
        ...
    '''
).strip()

INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT = dedent(
    """
    from __future__ import annotations

    from .runtime import xl_number

    def cell_some_sheet_z22(ctx):
        return xl_number(united_states_total_deaths(ctx))

    def united_states_total_deaths(ctx):
        return 1.0
    """
).strip()

EVAL_CONTEXT_REFACTOR_RESPONSE = SingletonRefactorResponse(
    symbol_name="united_states_excess_deaths",
    symbol_docstring=(
        "Excess deaths for the United States.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Excess deaths for the United States."
    ),
    symbol_source=dedent(
        '''
        def united_states_excess_deaths(ctx: EvalContext) -> float:
            """Excess deaths for the United States.

            Args:
                ctx: Workbook evaluation context.

            Returns:
                Excess deaths for the United States.
            """
            return xl_number(united_states_total_deaths(ctx))
        '''
    ).strip(),
)


MINIMAL_PROMPT_PAYLOAD: dict[str, object] = {
    "address": "Engine!C20",
    "function_name": "cell_engine_c20",
    "naming_hints": {
        "table_labels": "SHOCKED PATH",
        "row_labels": "Debt-to-GDP (%)",
        "column_labels": "1",
    },
}


def test_load_singleton_refactor_prompt_fixed_portion_matches_fixture() -> None:
    assert (
        load_singleton_refactor_prompt_fixed_portion().strip() == EXPECTED_FIXED_PORTION
    )


def test_prompt_for_singleton_refactor_starts_with_fixed_portion() -> None:
    prompt = _prompt_for_singleton_refactor(
        MINIMAL_PROMPT_PAYLOAD,
        SingletonRefactorLLMResponse.model_json_schema(),
    )
    context_marker = "\n\nSingleton context:"
    assert context_marker in prompt
    fixed_portion, _context = prompt.split(context_marker, maxsplit=1)
    assert fixed_portion.strip() == EXPECTED_FIXED_PORTION


def test_prompt_for_singleton_refactor_does_not_require_llm_note_section() -> None:
    prompt = _prompt_for_singleton_refactor(
        MINIMAL_PROMPT_PAYLOAD,
        SingletonRefactorLLMResponse.model_json_schema(),
    )
    assert "Include a Note section" not in prompt
    assert '"symbol_signature"' in prompt
    assert '"symbol_body"' in prompt
    assert '"symbol_source"' not in prompt.split("Singleton context:", maxsplit=1)[0]


def test_strip_python_string_delimiters_removes_triple_quotes() -> None:
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


def test_strip_python_string_delimiters_leaves_bare_docstring_unchanged() -> None:
    bare = (
        "Summary line.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    A value."
    )
    assert strip_python_string_delimiters(bare) == bare


def test_append_refactor_note_section_appends_covers_and_formula() -> None:
    docstring = (
        "Projected debt-to-GDP for period 1.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Projected debt-to-GDP ratio."
    )
    updated = append_refactor_note_section(
        docstring,
        address="Engine!C20",
        formula="=Inputs!B6+Engine!C10",
    )
    assert updated.endswith(
        "Note:\n    Covers Engine!C20. Excel: =Inputs!B6+Engine!C10."
    )
    assert "Note:" not in docstring


def test_assemble_singleton_symbol_source_indents_body_and_wraps_docstring() -> None:
    assembled = assemble_singleton_symbol_source(
        signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
        docstring=(
            "Projected debt-to-GDP for period 1.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio.\n\n"
            "Note:\n    Covers Engine!C20. Excel: =1."
        ),
        body="return shock_active(ctx, time_period=1)",
    )
    assert assembled == (
        "def projected_debt_to_gdp(ctx: EvalContext) -> float:\n"
        '    """Projected debt-to-GDP for period 1.\n'
        "\n"
        "Args:\n"
        "    ctx: Workbook evaluation context.\n"
        "\n"
        "Returns:\n"
        "    Projected debt-to-GDP ratio.\n"
        "\n"
        "Note:\n"
        "    Covers Engine!C20. Excel: =1.\n"
        '    """\n'
        "    return shock_active(ctx, time_period=1)\n"
    )


def test_prepare_singleton_refactor_response_assembles_and_appends_note() -> None:
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=1",
        normalized_formula="=Inputs!B6+Engine!C10",
        python_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
    )
    llm_response = SingletonRefactorLLMResponse(
        symbol_signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
        symbol_docstring=(
            '"""\n'
            "Projected debt-to-GDP for period 1.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio.\n"
            '"""'
        ),
        symbol_body="return shock_active(ctx, time_period=1)",
    )

    prepared = prepare_singleton_refactor_response(llm_response, ctx)

    assert isinstance(prepared, SingletonRefactorResponse)
    assert prepared.symbol_name == "projected_debt_to_gdp"
    assert "Note:\n    Covers Engine!C20. Excel: =Inputs!B6+Engine!C10." in (
        prepared.symbol_docstring
    )
    assert prepared.symbol_docstring in prepared.symbol_source
    assert "def projected_debt_to_gdp(ctx: EvalContext) -> float:" in (
        prepared.symbol_source
    )
    assert "return shock_active(ctx, time_period=1)" in prepared.symbol_source


def test_format_singleton_refactor_context_dump_matches_fixture() -> None:
    dump = format_singleton_refactor_context_dump(
        function_source=EXCESS_DEATHS_FUNCTION_SOURCE,
        cell_metadata=EXCESS_DEATHS_CELL_METADATA,
        dependency_stubs=EXCESS_DEATHS_DEPENDENCY_STUBS,
    )
    assert dump.strip() == EXPECTED_CONTEXT_DUMP


def test_build_singleton_refactor_context_dump_from_sources_matches_fixture() -> None:
    dump = build_singleton_refactor_context_dump(
        function_name="cell_some_sheet_z22",
        address="SomeSheet!Z22",
        internals_source=EXCESS_DEATHS_INTERNALS,
        runtime_source=EXCESS_DEATHS_RUNTIME_STUB,
        cell_metadata=EXCESS_DEATHS_CELL_METADATA,
    )
    assert dump.strip() == EXPECTED_CONTEXT_DUMP


def test_build_singleton_refactor_context_dump_excludes_legacy_prompt_fields() -> None:
    dump = build_singleton_refactor_context_dump(
        function_name="cell_some_sheet_z22",
        address="SomeSheet!Z22",
        internals_source=EXCESS_DEATHS_INTERNALS,
        runtime_source=EXCESS_DEATHS_RUNTIME_STUB,
        cell_metadata=EXCESS_DEATHS_CELL_METADATA,
    )
    assert "call_sites" not in dump
    assert "semantic_dependencies" not in dump
    assert "dependency_addresses" not in dump
    assert "naming_hints" not in dump


def test_prompt_for_singleton_refactor_appends_context_dump() -> None:
    context_dump = build_singleton_refactor_context_dump(
        function_name="cell_some_sheet_z22",
        address="SomeSheet!Z22",
        internals_source=EXCESS_DEATHS_INTERNALS,
        runtime_source=EXCESS_DEATHS_RUNTIME_STUB,
        cell_metadata=EXCESS_DEATHS_CELL_METADATA,
    )
    prompt = _prompt_for_singleton_refactor(context_dump)
    assert prompt.startswith(EXPECTED_FIXED_PORTION)
    assert prompt.endswith(context_dump.strip())
    assert "Function to refactor:" in prompt
    assert "Cell metadata:" in prompt
    assert "Dependencies:" in prompt


def test_parse_singleton_return_type_hint_from_signature() -> None:
    assert (
        parse_singleton_return_type_hint(
            "def united_states_excess_deaths(ctx: EvalContext) -> float:"
        )
        == "float"
    )

    assert (
        parse_singleton_return_type_hint(
            "def mixed_result(ctx: EvalContext) -> float | str:"
        )
        == "float | str"
    )


def test_validate_singleton_return_type_hint_accepts_allowlisted_types() -> None:
    for hint in ALLOWED_SINGLETON_RETURN_TYPE_HINTS:
        validate_singleton_return_type_hint(hint)


def test_validate_singleton_return_type_hint_accepts_union_of_allowlisted_scalars() -> (
    None
):
    validate_singleton_return_type_hint("float | str")
    validate_singleton_return_type_hint("int | float")


def test_validate_singleton_return_type_hint_rejects_xlerror_sentinel() -> None:
    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_singleton_return_type_hint("XlError")

    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_singleton_return_type_hint("float | XlError")


def test_validate_singleton_return_type_hint_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_singleton_return_type_hint("dict[str, float]")


def test_apply_singleton_refactor_plan_injects_eval_context_import() -> None:
    ctx = SingletonRefactorContext(
        address="SomeSheet!Z22",
        function_name="cell_some_sheet_z22",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_some_sheet_z22(ctx):\n    return 1.0\n",
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
    )
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        EVAL_CONTEXT_REFACTOR_RESPONSE,
    )
    assert "EvalContext" in updated
    assert "from .runtime import" in updated
    assert "EvalContext" in updated.split("from .runtime import", maxsplit=1)[1]

    applied, _rewrite_count = apply_singleton_refactor_plan(
        updated,
        EVAL_CONTEXT_REFACTOR_RESPONSE,
        ctx,
    )
    assert "from .runtime import" in applied
    assert "EvalContext" in applied.split("from .runtime import", maxsplit=1)[1]
    assert "def united_states_excess_deaths(ctx: EvalContext) -> float:" in applied
