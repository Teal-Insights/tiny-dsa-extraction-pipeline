"""Unit tests for internals refactor validation, collapse, and prompt contracts."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.formula_clustering import FormulaCluster
from src.internals_refactor import (
    REFACTOR_PROMPT_VERSION,
    ClusterRefactorContext,
    ClusterRefactorLLMResponse,
    ClusterRefactorResponse,
    HelperParameter,
    InternalsSourceIndex,
    MemberContext,
    MemberKeyEntry,
    MemberKeys,
    RefactorDeclaredError,
    SingletonRefactorContext,
    SingletonRefactorLLMResponse,
    SingletonRefactorResponse,
    apply_cluster_collapse,
    apply_phase_c,
    apply_singleton_refactor_plan,
    build_cluster_refactor_context,
    build_cluster_refactor_prompt_context,
    build_singleton_refactor_context,
    build_singleton_refactor_prompt_context,
    collapse_bindings_for_response,
    extract_function_source,
    llm_refactor_cluster,
    llm_refactor_singleton,
    load_cluster_refactor_prompt_fixed_portion,
    prepare_singleton_refactor_response,
    prompt_payload,
    raise_if_llm_declared_error,
    refactor_cache_key,
    refactor_internals_all_clusters,
    refactor_internals_singleton,
    singleton_prompt_payload,
    validate_allowed_global_references,
    validate_cluster_refactor_response,
    validate_parameter_names_match_vocabulary,
    validate_semantic_local_names,
    write_refactor_failure_diagnostic,
    _prepare_cluster_refactor_response,
    _prompt_for_refactor,
    _prompt_for_singleton_refactor,
    _single_function_def,
)
from src.llm_json import DEFAULT_MAX_ATTEMPTS
from src.refactor_parity_gate import ParityError
from excel_grapher.exporter import ProjectionResult
from src.refactor_bindings import BindingKeyValue, KeyConceptSpec
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
        dimension_id="TIME_PERIOD",
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
    HelperParameter(name="time_period", dimension_id="TIME_PERIOD", dtype="int"),
)

CLUSTER_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6",
        function_name="cell_engine_c6",
        keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
    ),
    MemberKeys(
        address="Engine!D6",
        function_name="cell_engine_d6",
        keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=2),),
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
    member_keys: tuple[MemberKeys, ...] = CLUSTER_MEMBER_KEYS,
) -> ClusterRefactorResponse:
    return ClusterRefactorResponse(
        helper_name="combined_input_passthrough",
        helper_docstring=CLUSTER_DOCSTRING,
        parameters=parameters,
        helper_source=helper_source,
        member_keys=member_keys,
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


def test_write_refactor_failure_diagnostic_persists_response_artifacts(
    tmp_path: Path,
) -> None:
    dump_dir = write_refactor_failure_diagnostic(
        kind="singleton",
        target="Engine!C20",
        error=ValueError("parity mismatch"),
        dump_dir=tmp_path,
        user_prompt="prompt body",
        llm_response={
            "symbol_signature": "def debt_to_gdp_shocked_path(ctx: EvalContext) -> float:",
            "symbol_docstring": "Example.",
            "symbol_body": "return 1.0",
        },
        prepared_response={
            "symbol_name": "debt_to_gdp_shocked_path",
            "symbol_docstring": "Example.",
            "symbol_source": "def debt_to_gdp_shocked_path(ctx: EvalContext) -> float:\n    return 1.0\n",
        },
        raw_content='{"symbol_body": "return 1.0"}',
        source="llm",
        model="test-model",
    )

    manifest = json.loads((dump_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["target"] == "Engine!C20"
    assert manifest["error_type"] == "ValueError"
    assert manifest["model"] == "test-model"
    assert (dump_dir / "llm_response.json").exists()
    assert (dump_dir / "prepared_response.json").exists()
    assert (dump_dir / "user_prompt.md").read_text(encoding="utf-8") == "prompt body"
    assert (dump_dir / "error.txt").read_text(encoding="utf-8") == "parity mismatch\n"
    llm_response = json.loads(
        (dump_dir / "llm_response.json").read_text(encoding="utf-8")
    )
    assert llm_response["symbol_body"] == "return 1.0"


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
    assert "uses_first_year_branch" not in prompt
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "uses_first_year_branch" not in properties


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


def test_validate_cluster_rejects_duplicate_member_key_combinations() -> None:
    duplicate_member_keys = (
        CLUSTER_MEMBER_KEYS[0],
        MemberKeys(
            address="Engine!D6",
            function_name="cell_engine_d6",
            keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
        ),
    )
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(ValueError, match="unique key combination"):
            validate_cluster_refactor_response(
                CLUSTER_CONTEXT,
                _cluster_response(member_keys=duplicate_member_keys),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )


def test_validate_cluster_accepts_eval_context_type_hint() -> None:
    helper_source = f'''def combined_input_passthrough(ctx: EvalContext, time_period: int) -> float:
    """{CLUSTER_DOCSTRING}"""
    columns = {{1: 'C', 2: 'D'}}
    column = columns[time_period]
    return xl_cell(ctx, f'Inputs!{{column}}1')
'''
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        validate_cluster_refactor_response(
            CLUSTER_CONTEXT,
            _cluster_response(helper_source=helper_source),
            existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
            internals_source=PRISTINE_CLUSTER,
        )


def test_validate_cluster_rejects_wrong_parameter_name() -> None:
    bad_parameters = (
        HelperParameter(name="period", dimension_id="TIME_PERIOD", dtype="int"),
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


def test_collapse_bindings_for_response_renders_literal_calls() -> None:
    bindings = collapse_bindings_for_response(_cluster_response())
    assert len(bindings) == 2
    assert bindings[0].literal_call == "combined_input_passthrough(ctx, time_period=1)"
    assert bindings[1].literal_call == "combined_input_passthrough(ctx, time_period=2)"


def test_collapse_bindings_for_dual_period_dimension_ids() -> None:
    response = ClusterRefactorResponse(
        helper_name="dual_period_lookup",
        helper_docstring="Lookup.\n\nArgs:\n    ctx: Context.\n",
        parameters=(
            HelperParameter(
                name="projection_period",
                dimension_id="PROJECTION_PERIOD",
                dtype="int",
            ),
            HelperParameter(
                name="reference_period",
                dimension_id="REFERENCE_PERIOD",
                dtype="int",
            ),
        ),
        helper_source=(
            "def dual_period_lookup(ctx, projection_period, reference_period):\n"
            "    return projection_period + reference_period\n"
        ),
        member_keys=(
            MemberKeys(
                address="Engine!C10",
                function_name="cell_engine_c10",
                keys=(
                    MemberKeyEntry(dimension_id="PROJECTION_PERIOD", value=1),
                    MemberKeyEntry(dimension_id="REFERENCE_PERIOD", value=0),
                ),
            ),
        ),
    )
    bindings = collapse_bindings_for_response(response)
    assert bindings[0].literal_call == (
        "dual_period_lookup(ctx, projection_period=1, reference_period=0)"
    )


def test_helper_parameter_accepts_legacy_concept_only_payload() -> None:
    parameter = HelperParameter.model_validate(
        {"name": "time_period", "concept": "TIME_PERIOD", "dtype": "int"}
    )
    assert parameter.dimension_id == "TIME_PERIOD"
    assert parameter.concept == "TIME_PERIOD"


def test_member_key_entry_accepts_legacy_concept_only_payload() -> None:
    entry = MemberKeyEntry.model_validate({"concept": "TIME_PERIOD", "value": 1})
    assert entry.dimension_id == "TIME_PERIOD"


def test_prepare_resolves_legacy_concept_payload_against_vocabulary() -> None:
    response = ClusterRefactorResponse.model_validate(
        {
            "helper_name": "combined_input_passthrough",
            "helper_docstring": CLUSTER_DOCSTRING,
            "parameters": [
                {"name": "time_period", "concept": "TIME_PERIOD", "dtype": "int"}
            ],
            "helper_source": VALID_CLUSTER_SOURCE,
            "member_keys": [
                {
                    "address": "Engine!C6",
                    "function_name": "cell_engine_c6",
                    "keys": [{"concept": "TIME_PERIOD", "value": 1}],
                },
                {
                    "address": "Engine!D6",
                    "function_name": "cell_engine_d6",
                    "keys": [{"concept": "TIME_PERIOD", "value": 2}],
                },
            ],
        }
    )
    prepared = _prepare_cluster_refactor_response(response, CLUSTER_CONTEXT)
    assert prepared.parameters[0].dimension_id == "TIME_PERIOD"
    assert prepared.parameters[0].concept == "TIME_PERIOD"
    assert prepared.member_keys[0].keys[0].dimension_id == "TIME_PERIOD"


def test_prepare_rejects_concept_mismatch_for_dimension_id() -> None:
    response = _cluster_response(
        parameters=(
            HelperParameter(
                name="time_period",
                dimension_id="TIME_PERIOD",
                concept="REF_AREA",
                dtype="int",
            ),
        )
    )
    with pytest.raises(ValueError, match="does not match vocabulary concept"):
        _prepare_cluster_refactor_response(response, CLUSTER_CONTEXT)


def test_prepare_rejects_ambiguous_shared_concept_without_dimension_id() -> None:
    dual_vocab = (
        KeyConceptSpec(
            dimension_id="PROJECTION_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="projection_period",
        ),
        KeyConceptSpec(
            dimension_id="REFERENCE_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="reference_period",
        ),
    )
    ctx = ClusterRefactorContext(
        cluster_id=1,
        canonical_template="=Inputs!{col}1",
        row=6,
        members=CLUSTER_MEMBERS,
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        first_year_column="C",
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        key_vocabulary=dual_vocab,
        expected_member_keys={
            "Engine!C6": {"PROJECTION_PERIOD": 1},
            "Engine!D6": {"PROJECTION_PERIOD": 2},
        },
        naming_hints={},
    )
    response = ClusterRefactorResponse.model_validate(
        {
            "helper_name": "combined_input_passthrough",
            "helper_docstring": CLUSTER_DOCSTRING,
            "parameters": [
                {"name": "time_period", "concept": "TIME_PERIOD", "dtype": "int"}
            ],
            "helper_source": VALID_CLUSTER_SOURCE,
            "member_keys": [
                {
                    "address": "Engine!C6",
                    "function_name": "cell_engine_c6",
                    "keys": [{"concept": "TIME_PERIOD", "value": 1}],
                },
                {
                    "address": "Engine!D6",
                    "function_name": "cell_engine_d6",
                    "keys": [{"concept": "TIME_PERIOD", "value": 2}],
                },
            ],
        }
    )
    with pytest.raises(ValueError, match="ambiguous"):
        _prepare_cluster_refactor_response(response, ctx)


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


SINGLETON_LLM_RESPONSE = SingletonRefactorLLMResponse(
    symbol_signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
    symbol_docstring=(
        "Projected debt-to-GDP.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Projected debt-to-GDP ratio."
    ),
    symbol_body="return 1.0",
    error=None,
    error_reason=None,
)


def _singleton_refactor_test_context(tmp_path: Path) -> SingletonRefactorContext:
    return SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
    )


def test_llm_refactor_singleton_wires_parity_into_post_validate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    recorded: dict[str, object] = {}
    parity_calls: list[int] = []

    def fake_generate_validated_json(
        **kwargs: object,
    ) -> tuple[SingletonRefactorLLMResponse, str]:
        recorded["post_validate"] = kwargs.get("post_validate")
        recorded["max_attempts"] = kwargs.get("max_attempts")
        post_validate = cast(
            Callable[[SingletonRefactorLLMResponse], SingletonRefactorLLMResponse],
            kwargs["post_validate"],
        )
        validated = post_validate(SINGLETON_LLM_RESPONSE)
        return validated, SINGLETON_LLM_RESPONSE.model_dump_json()

    def track_parity(**kwargs: object) -> None:
        parity_calls.append(1)

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))
    monkeypatch.setattr(
        module,
        "build_singleton_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        "src.refactor_parity_gate.check_singleton_parity",
        track_parity,
    )

    response = llm_refactor_singleton(
        ctx,
        internals_path=internals_path,
        pristine_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        input_vectors=[{}],
    )

    assert response.symbol_name == "projected_debt_to_gdp"
    assert recorded["post_validate"] is not None
    assert recorded["max_attempts"] == DEFAULT_MAX_ATTEMPTS
    assert parity_calls == [1]


def test_llm_refactor_singleton_post_validate_retries_on_parity_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    parity_calls = 0

    def fake_parity(**kwargs: object) -> None:
        nonlocal parity_calls
        parity_calls += 1
        if parity_calls == 1:
            raise ParityError("parity mismatch on vector #0")

    def fake_generate_validated_json(
        **kwargs: object,
    ) -> tuple[SingletonRefactorLLMResponse, str]:
        post_validate = cast(
            Callable[[SingletonRefactorLLMResponse], SingletonRefactorLLMResponse],
            kwargs["post_validate"],
        )
        max_attempts = cast(int, kwargs["max_attempts"])
        llm_response = SINGLETON_LLM_RESPONSE
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                validated = post_validate(llm_response)
                return validated, llm_response.model_dump_json()
            except ValueError as error:
                last_error = error
                if attempt + 1 >= max_attempts:
                    break
                llm_response = llm_response.model_copy(
                    update={"symbol_body": "return 2.0"},
                )
        raise RuntimeError("exhausted attempts") from last_error

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))
    monkeypatch.setattr(
        module,
        "build_singleton_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        "src.refactor_parity_gate.check_singleton_parity",
        fake_parity,
    )

    response = llm_refactor_singleton(
        ctx,
        internals_path=internals_path,
        pristine_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        input_vectors=[{}],
    )

    assert parity_calls == 2
    assert "return 2.0" in response.symbol_source


def test_singleton_llm_response_omits_optional_error_fields() -> None:
    response = SingletonRefactorLLMResponse(
        symbol_signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
        symbol_docstring=(
            "Projected debt-to-GDP.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Projected debt-to-GDP ratio."
        ),
        symbol_body="return 1.0",
        error=None,
        error_reason=None,
    )
    assert response.error is None
    assert response.error_reason is None


def test_singleton_llm_response_error_requires_nonempty_reason() -> None:
    with pytest.raises(ValidationError, match="error_reason"):
        SingletonRefactorLLMResponse(
            symbol_signature=None,
            symbol_docstring=None,
            symbol_body=None,
            error=True,
            error_reason="   ",
        )


def test_singleton_llm_response_error_requires_null_success_fields() -> None:
    with pytest.raises(ValidationError, match="success fields must be null"):
        SingletonRefactorLLMResponse(
            symbol_signature="def projected_debt_to_gdp(ctx: EvalContext) -> float:",
            symbol_docstring="Doc.",
            symbol_body="return 1.0",
            error=True,
            error_reason="Cannot proceed.",
        )


def test_singleton_llm_response_error_allows_null_success_fields() -> None:
    response = SingletonRefactorLLMResponse(
        symbol_signature=None,
        symbol_docstring=None,
        symbol_body=None,
        error=True,
        error_reason="Unsupported independent operand variation.",
    )
    assert response.error is True
    assert response.symbol_signature is None
    with pytest.raises(RefactorDeclaredError, match="Unsupported independent"):
        raise_if_llm_declared_error(
            response,
            kind="singleton",
            target="Engine!C20",
        )


def test_cluster_llm_response_error_allows_null_success_fields() -> None:
    response = ClusterRefactorLLMResponse(
        symbol_signature=None,
        symbol_docstring=None,
        symbol_body=None,
        parameters=None,
        member_keys=None,
        error=True,
        error_reason="Cluster members lack unique binding-key triangulation.",
    )
    assert response.error is True
    assert response.parameters is None
    with pytest.raises(RefactorDeclaredError, match="unique binding-key"):
        raise_if_llm_declared_error(
            response,
            kind="cluster",
            target="cluster_1",
        )


def test_llm_refactor_singleton_aborts_on_declared_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    prepare_calls = 0
    error_payload = {
        "symbol_signature": None,
        "symbol_docstring": None,
        "symbol_body": None,
        "error": True,
        "error_reason": "Cannot safely rename this singleton.",
    }

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> _FakeResponse:
            self.calls.append(kwargs)
            return _FakeResponse(json.dumps(error_payload))

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    fake_client = _FakeClient()

    def boom_prepare(*_args: object, **_kwargs: object) -> SingletonRefactorResponse:
        nonlocal prepare_calls
        prepare_calls += 1
        raise AssertionError("prepare should not run for declared errors")

    monkeypatch.setattr(
        module,
        "build_client",
        lambda _model: (
            fake_client,
            module.provider_for_model("glm-test"),
        ),
    )
    monkeypatch.setattr(module, "prepare_singleton_refactor_response", boom_prepare)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "glm-test")
    monkeypatch.setattr(
        module,
        "build_singleton_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        module,
        "write_refactor_failure_diagnostic",
        lambda **kwargs: tmp_path / "dump",
    )

    with pytest.raises(RefactorDeclaredError, match="Cannot safely rename"):
        llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source="def cell_engine_c20(ctx):\n    return 1.0\n",
            input_vectors=[{}],
        )

    assert len(fake_client.chat.completions.calls) == 1
    assert prepare_calls == 0


def test_llm_refactor_cluster_aborts_on_declared_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(PRISTINE_CLUSTER, encoding="utf-8")
    prepare_calls = 0
    error_payload = {
        "symbol_signature": None,
        "symbol_docstring": None,
        "symbol_body": None,
        "parameters": None,
        "member_keys": None,
        "error": True,
        "error_reason": "Cannot safely collapse this cluster.",
    }

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> _FakeResponse:
            self.calls.append(kwargs)
            return _FakeResponse(json.dumps(error_payload))

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    fake_client = _FakeClient()

    def boom_prepare(*_args: object, **_kwargs: object) -> ClusterRefactorResponse:
        nonlocal prepare_calls
        prepare_calls += 1
        raise AssertionError("prepare should not run for declared errors")

    monkeypatch.setattr(
        module,
        "build_client",
        lambda _model: (
            fake_client,
            module.provider_for_model("glm-test"),
        ),
    )
    monkeypatch.setattr(module, "prepare_cluster_refactor_response", boom_prepare)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "glm-test")
    monkeypatch.setattr(
        module,
        "build_cluster_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        module,
        "write_refactor_failure_diagnostic",
        lambda **kwargs: tmp_path / "dump",
    )

    with pytest.raises(RefactorDeclaredError, match="Cannot safely collapse"):
        llm_refactor_cluster(
            CLUSTER_CONTEXT,
            internals_path=internals_path,
            pristine_source=PRISTINE_CLUSTER,
            input_vectors=[{}],
        )

    assert len(fake_client.chat.completions.calls) == 1
    assert prepare_calls == 0


def test_llm_refactor_singleton_declared_error_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    dump_root = tmp_path / "failures"
    error_payload = {
        "symbol_signature": None,
        "symbol_docstring": None,
        "symbol_body": None,
        "error": True,
        "error_reason": "Ambiguous naming hints; aborting.",
    }

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse(json.dumps(error_payload))

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    monkeypatch.setattr(
        module,
        "build_client",
        lambda _model: (
            _FakeClient(),
            module.provider_for_model("glm-test"),
        ),
    )
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "glm-test")
    monkeypatch.setattr(
        module,
        "build_singleton_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(module, "REFACTOR_FAILURE_DUMP_DIR", dump_root)

    with pytest.raises(RefactorDeclaredError, match="Ambiguous naming hints"):
        llm_refactor_singleton(ctx, internals_path=internals_path)

    dumps = list(dump_root.iterdir())
    assert len(dumps) == 1
    dump_dir = dumps[0]
    error_text = (dump_dir / "error.txt").read_text(encoding="utf-8")
    assert "Ambiguous naming hints" in error_text
    llm_response = json.loads(
        (dump_dir / "llm_response.json").read_text(encoding="utf-8")
    )
    assert llm_response["error"] is True
    assert llm_response["error_reason"] == "Ambiguous naming hints; aborting."


# --- Dual cluster-refactor contracts (issue #74) ---

DUAL_PERIOD_LAYOUT = ProjectionColumnLayout(
    engine_sheet="Engine",
    engine_columns=("C", "D"),
    outputs_sheet="Outputs",
    outputs_column_to_engine={},
    time_period_to_engine_column={1: "C", 2: "D"},
    projection_dimension_id="PROJECTION_PERIOD",
)

DUAL_PERIOD_VOCABULARY = (
    KeyConceptSpec(
        dimension_id="PROJECTION_PERIOD",
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="projection_period",
    ),
    KeyConceptSpec(
        dimension_id="REFERENCE_PERIOD",
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="reference_period",
    ),
)

DUAL_PERIOD_CONTEXT = replace(
    CLUSTER_CONTEXT,
    key_vocabulary=DUAL_PERIOD_VOCABULARY,
    expected_member_keys={
        "Engine!C6": {"PROJECTION_PERIOD": 1, "REFERENCE_PERIOD": 0},
        "Engine!D6": {"PROJECTION_PERIOD": 2, "REFERENCE_PERIOD": 0},
    },
    contract="dimension_aware",
)

DUAL_PERIOD_DOCSTRING = (
    "Return the indicator change relative to its reference period.\n\n"
    "Args:\n    ctx: Workbook evaluation context.\n"
    "    projection_period: Projection period index.\n"
    "    reference_period: Reference period index.\n\n"
    "Returns:\n    Current value minus the reference-period value.\n"
)

DUAL_PERIOD_SOURCE = f'''def indicator_change_from_reference(ctx, projection_period, reference_period):
    """{DUAL_PERIOD_DOCSTRING}"""
    column_by_period = {{0: 'B', 1: 'C', 2: 'D'}}
    current_value = xl_cell(ctx, f'Inputs!{{column_by_period[projection_period]}}1')
    reference_value = xl_cell(ctx, f'Inputs!{{column_by_period[reference_period]}}1')
    return current_value - reference_value
'''

DUAL_PERIOD_PARAMETERS = (
    HelperParameter(
        name="projection_period", dimension_id="PROJECTION_PERIOD", dtype="int"
    ),
    HelperParameter(
        name="reference_period", dimension_id="REFERENCE_PERIOD", dtype="int"
    ),
)

DUAL_PERIOD_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6",
        function_name="cell_engine_c6",
        keys=(
            MemberKeyEntry(dimension_id="PROJECTION_PERIOD", value=1),
            MemberKeyEntry(dimension_id="REFERENCE_PERIOD", value=0),
        ),
    ),
    MemberKeys(
        address="Engine!D6",
        function_name="cell_engine_d6",
        keys=(
            MemberKeyEntry(dimension_id="PROJECTION_PERIOD", value=2),
            MemberKeyEntry(dimension_id="REFERENCE_PERIOD", value=0),
        ),
    ),
)


def _dimension_aware_response(
    *,
    parameters: tuple[HelperParameter, ...] = DUAL_PERIOD_PARAMETERS,
    member_keys: tuple[MemberKeys, ...] = DUAL_PERIOD_MEMBER_KEYS,
) -> ClusterRefactorResponse:
    return ClusterRefactorResponse(
        helper_name="indicator_change_from_reference",
        helper_docstring=DUAL_PERIOD_DOCSTRING,
        parameters=parameters,
        helper_source=DUAL_PERIOD_SOURCE,
        member_keys=member_keys,
    )


def test_validate_dimension_aware_accepts_counterpart_parameters() -> None:
    """Contract B accepts two parameters sharing one concept via distinct ids."""
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=DUAL_PERIOD_LAYOUT,
    ):
        validate_cluster_refactor_response(
            DUAL_PERIOD_CONTEXT,
            _dimension_aware_response(),
            existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
            internals_source=PRISTINE_CLUSTER,
        )


def test_validate_dimension_aware_rejects_collapsed_concept_parameter() -> None:
    """Contract B rejects one concept parameter standing in for two dimensions."""
    collapsed_member_keys = (
        MemberKeys(
            address="Engine!C6",
            function_name="cell_engine_c6",
            keys=(MemberKeyEntry(dimension_id="PROJECTION_PERIOD", value=1),),
        ),
        MemberKeys(
            address="Engine!D6",
            function_name="cell_engine_d6",
            keys=(MemberKeyEntry(dimension_id="PROJECTION_PERIOD", value=2),),
        ),
    )
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=DUAL_PERIOD_LAYOUT,
    ):
        with pytest.raises(ValueError, match="collapses distinct dimensions"):
            validate_cluster_refactor_response(
                DUAL_PERIOD_CONTEXT,
                _dimension_aware_response(
                    parameters=(DUAL_PERIOD_PARAMETERS[0],),
                    member_keys=collapsed_member_keys,
                ),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )


def test_prepare_dimension_aware_rejects_bare_concept_dimension_id() -> None:
    """A parameter keyed by the shared concept name cannot pick a dimension."""
    ambiguous_parameters = (
        HelperParameter(
            name="projection_period", dimension_id="TIME_PERIOD", dtype="int"
        ),
        DUAL_PERIOD_PARAMETERS[1],
    )
    with pytest.raises(ValueError, match="ambiguous binding key"):
        _prepare_cluster_refactor_response(
            _dimension_aware_response(parameters=ambiguous_parameters),
            DUAL_PERIOD_CONTEXT,
        )


COUNTERPART_REF_AREA_SPEC = KeyConceptSpec(
    dimension_id="COUNTERPART_REF_AREA",
    concept="REF_AREA",
    dtype="str",
    suggested_param_name="counterpart_ref_area",
)

REF_AREA_SPEC = KeyConceptSpec(
    dimension_id="REF_AREA",
    concept="REF_AREA",
    dtype="str",
    suggested_param_name="ref_area",
)


def test_validate_member_sweep_rejects_invented_counterpart_parameter() -> None:
    """Contract A rejects parameters beyond the member cells' varying keys."""
    ctx = replace(
        CLUSTER_CONTEXT,
        key_vocabulary=KEY_VOCABULARY + (COUNTERPART_REF_AREA_SPEC,),
    )
    invented_parameters = CLUSTER_PARAMETERS + (
        HelperParameter(
            name="counterpart_ref_area",
            dimension_id="COUNTERPART_REF_AREA",
            dtype="str",
        ),
    )
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(
            ValueError, match="parameters must match varying binding key dimensions"
        ):
            validate_cluster_refactor_response(
                ctx,
                _cluster_response(parameters=invented_parameters),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )


