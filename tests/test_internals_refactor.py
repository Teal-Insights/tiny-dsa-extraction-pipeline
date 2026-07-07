"""Unit tests for internals refactor validation, collapse, and prompt contracts."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from src.formula_clustering import FormulaCluster
from src.internals_refactor import (
    REFACTOR_PROMPT_VERSION,
    ClusterRefactorContext,
    ClusterRefactorResponse,
    HelperParameter,
    MemberContext,
    MemberKeyEntry,
    MemberKeys,
    SingletonRefactorContext,
    SingletonRefactorResponse,
    apply_cluster_collapse,
    apply_phase_c,
    apply_singleton_refactor_plan,
    build_singleton_refactor_context,
    collapse_bindings_for_response,
    prompt_payload,
    refactor_cache_key,
    singleton_prompt_payload,
    validate_allowed_global_references,
    validate_cluster_refactor_response,
    validate_parameter_names_match_vocabulary,
    validate_semantic_local_names,
    validate_uses_first_year_branch_flag,
    _prompt_for_refactor,
    _prompt_for_singleton_refactor,
    _single_function_def,
)
from excel_grapher.exporter import ProjectionResult
from src.refactor_bindings import KeyConceptSpec
from src.workbook_addresses import ProjectionColumnLayout

ALLOWED_RUNTIME_SYMBOLS = (
    "XlError",
    "xl_cell",
    "xl_eval",
)

TEST_LAYOUT = ProjectionColumnLayout(
    engine_sheet="Engine",
    engine_columns=("C", "D"),
    outputs_sheet="Outputs",
    outputs_column_to_engine={},
    time_period_to_engine_column={1: "C", 2: "D"},
)

RUNTIME_IMPORT = """from __future__ import annotations

from .runtime import (
    XlError,
    xl_cell,
    xl_eval,
    xl_number,
)
"""

RESOLVER_SECTION = """# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {}
_SYMBOL_DISPATCH = {}

def _address_to_func_name(address):
    return "cell_placeholder"

def _resolve_formula(address):
    return globals().get(_address_to_func_name(address))
"""

PRISTINE_CLUSTER = (
    RUNTIME_IMPORT
    + """
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    \"\"\"Covers Engine!C6.\"\"\"
    return xl_cell(ctx, 'Inputs!C1')

def cell_engine_d6(ctx):
    \"\"\"Covers Engine!D6.\"\"\"
    return xl_cell(ctx, 'Inputs!D1')

