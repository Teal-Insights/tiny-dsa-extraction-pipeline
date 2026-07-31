"""Contract tests for singleton refactor prompt shape and response assembly."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent
from typing import TypedDict

import pytest

from src.internals_refactor import (
    SingletonRefactorContext,
    SingletonRefactorLLMResponse,
    SingletonRefactorResponse,
    _prepare_singleton_refactor_response,
    _prompt_for_singleton_refactor,
    append_refactor_note_section,
    apply_singleton_refactor_plan,
    assemble_singleton_symbol_source,
    build_singleton_refactor_context_dump,
    ensure_singleton_refactor_imports,
    format_singleton_refactor_context_dump,
    inject_signature_return_type_hint,
    load_singleton_refactor_prompt_fixed_portion,
    prepare_singleton_refactor_response,
    strip_python_string_delimiters,
    validate_singleton_refactor_response,
)
from src.refactor_return_types import (
    ALLOWED_REFACTOR_RETURN_TYPE_HINTS,
    validate_scalar_return_type_hint,
)

ALLOWED_SINGLETON_RETURN_TYPE_HINTS = ALLOWED_REFACTOR_RETURN_TYPE_HINTS

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

EXCESS_DEATHS_FUNCTION_SOURCE = dedent(
    """
    def cell_some_sheet_z22(ctx):
        return xl_number(united_states_total_deaths(ctx)) - xl_number(
            united_states_expected_deaths(ctx)
        )
    """
).strip()

EXCESS_DEATHS_CELL_METADATA: dict[str, object] = {
    "address": "SomeSheet!Z22",
    "binding_keys": {},
    "binding_record": {
        "TABLE": "United States Vital Statistics",
        "INDICATOR": "excess_deaths",
    },
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
        return xl_number(united_states_total_deaths(ctx)) - xl_number(
            united_states_expected_deaths(ctx)
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


class SingletonPrepareKwargs(TypedDict):
    runtime_source: str
    internals_source: str


SINGLETON_PREPARE_KWARGS: SingletonPrepareKwargs = {
    "runtime_source": EXCESS_DEATHS_RUNTIME_STUB,
    "internals_source": EXCESS_DEATHS_INTERNALS,
}

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

INTERNALS_WITH_PAREN_RUNTIME_IMPORT = dedent(
    """
    from __future__ import annotations

    from .runtime import (
        xl_cell,
        xl_number,
    )

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

CELLVALUE_REFACTOR_RESPONSE = SingletonRefactorResponse(
    symbol_name="inputs_c1",
    symbol_docstring="Inputs cell C1.",
    symbol_source=dedent(
        '''
        def inputs_c1(ctx: EvalContext) -> CellValue:
            """Inputs cell C1."""
            return xl_cell(ctx, "Inputs!C1")
        '''
    ).strip(),
)