TRADE_BALANCE_CLUSTER = FormulaCluster(
    cluster_id=7,
    members=("Engine!B5", "Engine!C5", "Engine!D5"),
    canonical_template="=Inputs!B10-Inputs!C10",
    row=5,
)

TRADE_BALANCE_FORMULAS = {
    "Engine!B5": "=Inputs!B10-Inputs!C10",
    "Engine!C5": "=Inputs!B11-Inputs!C11",
    "Engine!D5": "=Inputs!B12-Inputs!C12",
}

TRADE_BALANCE_INTERNALS = (
    RUNTIME_IMPORT
    + """
# --- Formula cell functions ---

def cell_engine_b5(ctx):
    return xl_cell(ctx, 'Inputs!B10') - xl_cell(ctx, 'Inputs!C10')

def cell_engine_c5(ctx):
    return xl_cell(ctx, 'Inputs!B11') - xl_cell(ctx, 'Inputs!C11')

def cell_engine_d5(ctx):
    return xl_cell(ctx, 'Inputs!B12') - xl_cell(ctx, 'Inputs!C12')
"""
)

VARIABLE_PAIR_OPERAND_KEYS: dict[str, dict[str, BindingKeyValue]] = {
    "Inputs!B10": {"REF_AREA": "US", "TIME_PERIOD": 1},
    "Inputs!C10": {"REF_AREA": "CN", "TIME_PERIOD": 1},
    "Inputs!B11": {"REF_AREA": "DE", "TIME_PERIOD": 1},
    "Inputs!C11": {"REF_AREA": "FR", "TIME_PERIOD": 1},
    "Inputs!B12": {"REF_AREA": "JP", "TIME_PERIOD": 1},
    "Inputs!C12": {"REF_AREA": "KR", "TIME_PERIOD": 1},
}