"""
    + RESOLVER_SECTION
)

KEY_VOCABULARY = (
    KeyConceptSpec(
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="time_period",
    ),
)

CLUSTER_MEMBERS = (
    MemberContext(
        address="Engine!C6",
        function_name="cell_engine_c6",
        engine_column="C",
        normalized_formula="=Inputs!C1",
        python_source="def cell_engine_c6(ctx):\n    return xl_cell(ctx, 'Inputs!C1')\n",
        dependency_addresses=(),
        dependency_functions=(),
    ),
    MemberContext(
        address="Engine!D6",
        function_name="cell_engine_d6",
        engine_column="D",
        normalized_formula="=Inputs!D1",
        python_source="def cell_engine_d6(ctx):\n    return xl_cell(ctx, 'Inputs!D1')\n",
        dependency_addresses=(),
        dependency_functions=(),
    ),
)

CLUSTER_CONTEXT = ClusterRefactorContext(
    cluster_id=1,
    canonical_template="=Inputs!{col}1",
    row=6,
    members=CLUSTER_MEMBERS,
    external_dependencies=(),
    semantic_dependencies=(),
    call_sites=(),
    first_year_column="C",
    allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
    key_vocabulary=KEY_VOCABULARY,
    expected_member_keys={
        "Engine!C6": {"TIME_PERIOD": 1},
        "Engine!D6": {"TIME_PERIOD": 2},
    },
    naming_hints={},
)

CLUSTER_DOCSTRING = (
    "Return the passthrough input for a projection period.\n\n"
    "Args:\n    ctx: Workbook evaluation context.\n"
    "    time_period: Projection period.\n\n"
    "Returns:\n    The corresponding input value.\n"
)

CLUSTER_PARAMETERS = (
    HelperParameter(name="time_period", concept="TIME_PERIOD", dtype="int"),
)

CLUSTER_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6",
        function_name="cell_engine_c6",
        keys=(MemberKeyEntry(concept="TIME_PERIOD", value=1),),
    ),
    MemberKeys(
        address="Engine!D6",
        function_name="cell_engine_d6",
        keys=(MemberKeyEntry(concept="TIME_PERIOD", value=2),),
    ),
)

VALID_CLUSTER_SOURCE = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    columns = {{1: 'C', 2: 'D'}}
    column = columns[time_period]
    return xl_cell(ctx, f'Inputs!{{column}}1')
'''


def _cluster_response(
    *,
    helper_source: str = VALID_CLUSTER_SOURCE,
    parameters: tuple[HelperParameter, ...] = CLUSTER_PARAMETERS,
    uses_first_year_branch: bool = False,
) -> ClusterRefactorResponse:
    return ClusterRefactorResponse(
        helper_name="combined_input_passthrough",
        helper_docstring=CLUSTER_DOCSTRING,
        uses_first_year_branch=uses_first_year_branch,
        parameters=parameters,
        helper_source=helper_source,
        member_keys=CLUSTER_MEMBER_KEYS,
    )


def test_refactor_cache_key_includes_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.internals_refactor.refactor_model", lambda: "test-model")
    schema: dict[str, object] = {"type": "object"}
    internals_bytes = PRISTINE_CLUSTER.encode()
    key_v14 = refactor_cache_key(CLUSTER_CONTEXT, internals_bytes, schema)
    original = REFACTOR_PROMPT_VERSION
    try:
        import src.internals_refactor as module

        module.REFACTOR_PROMPT_VERSION = 99  # ty: ignore[invalid-assignment]
        key_v99 = refactor_cache_key(CLUSTER_CONTEXT, internals_bytes, schema)
    finally:
        import src.internals_refactor as module

        module.REFACTOR_PROMPT_VERSION = original
    assert key_v14 != key_v99


def test_prompt_payload_includes_allowed_runtime_symbols() -> None:
    payload = prompt_payload(CLUSTER_CONTEXT)
    constraints = payload["constraints"]
    assert isinstance(constraints, dict)
    allowed = constraints.get("allowed_runtime_symbols")
    assert isinstance(allowed, list)
    assert "xl_cell" in allowed


def test_prompt_for_refactor_lists_allowed_runtime_symbols() -> None:
    payload = prompt_payload(CLUSTER_CONTEXT)
    schema = ClusterRefactorResponse.model_json_schema()
    prompt = _prompt_for_refactor(payload, schema)
    assert "xl_cell" in prompt
    assert "suggested_param_name" in prompt
    assert "uses_first_year_branch" in prompt


def test_validate_cluster_accepts_well_formed_response() -> None:
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        validate_cluster_refactor_response(
            CLUSTER_CONTEXT,
            _cluster_response(),
            existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
            internals_source=PRISTINE_CLUSTER,
        )


def test_validate_cluster_rejects_wrong_parameter_name() -> None:
    bad_parameters = (
        HelperParameter(name="period", concept="TIME_PERIOD", dtype="int"),
    )
    with pytest.raises(ValueError, match="suggested_param_name"):
        validate_parameter_names_match_vocabulary(
            CLUSTER_CONTEXT,
            _cluster_response(parameters=bad_parameters),
        )


def test_validate_cluster_rejects_excel_shaped_locals() -> None:
    bad_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    t1 = xl_cell(ctx, 'Inputs!C1')
    return t1
'''
    helper_def = _single_function_def(bad_source)
    assert helper_def is not None
    with pytest.raises(ValueError, match="excel-shaped local names"):
        validate_semantic_local_names(helper_def)


def test_validate_cluster_rejects_disallowed_global_reference() -> None:
    bad_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return mystery_helper(ctx)
'''
    helper_def = _single_function_def(bad_source)
    assert helper_def is not None
    with pytest.raises(ValueError, match="disallowed global names"):
        validate_allowed_global_references(
            helper_def,
            allowed_names={"xl_cell", "ctx", "time_period"},
        )


def test_validate_uses_first_year_branch_requires_branching_source() -> None:
    with pytest.raises(ValueError, match="uses_first_year_branch is True"):
        validate_uses_first_year_branch_flag(
            _cluster_response(uses_first_year_branch=True),
        )


def test_validate_uses_first_year_branch_accepts_matching_source() -> None:
    branch_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    if time_period == 1:
        return xl_cell(ctx, 'Inputs!C1')
    return xl_cell(ctx, 'Inputs!D1')
'''
    validate_uses_first_year_branch_flag(
        _cluster_response(
            helper_source=branch_source,
            uses_first_year_branch=True,
        )
    )


def test_collapse_bindings_for_response_renders_literal_calls() -> None:
    bindings = collapse_bindings_for_response(_cluster_response())
    assert len(bindings) == 2
    assert bindings[0].literal_call == "combined_input_passthrough(ctx, time_period=1)"
    assert bindings[1].literal_call == "combined_input_passthrough(ctx, time_period=2)"


def test_apply_cluster_collapse_rewrites_and_removes_wrappers() -> None:
    updated, rewrite_count = apply_cluster_collapse(
        PRISTINE_CLUSTER,
        _cluster_response(),
    )
    assert rewrite_count == 0
    assert "def cell_engine_c6" not in updated
    assert "def cell_engine_d6" not in updated
    assert "def combined_input_passthrough" in updated


def test_apply_phase_c_prunes_unreferenced_thin_wrappers() -> None:
    source = (
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def shock_active(ctx, time_period):
    \"\"\"Covers Engine!C10:G10.\"\"\"
    return xl_cell(ctx, 'Inputs!B21')

def cell_engine_c10(ctx):
    \"\"\"Thin wrapper.\"\"\"
    return shock_active(ctx, time_period=1)

"""
        + RESOLVER_SECTION
    )
    updated, pruned = apply_phase_c(source)
    assert pruned >= 1
    assert "def cell_engine_c10" not in updated
    assert "_ADDRESS_DISPATCH" in updated