MINIMAL_PROMPT_PAYLOAD: dict[str, object] = {
    "address": "Engine!C20",
    "function_name": "cell_engine_c20",
    "naming_hints": {
        "binding_record": {"TABLE": "SHOCKED PATH", "INDICATOR": "debt_to_gdp"},
        "binding_keys": {"TIME_PERIOD": 1},
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
    assert '"symbol_docstring"' in prompt
    assert '"symbol_signature"' not in prompt
    assert '"symbol_body"' in prompt
    assert '"symbol_source"' not in prompt.split("Singleton context:", maxsplit=1)[0]


def test_singleton_prompt_documents_error_escape_hatch() -> None:
    prompt = load_singleton_refactor_prompt_fixed_portion()
    assert "## Aborting" in prompt
    assert '"error"' in prompt
    assert '"error_reason"' in prompt
    assert "stop the pipeline" in prompt
    assert "set every success field" in prompt
    assert "to `null`" in prompt

    schema = SingletonRefactorLLMResponse.model_json_schema()
    properties = schema["properties"]
    assert "error" in properties
    assert "error_reason" in properties
    required = schema.get("required", [])
    assert "symbol_docstring" in required
    assert "symbol_signature" not in required
    assert "error" in required
    assert "error_reason" in required


def test_singleton_prompt_teaches_renaming_mechanical_temporaries() -> None:
    """Mechanical codegen already unpacks nested xl_* calls into _tN temps."""
    prompt = load_singleton_refactor_prompt_fixed_portion()
    assert "unpacking nested calls" not in prompt.lower()
    assert "_tN" in prompt
    assert "rename" in prompt.lower()
    assert "do not re-nest" in prompt.lower()
    assert "_t1 =" in prompt
    assert "total_deaths =" in prompt
    assert "lazy" in prompt.lower() or "short-circuit" in prompt.lower()


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
    docstring = (
        "Projected debt-to-GDP for period 1.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Projected debt-to-GDP ratio.\n\n"
        "Note:\n    Covers Engine!C20. Excel: =1."
    )
    assembled = assemble_singleton_symbol_source(
        signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
        docstring=docstring,
        body="return shock_active(ctx, time_period=1)",
    )
    assert assembled.startswith(
        "def projected_debt_to_gdp(ctx: EvalContext) -> float:\n"
    )
    assert "    return shock_active(ctx, time_period=1)\n" in assembled
    helper_def = ast.parse(assembled).body[0]
    assert isinstance(helper_def, ast.FunctionDef)
    embedded = helper_def.body[0]
    assert isinstance(embedded, ast.Expr)
    assert isinstance(embedded.value, ast.Constant)
    assert embedded.value.value == docstring


def test_assemble_singleton_symbol_source_roundtrips_escape_bearing_docstrings() -> (
    None
):
    for formula in (r'=A1&"\n"', '=CONCAT("""")'):
        docstring = (
            "Projected debt-to-GDP for period 1.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio.\n\n"
            f"Note:\n    Covers Engine!C20. Excel: {formula}."
        )
        assembled = assemble_singleton_symbol_source(
            signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
            docstring=docstring,
            body="return 1.0",
        )
        helper_def = ast.parse(assembled).body[0]
        assert isinstance(helper_def, ast.FunctionDef)
        embedded = helper_def.body[0]
        assert isinstance(embedded, ast.Expr)
        assert isinstance(embedded.value, ast.Constant)
        assert embedded.value.value == docstring


def test_validate_singleton_refactor_response_accepts_eval_context_type_hint() -> None:
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
        expected_helper_name="united_states_excess_deaths",
    )
    prepared = _prepare_singleton_refactor_response(
        prepare_singleton_refactor_response(
            SingletonRefactorLLMResponse(
                symbol_docstring=(
                    '"""\n'
                    "Excess deaths for the United States.\n\n"
                    "Args:\n    ctx: Workbook evaluation context.\n\n"
                    "Returns:\n    Excess deaths for the United States.\n"
                    '"""'
                ),
                symbol_body="return xl_number(united_states_total_deaths(ctx))",
                error=None,
                error_reason=None,
            ),
            ctx,
            **SINGLETON_PREPARE_KWARGS,
        ),
        ctx,
    )

    validate_singleton_refactor_response(
        ctx,
        prepared,
        existing_names=frozenset(
            {
                "cell_some_sheet_z22",
                "united_states_total_deaths",
                "united_states_expected_deaths",
            }
        ),
        internals_source=EXCESS_DEATHS_INTERNALS,
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
        expected_helper_name="projected_debt_to_gdp",
    )
    llm_response = SingletonRefactorLLMResponse(
        symbol_docstring=(
            '"""\n'
            "Projected debt-to-GDP for period 1.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio.\n"
            '"""'
        ),
        symbol_body="return shock_active(ctx, time_period=1)",
        error=None,
        error_reason=None,
    )

    prepared = prepare_singleton_refactor_response(
        llm_response, ctx, **SINGLETON_PREPARE_KWARGS
    )

    assert isinstance(prepared, SingletonRefactorResponse)
    assert prepared.symbol_name == "projected_debt_to_gdp"
    assert "Note:\n    Covers Engine!C20. Excel: =Inputs!B6+Engine!C10." in (
        prepared.symbol_docstring
    )
    helper_def = ast.parse(prepared.symbol_source).body[0]
    assert isinstance(helper_def, ast.FunctionDef)
    embedded = helper_def.body[0]
    assert isinstance(embedded, ast.Expr)
    assert isinstance(embedded.value, ast.Constant)
    assert embedded.value.value == prepared.symbol_docstring
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


def test_validate_scalar_return_type_hint_accepts_allowlisted_types() -> None:
    for hint in ALLOWED_SINGLETON_RETURN_TYPE_HINTS:
        validate_scalar_return_type_hint(hint)


def test_validate_scalar_return_type_hint_accepts_cellvalue_union() -> None:
    validate_scalar_return_type_hint("bool | CellValue")


def test_validate_scalar_return_type_hint_accepts_union_of_allowlisted_scalars() -> (
    None
):
    validate_scalar_return_type_hint("float | str")
    validate_scalar_return_type_hint("int | float")


def test_validate_scalar_return_type_hint_rejects_xlerror_sentinel() -> None:
    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_scalar_return_type_hint("XlError")

    with pytest.raises(ValueError, match="unsupported return type hint"):
        validate_scalar_return_type_hint("float | XlError")


def test_inject_signature_return_type_hint_replaces_existing() -> None:
    assert (
        inject_signature_return_type_hint(
            "def helper(ctx: EvalContext) -> CellValue:",
            "float",
        )
        == "def helper(ctx: EvalContext) -> float:"
    )
    assert (
        inject_signature_return_type_hint("def helper(ctx: EvalContext):", "float")
        == "def helper(ctx: EvalContext) -> float:"
    )


def test_prepare_singleton_refactor_response_ignores_llm_return_type_hint() -> None:
    ctx = SingletonRefactorContext(
        address="SomeSheet!Z22",
        function_name="cell_some_sheet_z22",
        canonical_template="=1",
        normalized_formula="=SomeSheet!A1-SomeSheet!A2",
        python_source=EXCESS_DEATHS_FUNCTION_SOURCE,
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints=EXCESS_DEATHS_CELL_METADATA,
        expected_helper_name="united_states_excess_deaths",
    )
    prepared = prepare_singleton_refactor_response(
        SingletonRefactorLLMResponse(
            symbol_docstring=(
                '"""\n'
                "Excess deaths for the United States.\n\n"
                "Args:\n    ctx: Workbook evaluation context.\n\n"
                "Returns:\n    Excess deaths for the United States.\n"
                '"""'
            ),
            symbol_body="return xl_number(united_states_total_deaths(ctx))",
            error=None,
            error_reason=None,
        ),
        ctx,
        **SINGLETON_PREPARE_KWARGS,
    )
    assert "-> float:" in prepared.symbol_source
    assert "-> str:" not in prepared.symbol_source


def test_prepare_singleton_refactor_response_injects_return_type_when_llm_uses_cellvalue() -> (
    None
):
    ctx = SingletonRefactorContext(
        address="SomeSheet!Z22",
        function_name="cell_some_sheet_z22",
        canonical_template="=1",
        normalized_formula="=SomeSheet!A1-SomeSheet!A2",
        python_source=EXCESS_DEATHS_FUNCTION_SOURCE,
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints=EXCESS_DEATHS_CELL_METADATA,
        expected_helper_name="united_states_excess_deaths",
    )
    prepared = prepare_singleton_refactor_response(
        SingletonRefactorLLMResponse(
            symbol_docstring=(
                '"""\n'
                "Excess deaths for the United States.\n\n"
                "Args:\n    ctx: Workbook evaluation context.\n\n"
                "Returns:\n    Excess deaths for the United States.\n"
                '"""'
            ),
            symbol_body="return xl_number(united_states_total_deaths(ctx))",
            error=None,
            error_reason=None,
        ),
        ctx,
        **SINGLETON_PREPARE_KWARGS,
    )
    assert "-> float:" in prepared.symbol_source
    assert "CellValue" not in prepared.symbol_source.split('"""', maxsplit=1)[0]


def test_prepare_singleton_refactor_response_propagates_cellvalue_for_opaque_passthrough() -> (
    None
):
    runtime_source = dedent(
        """
        def xl_cell(ctx, address: str) -> CellValue:
            ...
        """
    ).strip()
    python_source = dedent(
        """
        def cell_inputs_c1(ctx):
            return xl_cell(ctx, "Inputs!C1")
        """
    ).strip()
    ctx = SingletonRefactorContext(
        address="Inputs!C1",
        function_name="cell_inputs_c1",
        canonical_template="=1",
        normalized_formula="=Inputs!C1",
        python_source=python_source,
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={"address": "Inputs!C1"},
        expected_helper_name="inputs_c1",
    )
    prepared = prepare_singleton_refactor_response(
        SingletonRefactorLLMResponse(
            symbol_docstring='"""\nInputs cell C1.\n"""',
            symbol_body='return xl_cell(ctx, "Inputs!C1")',
            error=None,
            error_reason=None,
        ),
        ctx,
        runtime_source=runtime_source,
        internals_source="",
    )
    assert "-> CellValue:" in prepared.symbol_source


def test_build_singleton_refactor_context_dump_includes_cellvalue_return_on_xl_cell() -> (
    None
):
    runtime_source = dedent(
        """
        def xl_cell(ctx, address):
            return None
        """
    ).strip()
    internals_source = dedent(
        """
        def cell_x(ctx):
            return xl_cell(ctx, "Sheet!A1")
        """
    ).strip()
    dump = build_singleton_refactor_context_dump(
        function_name="cell_x",
        address="Sheet!A1",
        internals_source=internals_source,
        runtime_source=runtime_source,
        cell_metadata={"address": "Sheet!A1"},
    )
    assert "def xl_cell(ctx, address) -> CellValue:" in dump


def test_ensure_singleton_refactor_imports_handles_parenthesized_runtime_import() -> (
    None
):
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITH_PAREN_RUNTIME_IMPORT,
        EVAL_CONTEXT_REFACTOR_RESPONSE,
    )
    ast.parse(updated)
    assert "EvalContext" in updated.split("from .runtime import", maxsplit=1)[1]


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
        expected_helper_name="united_states_excess_deaths",
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