class _ClusterProjectionStub:
    def __init__(
        self,
        formulas: dict[str, str],
        dependencies: dict[str, tuple[str, ...]],
    ) -> None:
        self._formulas = formulas
        self._dependencies = dependencies

    def get_node(self, address: str) -> _ProjectionNode | None:
        formula = self._formulas.get(address)
        if formula is None:
            return None
        return _ProjectionNode(normalized_formula=formula)

    def get_dependencies(self, address: str) -> tuple[str, ...]:
        return self._dependencies.get(address, ())


def _trade_balance_projection() -> ProjectionResult:
    dependencies = {
        "Engine!B5": ("Inputs!B10", "Inputs!C10"),
        "Engine!C5": ("Inputs!B11", "Inputs!C11"),
        "Engine!D5": ("Inputs!B12", "Inputs!C12"),
    }
    return cast(
        ProjectionResult,
        _ClusterProjectionStub(TRADE_BALANCE_FORMULAS, dependencies),
    )


def _write_trade_balance_internals(tmp_path: Path) -> Path:
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(TRADE_BALANCE_INTERNALS, encoding="utf-8")
    return internals_path


def _guard_internals_path_reads(
    monkeypatch: pytest.MonkeyPatch,
    internals_path: Path,
) -> None:
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def guarded_read_text(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self == internals_path:
            raise AssertionError(
                "internals_path.read_text should not run when index is shared"
            )
        return original_read_text(
            self,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def guarded_read_bytes(self: Path) -> bytes:
        if self == internals_path:
            raise AssertionError(
                "internals_path.read_bytes should not run when index is shared"
            )
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)


@contextmanager
def _block_internals_index_from_source() -> Iterator[None]:
    with patch.object(
        InternalsSourceIndex,
        "from_source",
        side_effect=AssertionError(
            "InternalsSourceIndex.from_source should not run when index is shared"
        ),
    ):
        yield


def test_build_cluster_refactor_context_skips_unroutable_operand_variation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without counterpart dimension ids the cluster keeps today's skip."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        **VARIABLE_PAIR_OPERAND_KEYS,
        "Engine!B5": {"REF_AREA": "US"},
        "Engine!C5": {"REF_AREA": "DE"},
        "Engine!D5": {"REF_AREA": "JP"},
    }
    ctx = build_cluster_refactor_context(
        _trade_balance_projection(),
        TRADE_BALANCE_CLUSTER,
        _write_trade_balance_internals(tmp_path),
        bound_address_keys=bound_address_keys,
        key_vocabulary=(KEY_VOCABULARY[0], REF_AREA_SPEC),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
    )
    assert ctx is None