def test_validate_allowed_global_references_allows_runtime_symbols() -> None:
    module = ast.parse(VALID_CLUSTER_SOURCE)
    function_def = next(
        node for node in module.body if isinstance(node, ast.FunctionDef)
    )
    validate_allowed_global_references(
        function_def,
        allowed_names={"xl_cell", "ctx", "time_period", "columns", "column"},
    )


INTERNALS_AFTER_C10_COLLAPSE = '''
def shock_active(ctx, time_period: int) -> float:
    """Return 1.0 when the shock is active for the given projection period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection period.

    Returns:
        1.0 when active.

    Note:
        Covers Engine!C10:G10. Excel: =IF(1,1,0).
    """
    return 1.0


def cell_inputs_b6(ctx) -> float:
    return xl_cell(ctx, "Inputs!B6")


def cell_engine_c20(ctx) -> float:
    return shock_active(ctx, time_period=1) + cell_inputs_b6(ctx)
'''


@dataclass(frozen=True)
class _ProjectionNode:
    normalized_formula: str


class _SingletonProjectionStub:
    def get_dependencies(self, address: str) -> tuple[str, ...]:
        if address == "Engine!C20":
            return ("Engine!C10", "Inputs!B6")
        return ()

    def get_node(self, address: str) -> _ProjectionNode | None:
        if address == "Engine!C20":
            return _ProjectionNode(normalized_formula="=Engine!C10+Inputs!B6")
        return None


def test_build_singleton_refactor_context_includes_collapsed_semantic_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(INTERNALS_AFTER_C10_COLLAPSE, encoding="utf-8")
    cluster = FormulaCluster(
        cluster_id=4,
        members=("Engine!C20",),
        canonical_template="=Engine!C10+Inputs!B6",
        row=20,
    )
    projection = cast(ProjectionResult, _SingletonProjectionStub())

    ctx = build_singleton_refactor_context(
        projection,
        cluster,
        internals_path,
    )

    assert ctx is not None
    assert ctx.external_dependencies == ("cell_inputs_b6", "shock_active")
    assert len(ctx.semantic_dependencies) == 1
    assert ctx.semantic_dependencies[0].helper_name == "shock_active"
    assert ctx.semantic_dependencies[0].call_form == (
        "shock_active(ctx, time_period=time_period)"
    )
    assert "Engine!C10" in ctx.semantic_dependencies[0].addresses

    payload = singleton_prompt_payload(ctx)
    semantic_dependencies = payload["semantic_dependencies"]
    assert isinstance(semantic_dependencies, list)
    assert len(semantic_dependencies) == 1
    assert semantic_dependencies[0] == {
        "address_template": ctx.semantic_dependencies[0].address_template,
        "columns": list(ctx.semantic_dependencies[0].columns),
        "helper_name": "shock_active",
        "call_form": "shock_active(ctx, time_period=time_period)",
    }

    prompt = _prompt_for_singleton_refactor(payload, {"type": "object"})
    assert "semantic_dependencies" in prompt
    assert "shock_active(ctx, time_period=time_period)" in prompt


INTERNALS_WITH_SINGLETON_CALLER = (
    RUNTIME_IMPORT
    + """
# --- Formula cell functions ---

def shock_active(ctx, time_period):
    return 1.0

def cell_engine_c20(ctx):
    \"\"\"Covers Engine!C20.\"\"\"
    return shock_active(ctx, time_period=1)

def cell_engine_d20(ctx):
    \"\"\"Covers Engine!D20.\"\"\"
    return xl_number(xl_eval(ctx, 'Engine!C20', cell_engine_c20))

"""
    + RESOLVER_SECTION
)

PROJECTED_DEBT_TO_GDP_SOURCE = '''def projected_debt_to_gdp(ctx):
    """Return projected debt-to-GDP for the first projection period.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Projected debt-to-GDP ratio.

    Note:
        Covers Engine!C20. Excel: =1.
    """
    return shock_active(ctx, time_period=1)
'''


def test_apply_singleton_refactor_plan_replaces_xl_eval_at_call_sites() -> None:
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        dependency_addresses=("Engine!C10",),
        external_dependencies=("shock_active",),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
    )
    response = SingletonRefactorResponse(
        symbol_name="projected_debt_to_gdp",
        symbol_docstring=(
            "Return projected debt-to-GDP for the first projection period.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio.\n\n"
            "Note:\n    Covers Engine!C20. Excel: =1."
        ),
        symbol_source=PROJECTED_DEBT_TO_GDP_SOURCE,
    )

    updated, rewrite_count = apply_singleton_refactor_plan(
        INTERNALS_WITH_SINGLETON_CALLER,
        response,
        ctx,
    )

    assert rewrite_count == 1
    assert "def cell_engine_c20" not in updated
    assert "def projected_debt_to_gdp" in updated
    assert "xl_eval(ctx, 'Engine!C20', cell_engine_c20)" not in updated
    assert "xl_eval(ctx, 'Engine!C20', projected_debt_to_gdp)" not in updated
    assert "xl_number(projected_debt_to_gdp(ctx))" in updated