def test_ensure_singleton_refactor_imports_injects_referenced_runtime_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_module_symbols",
        lambda *_args, **_kwargs: ("XlError", "xl_cell", "xl_number", "xl_raise"),
    )
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        CELLVALUE_REFACTOR_RESPONSE,
        package_root=Path("."),
    )
    import_line = next(
        line for line in updated.splitlines() if line.startswith("from .runtime import")
    )
    assert "xl_cell" in import_line
    assert "CellValue" in import_line
    assert "EvalContext" in import_line
    # Unreferenced runtime symbols stay out of the bundle.
    assert "xl_raise" not in import_line
    ast.parse(updated)


def test_ensure_singleton_refactor_imports_ignores_docstring_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symbol named only in prose is not a reference and needs no import."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_module_symbols",
        lambda *_args, **_kwargs: ("xl_cell", "xl_number"),
    )
    response = SingletonRefactorResponse(
        symbol_name="inputs_c1",
        symbol_docstring="Inputs cell C1.",
        symbol_source=dedent(
            '''
            def inputs_c1(ctx: EvalContext) -> CellValue:
                """Inputs cell C1, formerly read through xl_cell."""
                return 1.0
            '''
        ).strip(),
    )
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        response,
        package_root=Path("."),
    )
    import_line = next(
        line for line in updated.splitlines() if line.startswith("from .runtime import")
    )
    assert "xl_cell" not in import_line