def test_build_cluster_refactor_context_selects_dimension_aware_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct dimension ids on the member cells unlock Contract B."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        **VARIABLE_PAIR_OPERAND_KEYS,
        "Engine!B5": {"REF_AREA": "US", "COUNTERPART_REF_AREA": "CN"},
        "Engine!C5": {"REF_AREA": "DE", "COUNTERPART_REF_AREA": "FR"},
        "Engine!D5": {"REF_AREA": "JP", "COUNTERPART_REF_AREA": "KR"},
    }
    ctx = build_cluster_refactor_context(
        _trade_balance_projection(),
        TRADE_BALANCE_CLUSTER,
        _write_trade_balance_internals(tmp_path),
        bound_address_keys=bound_address_keys,
        key_vocabulary=(KEY_VOCABULARY[0], REF_AREA_SPEC, COUNTERPART_REF_AREA_SPEC),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
    )
    assert ctx is not None
    assert ctx.contract == "dimension_aware"
    assert ctx.expected_member_keys["Engine!B5"] == {
        "REF_AREA": "US",
        "COUNTERPART_REF_AREA": "CN",
    }


def test_build_cluster_refactor_context_defaults_to_member_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain sweep cluster keeps Contract A."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        "Inputs!B10": {"TIME_PERIOD": 1},
        "Inputs!C10": {"TIME_PERIOD": 1},
        "Inputs!B11": {"TIME_PERIOD": 2},
        "Inputs!C11": {"TIME_PERIOD": 2},
        "Inputs!B12": {"TIME_PERIOD": 3},
        "Inputs!C12": {"TIME_PERIOD": 3},
        "Engine!B5": {"TIME_PERIOD": 1},
        "Engine!C5": {"TIME_PERIOD": 2},
        "Engine!D5": {"TIME_PERIOD": 3},
    }
    ctx = build_cluster_refactor_context(
        _trade_balance_projection(),
        TRADE_BALANCE_CLUSTER,
        _write_trade_balance_internals(tmp_path),
        bound_address_keys=bound_address_keys,
        key_vocabulary=(KEY_VOCABULARY[0],),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
    )
    assert ctx is not None
    assert ctx.contract == "member_sweep"


def test_extract_function_source_matches_index_slice() -> None:
    source = TRADE_BALANCE_INTERNALS
    index = InternalsSourceIndex.from_source(source)
    for name in ("cell_engine_b5", "cell_engine_c5", "cell_engine_d5"):
        assert index.function_source(name) == extract_function_source(source, name)


def test_build_cluster_context_parses_internals_once_with_shared_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    internals_path = _write_trade_balance_internals(tmp_path)
    index = InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        "Inputs!B10": {"TIME_PERIOD": 1},
        "Inputs!C10": {"TIME_PERIOD": 1},
        "Inputs!B11": {"TIME_PERIOD": 2},
        "Inputs!C11": {"TIME_PERIOD": 2},
        "Inputs!B12": {"TIME_PERIOD": 3},
        "Inputs!C12": {"TIME_PERIOD": 3},
        "Engine!B5": {"TIME_PERIOD": 1},
        "Engine!C5": {"TIME_PERIOD": 2},
        "Engine!D5": {"TIME_PERIOD": 3},
    }

    with patch(
        "src.internals_refactor.ast.parse",
        side_effect=AssertionError("ast.parse should not run when index is shared"),
    ):
        ctx = build_cluster_refactor_context(
            _trade_balance_projection(),
            TRADE_BALANCE_CLUSTER,
            internals_path,
            bound_address_keys=bound_address_keys,
            key_vocabulary=(KEY_VOCABULARY[0],),
            workbook_path=tmp_path / "workbook.xlsx",
            bindings_path=tmp_path / "bindings",
            internals_index=index,
        )
    assert ctx is not None
    assert ctx.contract == "member_sweep"
    assert {member.function_name for member in ctx.members} == {
        "cell_engine_b5",
        "cell_engine_c5",
        "cell_engine_d5",
    }