def test_ensure_singleton_refactor_imports_ignores_locally_bound_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local that shadows a runtime name is not an unresolved reference."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_module_symbols",
        lambda *_args, **_kwargs: ("as_scalar", "xl_cell"),
    )
    response = SingletonRefactorResponse(
        symbol_name="inputs_c1",
        symbol_docstring="Inputs cell C1.",
        symbol_source=dedent(
            '''
            def inputs_c1(ctx: EvalContext) -> CellValue:
                """Inputs cell C1."""
                as_scalar = xl_cell(ctx, "Inputs!C1")
                return as_scalar
            '''
        ).strip(),
    )
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        response,
        package_root=Path("."),
    )
    import_line = next(
        line for line in updated.splitlines() if line.startswith("from .runtime import")
    )
    assert "xl_cell" in import_line
    assert "as_scalar" not in import_line


def test_ensure_singleton_refactor_imports_injects_cellvalue() -> None:
    updated = ensure_singleton_refactor_imports(
        INTERNALS_WITHOUT_EVAL_CONTEXT_IMPORT,
        CELLVALUE_REFACTOR_RESPONSE,
    )
    import_line = next(
        line for line in updated.splitlines() if line.startswith("from .runtime import")
    )
    assert "CellValue" in import_line
    assert "EvalContext" in import_line


def test_apply_singleton_refactor_plan_injects_cellvalue_import() -> None:
    internals = dedent(
        """
        from __future__ import annotations

        from .runtime import xl_cell

        def cell_inputs_c1(ctx):
            return xl_cell(ctx, "Inputs!C1")
        """
    ).strip()
    ctx = SingletonRefactorContext(
        address="Inputs!C1",
        function_name="cell_inputs_c1",
        canonical_template="=Inputs!C1",
        normalized_formula="=Inputs!C1",
        python_source="def cell_inputs_c1(ctx):\n    return xl_cell(ctx, 'Inputs!C1')\n",
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
        expected_helper_name="inputs_c1",
    )
    applied, _rewrite_count = apply_singleton_refactor_plan(
        internals,
        CELLVALUE_REFACTOR_RESPONSE,
        ctx,
    )
    import_line = next(
        line for line in applied.splitlines() if line.startswith("from .runtime import")
    )
    assert "CellValue" in import_line
    assert "EvalContext" in import_line
    assert "def inputs_c1(ctx: EvalContext) -> CellValue:" in applied