def test_build_singleton_prompt_context_uses_shared_index_without_rereads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    internals_path = _write_trade_balance_internals(tmp_path)
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    cluster = FormulaCluster(
        cluster_id=99,
        members=("Engine!B5",),
        canonical_template="=Inputs!B10-Inputs!C10",
        row=5,
    )
    index = InternalsSourceIndex.from_source(TRADE_BALANCE_INTERNALS)
    ctx = build_singleton_refactor_context(
        _trade_balance_projection(),
        cluster,
        internals_path,
        internals_index=index,
    )
    assert ctx is not None

    _guard_internals_path_reads(monkeypatch, internals_path)
    with _block_internals_index_from_source():
        dump = build_singleton_refactor_prompt_context(
            ctx,
            internals_path=internals_path,
            runtime_path=runtime_path,
            internals_index=index,
        )
    assert "cell_engine_b5" in dump


def test_build_cluster_prompt_context_uses_shared_index_without_rereads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.internals_refactor as module

    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ALLOWED_RUNTIME_SYMBOLS,
    )
    internals_path = _write_trade_balance_internals(tmp_path)
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] = {
        "Inputs!B10": {"TIME_PERIOD": 1},
        "Inputs!C10": {"TIME_PERIOD": 1},
        "Inputs!B11": {"TIME_PERIOD": 2},
        "Inputs!C11": {"TIME_PERIOD": 2},
        "Inputs!B12": {"TIME_PERIOD": 3},
        "Inputs!C12": {"TIME_PERIOD": 3},
        "Engine!B5": {"TIME_PERIOD": 1},
        "Engine!C5": {"TIME_PERIOD": 2},
        "Engine!D5": {"TIME_PERIOD": 3},
    }
    index = InternalsSourceIndex.from_source(TRADE_BALANCE_INTERNALS)
    ctx = build_cluster_refactor_context(
        _trade_balance_projection(),
        TRADE_BALANCE_CLUSTER,
        internals_path,
        bound_address_keys=bound_address_keys,
        key_vocabulary=(KEY_VOCABULARY[0],),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        internals_index=index,
    )
    assert ctx is not None

    extract_calls: list[str] = []

    def tracking_extract(source: str, function_name: str) -> str:
        extract_calls.append(function_name)
        return extract_function_source(source, function_name)

    _guard_internals_path_reads(monkeypatch, internals_path)
    monkeypatch.setattr(module, "extract_function_source", tracking_extract)
    with _block_internals_index_from_source():
        dump = build_cluster_refactor_prompt_context(
            ctx,
            internals_path=internals_path,
            runtime_path=runtime_path,
            internals_index=index,
        )
    assert extract_calls == []
    assert "cell_engine_b5" in dump
    assert "cell_engine_c5" in dump
    assert "cell_engine_d5" in dump


def test_llm_refactor_singleton_uses_shared_index_without_rereads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    index = InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))
    ctx = _singleton_refactor_test_context(tmp_path)

    def fake_generate_validated_json(
        **kwargs: object,
    ) -> tuple[SingletonRefactorLLMResponse, str]:
        post_validate = cast(
            Callable[[SingletonRefactorLLMResponse], SingletonRefactorLLMResponse],
            kwargs["post_validate"],
        )
        validated = post_validate(SINGLETON_LLM_RESPONSE)
        return validated, SINGLETON_LLM_RESPONSE.model_dump_json()

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))
    _guard_internals_path_reads(monkeypatch, internals_path)
    with _block_internals_index_from_source():
        response = llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            internals_index=index,
        )
    assert response.symbol_name == "projected_debt_to_gdp"


def test_llm_refactor_cluster_uses_shared_index_without_rereads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(PRISTINE_CLUSTER, encoding="utf-8")
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    index = InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))
    llm_response = ClusterRefactorLLMResponse(
        symbol_signature=(
            "def indicator_change_from_reference(ctx: EvalContext, "
            "projection_period: int, reference_period: int) -> float:"
        ),
        symbol_docstring=DUAL_PERIOD_DOCSTRING,
        symbol_body=(
            "column_by_period = {0: 'B', 1: 'C', 2: 'D'}\n"
            "current_value = xl_cell(ctx, f'Inputs!{column_by_period[projection_period]}1')\n"
            "reference_value = xl_cell(ctx, f'Inputs!{column_by_period[reference_period]}1')\n"
            "return current_value - reference_value"
        ),
        parameters=DUAL_PERIOD_PARAMETERS,
        member_keys=DUAL_PERIOD_MEMBER_KEYS,
        error=None,
        error_reason=None,
    )

    def fake_generate_validated_json(
        **kwargs: object,
    ) -> tuple[ClusterRefactorLLMResponse, str]:
        post_validate = cast(
            Callable[[ClusterRefactorLLMResponse], ClusterRefactorLLMResponse],
            kwargs["post_validate"],
        )
        validated = post_validate(llm_response)
        return validated, llm_response.model_dump_json()

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))
    monkeypatch.setattr(
        module,
        "_resolved_projection_layout",
        lambda layout=None: DUAL_PERIOD_LAYOUT,
    )
    _guard_internals_path_reads(monkeypatch, internals_path)
    with _block_internals_index_from_source():
        response = llm_refactor_cluster(
            DUAL_PERIOD_CONTEXT,
            internals_path=internals_path,
            internals_index=index,
        )
    assert response.helper_name == "indicator_change_from_reference"


def test_refactor_internals_singleton_forwards_shared_index_to_llm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    index = InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))
    ctx = _singleton_refactor_test_context(tmp_path)
    seen: dict[str, object] = {}

    def fake_llm_refactor_singleton(
        _ctx: SingletonRefactorContext,
        *,
        internals_path: Path,
        internals_index: InternalsSourceIndex | None = None,
        **kwargs: object,
    ) -> SingletonRefactorResponse:
        seen["internals_index"] = internals_index
        seen["kwargs"] = kwargs
        return prepare_singleton_refactor_response(
            SINGLETON_LLM_RESPONSE,
            _ctx,
            runtime_source="",
            internals_source=index.source,
        )

    monkeypatch.setattr(module, "llm_refactor_singleton", fake_llm_refactor_singleton)
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        module,
        "apply_singleton_refactor_plan",
        lambda source, _response, _ctx: (source, 0),
    )
    monkeypatch.setattr(module, "validate_refactored_internals", lambda _source: None)

    _guard_internals_path_reads(monkeypatch, internals_path)
    refactor_internals_singleton(
        ctx,
        internals_path=internals_path,
        dry_run=True,
        internals_index=index,
    )
    assert seen["internals_index"] is index


def test_refactor_schedule_rebuilds_index_only_after_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    version_sources = [
        "def cell_engine_b2(ctx):\n    return 1.0\n",
        "def cell_engine_b2(ctx):\n    return 2.0\n",
        "def cell_engine_b2(ctx):\n    return 3.0\n",
        "def cell_engine_b2(ctx):\n    return 4.0\n",
        "def cell_engine_b2(ctx):\n    return 5.0\n",
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")

    from_source_sources: list[str] = []
    real_from_source = module.InternalsSourceIndex.from_source
    indices_seen: list[module.InternalsSourceIndex] = []
    apply_count = {"n": 0}

    def tracking_from_source(source: str) -> module.InternalsSourceIndex:
        from_source_sources.append(source)
        return real_from_source(source)

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **kwargs: object,
    ) -> SimpleNamespace:
        index = kwargs.get("internals_index")
        assert isinstance(index, module.InternalsSourceIndex)
        indices_seen.append(index)
        return SimpleNamespace(address=cluster.members[0])

    def fake_singleton(
        ctx: SimpleNamespace,
        *,
        internals_path: Path,
        dry_run: bool = False,
        pristine_source: str | None = None,
        input_vectors: object | None = None,
        source_graph: object | None = None,
        diagnostic_target: str | None = None,
        internals_index: object | None = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        assert isinstance(internals_index, module.InternalsSourceIndex)
        assert internals_index is indices_seen[-1]
        _ = kwargs
        apply_count["n"] += 1
        updated = version_sources[apply_count["n"]]
        if not dry_run:
            internals_path.write_text(updated, encoding="utf-8")
        return SimpleNamespace(source=updated)

    monkeypatch.setattr(
        module.InternalsSourceIndex,
        "from_source",
        staticmethod(tracking_from_source),
    )
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(module, "refactor_internals_singleton", fake_singleton)
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )

    refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
    )

    # One initial index + one rebuild per successful singleton apply (4 units).
    assert len(from_source_sources) == 1 + apply_count["n"]
    assert apply_count["n"] == 4
    assert len(indices_seen) == 4
    assert indices_seen[0] is not indices_seen[1]
    assert from_source_sources[0] == version_sources[0]
    assert from_source_sources[1] == version_sources[1]


def test_llm_refactor_cluster_uses_dimension_aware_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(PRISTINE_CLUSTER, encoding="utf-8")
    recorded: dict[str, object] = {}
    llm_response = ClusterRefactorLLMResponse(
        symbol_signature=(
            "def indicator_change_from_reference(ctx: EvalContext, "
            "projection_period: int, reference_period: int) -> float:"
        ),
        symbol_docstring=DUAL_PERIOD_DOCSTRING,
        symbol_body=(
            "column_by_period = {0: 'B', 1: 'C', 2: 'D'}\n"
            "current_value = xl_cell(ctx, f'Inputs!{column_by_period[projection_period]}1')\n"
            "reference_value = xl_cell(ctx, f'Inputs!{column_by_period[reference_period]}1')\n"
            "return current_value - reference_value"
        ),
        parameters=DUAL_PERIOD_PARAMETERS,
        member_keys=DUAL_PERIOD_MEMBER_KEYS,
        error=None,
        error_reason=None,
    )

    def fake_generate_validated_json(
        **kwargs: object,
    ) -> tuple[ClusterRefactorLLMResponse, str]:
        recorded["user_prompt"] = kwargs["user_prompt"]
        post_validate = cast(
            Callable[[ClusterRefactorLLMResponse], ClusterRefactorLLMResponse],
            kwargs["post_validate"],
        )
        validated = post_validate(llm_response)
        return validated, llm_response.model_dump_json()

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", lambda: {})
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "_refactor_provider_key_present", lambda: True)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))
    monkeypatch.setattr(
        module,
        "build_cluster_refactor_prompt_context",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        module,
        "_resolved_projection_layout",
        lambda layout=None: DUAL_PERIOD_LAYOUT,
    )

    response = llm_refactor_cluster(DUAL_PERIOD_CONTEXT, internals_path=internals_path)

    assert response.helper_name == "indicator_change_from_reference"
    user_prompt = cast(str, recorded["user_prompt"])
    assert user_prompt.startswith(
        load_cluster_refactor_prompt_fixed_portion("dimension_aware").strip()
    )
    assert "Never collapse two dimension ids" in user_prompt


def test_refactor_internals_all_clusters_consumes_refactor_schedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_b2(ctx):\n    return 1.0\n", encoding="utf-8"
    )

    scheduled_members: list[tuple[str, ...]] = []
    diagnostic_targets: list[str] = []

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(address=cluster.members[0])

    def fake_singleton(
        ctx: SimpleNamespace,
        *,
        internals_path: Path,
        dry_run: bool = False,
        pristine_source: str | None = None,
        input_vectors: object | None = None,
        source_graph: object | None = None,
        diagnostic_target: str | None = None,
        internals_index: object | None = None,
        **kwargs: object,
    ) -> object:
        _ = internals_index, kwargs
        scheduled_members.append((ctx.address,))
        if diagnostic_target is not None:
            diagnostic_targets.append(diagnostic_target)
        return object()

    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(module, "refactor_internals_singleton", fake_singleton)
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=True,
        parity_gate=False,
    )

    assert scheduled_members == [
        ("Engine!B2",),
        ("Engine!C2",),
        ("Engine!B3",),
        ("Engine!C3",),
    ]
    assert diagnostic_targets == [
        "cluster_0_g0",
        "cluster_1_g1",
        "cluster_0_g2",
        "cluster_1_g3",
    ]
