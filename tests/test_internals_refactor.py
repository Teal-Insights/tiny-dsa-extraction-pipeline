"""Unit tests for internals refactor validation, collapse, and prompt contracts."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
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
    FORMULA_SECTION_MARKER,
    PROJECTION_ALIAS_SECTION_MARKER,
    RESOLVER_SECTION_MARKER,
    UNREFACTORED_CELLS_SECTION_MARKER,
    address_to_function_name,
    apply_cluster_collapse,
    apply_phase_c,
    apply_singleton_refactor_plan,
    build_cluster_refactor_context,
    insert_helper_source,
    rehome_unrefactored_cell_functions,
    validate_refactored_internals,
    build_cluster_refactor_prompt_context,
    build_singleton_refactor_context,
    build_singleton_refactor_prompt_context,
    collapse_bindings_for_response,
    complete_cluster_member_keys_from_expected,
    extract_function_source,
    helper_static_key_domain,
    llm_refactor_cluster,
    llm_refactor_singleton,
    load_cluster_refactor_prompt_fixed_portion,
    prepare_cluster_refactor_response,
    prepare_singleton_refactor_response,
    prompt_payload,
    raise_if_llm_declared_error,
    refactor_cache_key,
    refactor_internals_all_clusters,
    refactor_internals_singleton,
    resolve_semantic_dependencies,
    sample_indices_for_prompt,
    singleton_prompt_payload,
    substitute_collapse_bindings,
    validate_allowed_global_references,
    validate_cluster_refactor_response,
    validate_no_xl_index_ref_of_xl_range,
    validate_parameter_names_match_vocabulary,
    validate_semantic_local_names,
    validate_singleton_refactor_response,
    write_refactor_failure_diagnostic,
    _attempt_artifacts_from_validated_json_failure,
    _dump_validated_json_failure,
    _prepare_cluster_refactor_response,
    _prompt_for_refactor,
    _prompt_for_singleton_refactor,
    _single_function_def,
)
from src.llm_json import (
    DEFAULT_MAX_ATTEMPTS,
    ValidatedJsonFailure,
    ValidationAttemptRecord,
)
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
    CellValue,
    EvalContext,
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
    expected_helper_name="combined_input_passthrough",
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


def test_write_refactor_failure_diagnostic_persists_mechanical_context(
    tmp_path: Path,
) -> None:
    dump_dir = write_refactor_failure_diagnostic(
        kind="cluster",
        target="cluster_42_g0",
        error=ValueError("could not infer return type from mechanical member source"),
        dump_dir=tmp_path,
        context={
            "cluster_id": 42,
            "members": [
                {
                    "index": 0,
                    "address": "Engine!C10",
                    "python_source": "def cell_engine_c10(ctx):\n    return mystery(ctx)\n",
                }
            ],
            "mechanical_draft": {"body": "return mystery(ctx)", "group_count": 1},
        },
        source="mechanical",
    )

    manifest = json.loads((dump_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "mechanical"
    assert manifest["target"] == "cluster_42_g0"
    assert dump_dir.name == "cluster_42_g0"
    context = json.loads((dump_dir / "context.json").read_text(encoding="utf-8"))
    assert context["members"][0]["address"] == "Engine!C10"
    assert context["mechanical_draft"]["body"] == "return mystery(ctx)"
    assert (
        (dump_dir / "error.txt")
        .read_text(encoding="utf-8")
        .startswith("could not infer return type")
    )


def test_pass1_mechanical_cluster_failure_writes_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass-1 mechanical assemble/validate failures must dump like LLM failures."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import FormulaCluster
    from src.mechanical_body import MechanicalBodyDraft
    from src.refactor_return_types import RefactorReturnTypeInferenceError

    cluster = FormulaCluster(
        cluster_id=7,
        members=("Engine!C4", "Engine!D4"),
        canonical_template="=Inputs!{col}1",
        row=4,
    )

    class _Projection:
        def get_dependencies(self, _address: str) -> tuple[str, ...]:
            return ()

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c4(ctx):\n    return 1.0\n"
        "def cell_engine_d4(ctx):\n    return 2.0\n",
        encoding="utf-8",
    )
    dump_root = tmp_path / "refactor_failures"
    draft = MechanicalBodyDraft(
        body="return mystery(ctx)",
        renameable_locals=(),
        lookup_table_names=(),
        group_count=1,
    )
    cluster_ctx = SimpleNamespace(
        cluster_id=7,
        canonical_template="=Inputs!{col}1",
        members=(
            SimpleNamespace(
                address="Engine!C4",
                function_name="cell_engine_c4",
                python_source="def cell_engine_c4(ctx):\n    return mystery(ctx)\n",
            ),
            SimpleNamespace(
                address="Engine!D4",
                function_name="cell_engine_d4",
                python_source="def cell_engine_d4(ctx):\n    return mystery(ctx)\n",
            ),
        ),
        naming_hints={},
        allowed_runtime_symbols=(),
        expected_helper_name="combined_mystery",
        contract="member_sweep",
    )

    def boom_build(*_args: object, **_kwargs: object) -> object:
        raise RefactorReturnTypeInferenceError(
            "could not infer return type from mechanical member source at index 0; "
            "callee annotations and literals were insufficient"
        )

    monkeypatch.setattr(module, "REFACTOR_FAILURE_DUMP_DIR", dump_root)
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: cluster_ctx
    )
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: draft)
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(module, "build_mechanical_cluster_response", boom_build)
    monkeypatch.setattr(
        module,
        "compute_refactor_schedule",
        lambda *_a, **_k: (
            SimpleNamespace(
                parent_cluster_id=7,
                refactor_group_id=0,
                members=cluster.members,
                as_formula_cluster=lambda: cluster,
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "allocate_schedule_helper_names",
        lambda *_a, **_k: ("combined_mystery",),
    )

    with pytest.raises(RefactorReturnTypeInferenceError, match="index 0"):
        module.refactor_internals_all_clusters(
            cast(ProjectionResult, _Projection()),
            (cluster,),
            internals_path=internals_path,
            bindings_path=tmp_path / "bindings",
            workbook_path=tmp_path / "workbook.xlsx",
            dry_run=False,
            parity_gate=False,
            address_to_series_id={
                "Engine!C4": "mystery_series",
                "Engine!D4": "mystery_series",
            },
        )

    failure_dirs = list(dump_root.iterdir())
    assert len(failure_dirs) == 1
    dump_dir = failure_dirs[0]
    manifest = json.loads((dump_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "mechanical"
    assert manifest["kind"] == "cluster"
    assert "index 0" in manifest["error_message"]
    context = json.loads((dump_dir / "context.json").read_text(encoding="utf-8"))
    assert context["cluster_id"] == 7
    assert context["members"][0]["address"] == "Engine!C4"
    assert context["mechanical_draft"]["body"] == "return mystery(ctx)"


def test_pass1_refreshes_callee_hints_after_mechanical_cluster_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Downstream units must see upstream helper return annotations after flush."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import FormulaCluster
    from src.mechanical_body import MechanicalBodyDraft

    upstream = FormulaCluster(
        cluster_id=1,
        members=("Engine!C12", "Engine!D12"),
        canonical_template="=1",
        row=12,
    )
    downstream = FormulaCluster(
        cluster_id=2,
        members=("Engine!C4", "Engine!D4"),
        canonical_template="=IF(...)",
        row=4,
    )

    class _Projection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            if address in {"Engine!C4", "Engine!D4"}:
                return ("Engine!C12", "Engine!D12")
            return ()

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def cell_engine_c12(ctx):
    return 1.0

def cell_engine_d12(ctx):
    return 2.0

def cell_engine_c4(ctx):
    return population_medium(ctx, time_period=1)

def cell_engine_d4(ctx):
    return population_medium(ctx, time_period=2)

"""
        + RESOLVER_SECTION,
        encoding="utf-8",
    )
    draft = MechanicalBodyDraft(
        body="return 1.0",
        renameable_locals=(),
        lookup_table_names=(),
        group_count=1,
    )
    hints_by_helper: dict[str, dict[str, str]] = {}

    def fake_build_cluster(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        helper = (
            "population_medium"
            if cluster.cluster_id == 1
            else "demography_total_population"
        )
        return SimpleNamespace(
            cluster_id=cluster.cluster_id,
            canonical_template=cluster.canonical_template,
            members=tuple(
                SimpleNamespace(
                    address=address,
                    function_name=address_to_function_name(address),
                    normalized_formula="=1",
                    python_source=(
                        f"def {address_to_function_name(address)}(ctx):\n"
                        f"    return 1.0\n"
                    ),
                )
                for address in cluster.members
            ),
            naming_hints={},
            allowed_runtime_symbols=(),
            expected_helper_name=helper,
            contract="member_sweep",
        )

    def tracking_mechanical_response(
        ctx: SimpleNamespace, _draft: object, **kwargs: object
    ) -> ClusterRefactorResponse:
        hints = kwargs.get("callee_hints")
        captured: dict[str, str] = {}
        if isinstance(hints, dict):
            for key, value in hints.items():
                if isinstance(key, str) and isinstance(value, str):
                    captured[key] = value
        hints_by_helper[ctx.expected_helper_name] = captured
        return ClusterRefactorResponse(
            helper_name=ctx.expected_helper_name,
            helper_docstring=CLUSTER_DOCSTRING,
            parameters=CLUSTER_PARAMETERS,
            helper_source=(
                f"def {ctx.expected_helper_name}"
                "(ctx, time_period: int) -> float:\n"
                f'    """{CLUSTER_DOCSTRING}"""\n'
                "    return float(time_period)\n"
            ),
            member_keys=tuple(
                MemberKeys(
                    address=member.address,
                    function_name=member.function_name,
                    keys=(
                        MemberKeyEntry(
                            dimension_id="TIME_PERIOD",
                            value=(1 if member.address.endswith(("C12", "C4")) else 2),
                        ),
                    ),
                )
                for member in ctx.members
            ),
        )

    monkeypatch.setattr(module, "build_cluster_refactor_context", fake_build_cluster)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: draft)
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(
        module, "build_mechanical_cluster_response", tracking_mechanical_response
    )
    monkeypatch.setattr(
        module, "validate_cluster_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        module,
        "compute_refactor_schedule",
        lambda *_a, **_k: (
            SimpleNamespace(
                parent_cluster_id=1,
                refactor_group_id=0,
                members=upstream.members,
                as_formula_cluster=lambda: upstream,
            ),
            SimpleNamespace(
                parent_cluster_id=2,
                refactor_group_id=0,
                members=downstream.members,
                as_formula_cluster=lambda: downstream,
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "allocate_schedule_helper_names",
        lambda *_a, **_k: ("population_medium", "demography_total_population"),
    )
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(
        module,
        "_refresh_mechanical_cluster_results",
        lambda results, **_k: list(results),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, _Projection()),
        (upstream, downstream),
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!C12": "population_medium",
            "Engine!D12": "population_medium",
            "Engine!C4": "demography_total_population",
            "Engine!D4": "demography_total_population",
        },
    )

    assert "population_medium" not in hints_by_helper["population_medium"]
    assert hints_by_helper["demography_total_population"]["population_medium"] == (
        "float"
    )


def test_pass1_mechanical_singleton_failure_writes_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass-1 singleton mechanical failures must also dump diagnostics."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import FormulaCluster
    from src.mechanical_body import MechanicalBodyDraft
    from src.refactor_return_types import RefactorReturnTypeInferenceError

    cluster = FormulaCluster(
        cluster_id=3,
        members=("Engine!C9",),
        canonical_template="=1",
        row=9,
    )

    class _Projection:
        def get_dependencies(self, _address: str) -> tuple[str, ...]:
            return ()

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c9(ctx):\n    return mystery(ctx)\n",
        encoding="utf-8",
    )
    dump_root = tmp_path / "refactor_failures"
    draft = MechanicalBodyDraft(
        body="return mystery(ctx)",
        renameable_locals=(),
        lookup_table_names=(),
        group_count=1,
    )
    singleton_ctx = SimpleNamespace(
        address="Engine!C9",
        function_name="cell_engine_c9",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_engine_c9(ctx):\n    return mystery(ctx)\n",
        naming_hints={},
        allowed_runtime_symbols=(),
        expected_helper_name="mystery_singleton",
    )

    def boom_build(*_args: object, **_kwargs: object) -> object:
        raise RefactorReturnTypeInferenceError(
            "could not infer return type from mechanical member source at index 0; "
            "callee annotations and literals were insufficient"
        )

    monkeypatch.setattr(module, "REFACTOR_FAILURE_DUMP_DIR", dump_root)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: singleton_ctx
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: draft)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(module, "build_mechanical_singleton_response", boom_build)
    monkeypatch.setattr(
        module,
        "compute_refactor_schedule",
        lambda *_a, **_k: (
            SimpleNamespace(
                parent_cluster_id=3,
                refactor_group_id=0,
                members=cluster.members,
                as_formula_cluster=lambda: cluster,
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "allocate_schedule_helper_names",
        lambda *_a, **_k: ("mystery_singleton",),
    )

    with pytest.raises(RefactorReturnTypeInferenceError, match="index 0"):
        module.refactor_internals_all_clusters(
            cast(ProjectionResult, _Projection()),
            (cluster,),
            internals_path=internals_path,
            bindings_path=tmp_path / "bindings",
            workbook_path=tmp_path / "workbook.xlsx",
            dry_run=False,
            parity_gate=False,
            address_to_series_id={"Engine!C9": "mystery_series"},
        )

    failure_dirs = list(dump_root.iterdir())
    assert len(failure_dirs) == 1
    dump_dir = failure_dirs[0]
    manifest = json.loads((dump_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "mechanical"
    assert manifest["kind"] == "singleton"
    context = json.loads((dump_dir / "context.json").read_text(encoding="utf-8"))
    assert context["address"] == "Engine!C9"
    assert context["mechanical_draft"]["body"] == "return mystery(ctx)"


def test_write_refactor_failure_diagnostic_persists_all_attempts(
    tmp_path: Path,
) -> None:
    conversation = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "initial user prompt"},
        {"role": "assistant", "content": '{"symbol_body": "return 1.0"}'},
        {"role": "user", "content": "Your previous response failed validation..."},
        {"role": "assistant", "content": '{"symbol_body": "return 2.0"}'},
        {"role": "user", "content": "Your previous response failed validation..."},
        {"role": "assistant", "content": '{"symbol_body": "return 3.0"}'},
        {"role": "user", "content": "Your previous response failed validation..."},
    ]
    attempts: list[dict[str, Any]] = [
        {
            "attempt": 1,
            "raw_content": '{"symbol_body": "return 1.0"}',
            "error": "first parity mismatch",
            "llm_response": {"symbol_body": "return 1.0"},
            "prepared_response": {"symbol_name": "helper", "symbol_body": "return 1.0"},
        },
        {
            "attempt": 2,
            "raw_content": '{"symbol_body": "return 2.0"}',
            "error": "second parity mismatch",
            "llm_response": {"symbol_body": "return 2.0"},
            "prepared_response": {"symbol_name": "helper", "symbol_body": "return 2.0"},
        },
        {
            "attempt": 3,
            "raw_content": '{"symbol_body": "return 3.0"}',
            "error": "third parity mismatch",
            "llm_response": {"symbol_body": "return 3.0"},
            "prepared_response": {"symbol_name": "helper", "symbol_body": "return 3.0"},
        },
    ]
    last_attempt = attempts[-1]

    dump_dir = write_refactor_failure_diagnostic(
        kind="singleton",
        target="Engine!C20",
        error=RuntimeError("LLM failed after 3 attempts"),
        dump_dir=tmp_path,
        user_prompt="initial user prompt",
        llm_response=last_attempt["llm_response"],
        prepared_response=last_attempt["prepared_response"],
        raw_content=str(last_attempt["raw_content"]),
        conversation=conversation,
        attempts=attempts,
        source="llm",
        model="test-model",
    )

    manifest = json.loads((dump_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["conversation"] == "conversation.json"
    assert manifest["files"]["attempts"] == "attempts"
    assert "last attempt" in manifest["compatibility_note"].lower()

    saved_conversation = json.loads(
        (dump_dir / "conversation.json").read_text(encoding="utf-8")
    )
    assert saved_conversation[1]["content"] == "initial user prompt"
    assert saved_conversation[2]["content"] == '{"symbol_body": "return 1.0"}'
    assert saved_conversation[4]["content"] == '{"symbol_body": "return 2.0"}'
    assert saved_conversation[6]["content"] == '{"symbol_body": "return 3.0"}'

    for index, attempt in enumerate(attempts, start=1):
        attempt_dir = dump_dir / "attempts" / f"{index:02d}"
        assert (attempt_dir / "error.txt").read_text(encoding="utf-8") == (
            f"{attempt['error']}\n"
        )
        raw = json.loads((attempt_dir / "raw_content.json").read_text(encoding="utf-8"))
        assert raw["content"] == attempt["raw_content"]
        llm_response = json.loads(
            (attempt_dir / "llm_response.json").read_text(encoding="utf-8")
        )
        assert llm_response == attempt["llm_response"]
        prepared = json.loads(
            (attempt_dir / "prepared_response.json").read_text(encoding="utf-8")
        )
        assert prepared == attempt["prepared_response"]

    # Legacy top-level fields remain the final attempt.
    assert (
        json.loads((dump_dir / "llm_response.json").read_text(encoding="utf-8"))
        == attempts[-1]["llm_response"]
    )
    assert json.loads((dump_dir / "raw_content.json").read_text(encoding="utf-8")) == {
        "content": attempts[-1]["raw_content"]
    }


def test_attempt_artifact_merge_does_not_attach_to_unrelated_raw_json() -> None:
    artifact = {
        "llm_response": {
            "symbol_docstring": "Doc",
            "symbol_body": "return 2.0",
            "error": False,
            "error_reason": None,
        },
        "prepared_response": {"symbol_source": "return 2.0"},
    }
    failure = ValidatedJsonFailure(
        "exhausted",
        messages=[],
        attempts=[
            ValidationAttemptRecord(1, '{"totally": "wrong"}', "schema error"),
            ValidationAttemptRecord(
                2,
                (
                    '{"symbol_docstring": "Doc", "symbol_body": "return 2.0", '
                    '"error": false, "error_reason": null}'
                ),
                "parity mismatch",
            ),
            ValidationAttemptRecord(
                3, '{"symbol_body": "return 2.0"}', "missing fields"
            ),
        ],
        last_error=None,
    )

    merged = _attempt_artifacts_from_validated_json_failure(
        failure, local_artifacts=[artifact]
    )

    assert "llm_response" not in merged[0]
    assert "prepared_response" not in merged[0]
    assert merged[1]["llm_response"] == artifact["llm_response"]
    assert merged[1]["prepared_response"] == artifact["prepared_response"]
    assert "llm_response" not in merged[2]
    assert "prepared_response" not in merged[2]


def test_attempt_artifact_merge_allows_omitted_null_fields_in_raw_json() -> None:
    artifact = {
        "llm_response": {
            "symbol_docstring": "Doc",
            "symbol_body": "return 1.0",
            "error": False,
            "error_reason": None,
        },
        "prepared_response": {"symbol_source": "return 1.0"},
    }
    failure = ValidatedJsonFailure(
        "exhausted",
        messages=[],
        attempts=[
            ValidationAttemptRecord(
                1,
                '{"symbol_docstring": "Doc", "symbol_body": "return 1.0", "error": false}',
                "parity mismatch",
            ),
        ],
        last_error=None,
    )

    merged = _attempt_artifacts_from_validated_json_failure(
        failure, local_artifacts=[artifact]
    )

    assert merged[0]["prepared_response"] == artifact["prepared_response"]


def test_dump_validated_json_failure_top_level_uses_merged_last_attempt(
    tmp_path: Path,
) -> None:
    local_artifacts = [
        {
            "llm_response": {"symbol_body": "return 1.0"},
            "prepared_response": {"symbol_source": "return 1.0"},
        },
    ]
    failure = ValidatedJsonFailure(
        "after 2 attempts",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ],
        attempts=[
            ValidationAttemptRecord(1, '{"symbol_body": "return 1.0"}', "parity"),
            ValidationAttemptRecord(2, '{"symbol_bodyy": "oops"}', "schema error"),
        ],
        last_error=None,
    )

    dump_dir = _dump_validated_json_failure(
        kind="singleton",
        target="Engine!C20",
        error=failure,
        user_prompt="usr",
        local_artifacts=local_artifacts,
        model="test-model",
        dump_dir=tmp_path,
    )

    assert not (dump_dir / "llm_response.json").exists()
    assert not (dump_dir / "prepared_response.json").exists()
    assert json.loads((dump_dir / "raw_content.json").read_text(encoding="utf-8")) == {
        "content": '{"symbol_bodyy": "oops"}'
    }
    assert (dump_dir / "attempts" / "01" / "llm_response.json").exists()
    assert not (dump_dir / "attempts" / "02" / "llm_response.json").exists()


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


_INDEX_REF_HINT = "xl_index_ref expects a geometry tuple"


def test_validate_no_xl_index_ref_of_xl_range_rejects_nested_call() -> None:
    source = '''def helper(ctx, time_period):
    """Doc.

Args:
    ctx: Context.
    time_period: Period.

Returns:
    Value.
"""
    return xl_index_ref(xl_range(ctx, "'Sheet'!A1:B2"), 1.0, 1.0)
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    with pytest.raises(ValueError, match=_INDEX_REF_HINT) as exc_info:
        validate_no_xl_index_ref_of_xl_range(helper_def)
    assert "xl_range" in str(exc_info.value)
    assert "do not pass xl_range" in str(exc_info.value).lower()


def test_validate_no_xl_index_ref_of_xl_range_rejects_bound_local() -> None:
    """Catch the cluster_12 pattern: bind xl_range then pass the name to xl_index_ref."""
    source = '''def helper(ctx, time_period):
    """Doc.

Args:
    ctx: Context.
    time_period: Period.

Returns:
    Value.
"""
    data_range = xl_range(ctx, f"'Sheet'!A{time_period}:B10")
    return xl_offset(ctx, xl_index_ref(data_range, 1.0, 1.0), 0.0, 0.0)
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    with pytest.raises(ValueError, match=_INDEX_REF_HINT):
        validate_no_xl_index_ref_of_xl_range(helper_def)


def test_validate_no_xl_index_ref_of_xl_range_accepts_geometry_tuple() -> None:
    source = '''def helper(ctx, time_period):
    """Doc.

Args:
    ctx: Context.
    time_period: Period.

Returns:
    Value.
"""
    start_col = {1: 3, 2: 4}[time_period]
    return xl_offset(
        ctx,
        xl_index_ref(("Sheet", 1, start_col, 10, start_col), 1.0, 1.0),
        0.0,
        0.0,
    )
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    validate_no_xl_index_ref_of_xl_range(helper_def)


def test_validate_cluster_rejects_xl_index_ref_of_xl_range() -> None:
    ctx = replace(
        CLUSTER_CONTEXT,
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS
        + ("xl_index_ref", "xl_range", "xl_offset"),
    )
    bad_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    data_range = xl_range(ctx, f"Inputs!A{{time_period}}:B10")
    return xl_offset(ctx, xl_index_ref(data_range, 1.0, 1.0), 0.0, 0.0)
'''
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(ValueError, match=_INDEX_REF_HINT) as exc_info:
            validate_cluster_refactor_response(
                ctx,
                _cluster_response(helper_source=bad_source),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )
    assert "do not pass xl_range" in str(exc_info.value).lower()


def test_validate_cluster_allows_locked_helper_name_already_in_internals() -> None:
    """Re-applying the schedule-allocated helper must not look like a foreign collision."""
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        validate_cluster_refactor_response(
            CLUSTER_CONTEXT,
            _cluster_response(),
            existing_names=frozenset(
                {
                    "cell_engine_c6",
                    "cell_engine_d6",
                    CLUSTER_CONTEXT.expected_helper_name,
                }
            ),
            internals_source=PRISTINE_CLUSTER,
        )


def test_validate_refactored_internals_rejects_duplicate_top_level_defs() -> None:
    source = """
def shocked_path_internal(ctx):
    return 1.0

def shocked_path_internal(ctx):
    return 2.0
"""
    with pytest.raises(ValueError, match="duplicate top-level function"):
        validate_refactored_internals(source)


def test_record_name_deltas_reject_duplicates() -> None:
    import src.internals_refactor as module

    names = {"cell_engine_c6", "cell_engine_d6"}
    with pytest.raises(ValueError, match="duplicate top-level function"):
        module._record_cluster_name_delta(
            names,
            helper_name="cell_engine_c6",
            removed_names=("cell_engine_d6",),
        )
    names = {"cell_engine_c6"}
    module._record_cluster_name_delta(
        names,
        helper_name="combined_helper",
        removed_names=("cell_engine_c6",),
    )
    assert names == {"combined_helper"}

    names = {"cell_engine_c9", "existing_helper"}
    with pytest.raises(ValueError, match="duplicate top-level function"):
        module._record_singleton_name_delta(
            names,
            old_name="cell_engine_c9",
            new_name="existing_helper",
        )
    names = {"cell_engine_c9"}
    module._record_singleton_name_delta(
        names,
        old_name="cell_engine_c9",
        new_name="renamed_helper",
    )
    assert names == {"renamed_helper"}


def test_insert_helper_source_rejects_existing_helper_name() -> None:
    source = (
        f"{FORMULA_SECTION_MARKER}\n\ndef shocked_path_internal(ctx):\n    return 1.0\n"
    )
    helper = "def shocked_path_internal(ctx):\n    return 2.0\n"
    with pytest.raises(ValueError, match="already exists"):
        insert_helper_source(source, helper)


def test_validate_cluster_skips_engine_column_check_without_projection_layout() -> None:
    """Bindings already triangulate members; layout mapping is optional."""
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=None,
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


# --- RED: a helper must serve every member key it claims (#139) ---------------
#
# `Baseline!D12:CP12` (labour productivity growth) interleaves two formula
# regimes — `D12:Q12` / `Y12:CP12` read `Productivity!*6`, `R12:X12` is the
# GDP/employment ratio. A refactor that claims the whole series but implements
# one regime emits a helper whose literal period tables (or key-dispatch chain)
# cannot serve the other regime's years, and the call raises `KeyError` /
# `ValueError` the first time a caller asks for one. The claimed member keys are
# already in the context, so the mismatch is provable before the helper lands.


def test_validate_cluster_rejects_helper_that_cannot_serve_a_member_key() -> None:
    helper_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    columns = {{1: 'C'}}
    column = columns[time_period]
    return xl_cell(ctx, f'Inputs!{{column}}1')
'''
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(ValueError, match="cannot serve member keys"):
            validate_cluster_refactor_response(
                CLUSTER_CONTEXT,
                _cluster_response(helper_source=helper_source),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )


def test_validate_cluster_rejects_key_dispatch_chain_missing_a_member_key() -> None:
    helper_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    if time_period == 1:
        return xl_cell(ctx, 'Inputs!C1')
    raise ValueError(time_period)
'''
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(ValueError, match="cannot serve member keys"):
            validate_cluster_refactor_response(
                CLUSTER_CONTEXT,
                _cluster_response(helper_source=helper_source),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )


def test_validate_cluster_accepts_member_key_handled_by_a_guard_branch() -> None:
    """A period special-cased before the lookup table is served, not missing."""
    helper_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    if time_period == 2:
        return xl_cell(ctx, 'Inputs!D1')
    columns = {{1: 'C'}}
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


def test_validate_allowed_global_references_treats_lambda_params_as_locals() -> None:
    """Lambda parameters must not be reported as disallowed globals (issue #149)."""
    source = f'''def safe_ratio(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    numerator = xl_cell(ctx, 'Inputs!C1')
    denominator = xl_cell(ctx, 'Inputs!C2')
    return (lambda num, den: num / den if den != 0 else xl_raise(XlError.DIV))(
        numerator, denominator
    )
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    validate_allowed_global_references(
        helper_def,
        allowed_names={"xl_cell", "xl_raise", "XlError", "ctx", "time_period"},
    )


def test_validate_allowed_global_references_treats_nested_def_bindings_as_locals() -> (
    None
):
    source = f'''def safe_ratio(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    numerator = xl_cell(ctx, 'Inputs!C1')
    denominator = xl_cell(ctx, 'Inputs!C2')

    def divide(num, den):
        return num / den if den != 0 else xl_raise(XlError.DIV)

    return divide(numerator, denominator)
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    validate_allowed_global_references(
        helper_def,
        allowed_names={"xl_cell", "xl_raise", "XlError", "ctx", "time_period"},
    )


def test_validate_allowed_global_references_treats_with_as_targets_as_locals() -> None:
    source = f'''def read_with_temp(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    with open('/dev/null') as handle:
        _ = handle.read(0)
    return xl_cell(ctx, 'Inputs!C1')
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    validate_allowed_global_references(
        helper_def,
        allowed_names={"xl_cell", "open", "ctx", "time_period"},
    )


def test_validate_allowed_global_references_still_rejects_free_names_in_lambda() -> (
    None
):
    source = f'''def safe_ratio(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return (lambda num, den: mystery_helper(num, den))(1, 2)
'''
    helper_def = _single_function_def(source)
    assert helper_def is not None
    with pytest.raises(ValueError, match="disallowed global names.*mystery_helper"):
        validate_allowed_global_references(
            helper_def,
            allowed_names={"xl_cell", "ctx", "time_period"},
        )


def test_validate_cluster_allowlist_excludes_cell_star_names() -> None:
    """``cell_*`` calls are already banned; do not dump them in allowlist errors."""
    ctx = replace(
        CLUSTER_CONTEXT,
        external_dependencies=(
            "cell_climate_database_z72",
            "cell_inputs_b6",
            "shock_active",
        ),
    )
    bad_source = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return mystery_helper(ctx)
'''
    with patch(
        "src.internals_refactor._resolved_projection_layout",
        return_value=TEST_LAYOUT,
    ):
        with pytest.raises(ValueError, match="disallowed function") as exc_info:
            validate_cluster_refactor_response(
                ctx,
                _cluster_response(helper_source=bad_source),
                existing_names=frozenset({"cell_engine_c6", "cell_engine_d6"}),
                internals_source=PRISTINE_CLUSTER,
            )
    message = str(exc_info.value)
    assert "cell_climate_database_z72" not in message
    assert "cell_inputs_b6" not in message
    assert "cell_engine_c6" not in message
    assert "cell_engine_d6" not in message
    assert "shock_active" in message
    assert "xl_cell" in message


def test_validate_singleton_allowlist_excludes_cell_star_names() -> None:
    docstring = (
        "Return a value.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Projected value.\n"
    )
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        dependency_addresses=("Climate Database!Z72",),
        external_dependencies=(
            "cell_climate_database_z72",
            "shock_active",
        ),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
        expected_helper_name="projected_debt_to_gdp",
    )
    bad_source = f'''def projected_debt_to_gdp(ctx):
    """{docstring}"""
    return mystery_helper(ctx)
'''
    with pytest.raises(ValueError, match="disallowed function") as exc_info:
        validate_singleton_refactor_response(
            ctx,
            SingletonRefactorResponse(
                symbol_name="projected_debt_to_gdp",
                symbol_docstring=docstring,
                symbol_source=bad_source,
            ),
            existing_names=frozenset({"cell_engine_c20"}),
            internals_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        )
    message = str(exc_info.value)
    assert "cell_climate_database_z72" not in message
    assert "cell_engine_c20" not in message
    assert "shock_active" in message
    assert "xl_cell" in message


def test_validate_singleton_allowlist_accepts_reader_functions() -> None:
    docstring = (
        "Return the configured shock type.\n\n"
        "Args:\n    ctx: Workbook evaluation context.\n\n"
        "Returns:\n    Shock type label.\n"
    )
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=Inputs!B1",
        normalized_formula="=Inputs!B1",
        python_source="def cell_engine_c20(ctx):\n    return xl_cell(ctx, 'Inputs!B1')\n",
        dependency_addresses=("Inputs!B1",),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS + ("read_shock_type",),
        naming_hints={},
        expected_helper_name="shock_type",
    )
    source = f'''def shock_type(ctx):
    """{docstring}"""
    return read_shock_type(ctx)
'''
    validate_singleton_refactor_response(
        ctx,
        SingletonRefactorResponse(
            symbol_name="shock_type",
            symbol_docstring=docstring,
            symbol_source=source,
        ),
        existing_names=frozenset({"cell_engine_c20"}),
        internals_source="def cell_engine_c20(ctx):\n    return xl_cell(ctx, 'Inputs!B1')\n",
    )


def test_collapse_bindings_for_response_renders_literal_calls() -> None:
    bindings = collapse_bindings_for_response(_cluster_response())
    assert len(bindings) == 2
    assert bindings[0].literal_call == "combined_input_passthrough(ctx, time_period=1)"
    assert bindings[1].literal_call == "combined_input_passthrough(ctx, time_period=2)"


def test_substitute_collapse_bindings_parses_source_once() -> None:
    """Large internals modules must not re-parse once per cluster member."""
    source = """
def consumer(ctx):
    a = cell_engine_c6(ctx)
    b = cell_engine_d6(ctx)
    c = xl_eval(ctx, 'Engine!C6', cell_engine_c6)
    return a + b + c
"""
    bindings = collapse_bindings_for_response(_cluster_response())
    parse_calls = {"count": 0}
    real_parse = ast.parse

    def counting_parse(
        source_text: str, *_args: object, **_kwargs: object
    ) -> ast.Module:
        parse_calls["count"] += 1
        return real_parse(source_text)

    with patch("src.internals_refactor.ast.parse", side_effect=counting_parse):
        updated, rewrite_count = substitute_collapse_bindings(source, bindings)

    assert parse_calls["count"] == 1
    assert rewrite_count == 3
    assert "cell_engine_c6(ctx)" not in updated
    assert "cell_engine_d6(ctx)" not in updated
    assert "xl_eval(ctx, 'Engine!C6', cell_engine_c6)" not in updated
    assert updated.count("combined_input_passthrough(ctx, time_period=1)") == 2
    assert "combined_input_passthrough(ctx, time_period=2)" in updated


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
        expected_helper_name="combined_input_passthrough",
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


def _count_full_module_parses(
    fn: Callable[..., tuple[str, int]],
    *args: object,
    **kwargs: object,
) -> tuple[tuple[str, int], int]:
    """Count ``ast.parse`` calls on full internals modules (not helper snippets)."""
    parse_calls = {"count": 0}
    real_parse = ast.parse

    def counting_parse(
        source_text: str, *_args: object, **_kwargs: object
    ) -> ast.Module:
        if (
            FORMULA_SECTION_MARKER in source_text
            or RESOLVER_SECTION_MARKER in source_text
        ):
            parse_calls["count"] += 1
        return real_parse(source_text)

    with patch("src.internals_refactor.ast.parse", side_effect=counting_parse):
        result = fn(*args, **kwargs)
    return result, parse_calls["count"]


def test_apply_cluster_collapse_parses_full_module_once() -> None:
    """Apply must not re-parse the multi-megabyte module for substitute/remove/dispatch."""
    source = (
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    return 1.0

def cell_engine_d6(ctx):
    return 2.0

def consumer(ctx):
    return cell_engine_c6(ctx) + cell_engine_d6(ctx)

"""
        + RESOLVER_SECTION
    )
    (updated, rewrite_count), full_parses = _count_full_module_parses(
        apply_cluster_collapse,
        source,
        _cluster_response(),
    )
    assert full_parses == 1
    assert rewrite_count == 2
    assert "def cell_engine_c6" not in updated
    assert "def cell_engine_d6" not in updated
    assert "combined_input_passthrough(ctx, time_period=1)" in updated
    assert "combined_input_passthrough(ctx, time_period=2)" in updated


def test_apply_cluster_collapses_batch_parses_full_module_once() -> None:
    """Independent collapses in one batch share a single full-module parse."""
    import src.internals_refactor as module

    source = (
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    return 1.0

def cell_engine_d6(ctx):
    return 2.0

def cell_engine_c7(ctx):
    return 3.0

def cell_engine_d7(ctx):
    return 4.0

def consumer(ctx):
    return (
        cell_engine_c6(ctx)
        + cell_engine_d6(ctx)
        + cell_engine_c7(ctx)
        + cell_engine_d7(ctx)
    )

"""
        + RESOLVER_SECTION
    )
    first = _cluster_response()
    second = ClusterRefactorResponse(
        helper_name="combined_row7_passthrough",
        helper_docstring=CLUSTER_DOCSTRING,
        parameters=CLUSTER_PARAMETERS,
        helper_source=(
            "def combined_row7_passthrough(ctx: EvalContext, time_period: int) "
            "-> CellValue:\n"
            '    """doc"""\n'
            "    return float(time_period)\n"
        ),
        member_keys=(
            MemberKeys(
                address="Engine!C7",
                function_name="cell_engine_c7",
                keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
            ),
            MemberKeys(
                address="Engine!D7",
                function_name="cell_engine_d7",
                keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=2),),
            ),
        ),
    )
    typed_first = ClusterRefactorResponse(
        helper_name="combined_input_passthrough",
        helper_docstring=CLUSTER_DOCSTRING,
        parameters=CLUSTER_PARAMETERS,
        helper_source=(
            "def combined_input_passthrough(ctx: EvalContext, time_period: int) "
            "-> CellValue:\n"
            '    """doc"""\n'
            "    return float(time_period)\n"
        ),
        member_keys=first.member_keys,
    )
    (updated, rewrite_count), full_parses = _count_full_module_parses(
        module.apply_cluster_collapses_batch,
        source,
        (typed_first, second),
    )
    assert full_parses == 1
    assert rewrite_count == 4
    assert "def cell_engine_c6" not in updated
    assert "def cell_engine_d7" not in updated
    assert "def combined_input_passthrough" in updated
    assert "def combined_row7_passthrough" in updated
    assert "combined_input_passthrough(ctx, time_period=1)" in updated
    assert "combined_row7_passthrough(ctx, time_period=2)" in updated


def test_apply_cluster_collapses_batch_merges_missing_imports_once() -> None:
    """Adding typed imports for a batch must not parse once per response."""
    import src.internals_refactor as module

    source = (
        """from __future__ import annotations

from .runtime import (
    XlError,
    xl_cell,
    xl_eval,
)
"""
        + """
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    return 1.0

def cell_engine_d6(ctx):
    return 2.0

def cell_engine_c7(ctx):
    return 3.0

def cell_engine_d7(ctx):
    return 4.0

"""
        + RESOLVER_SECTION
    )
    responses = (
        ClusterRefactorResponse(
            helper_name="h1",
            helper_docstring=CLUSTER_DOCSTRING,
            parameters=CLUSTER_PARAMETERS,
            helper_source=(
                "def h1(ctx: EvalContext, time_period: int) -> CellValue:\n"
                '    """doc"""\n'
                "    return 1.0\n"
            ),
            member_keys=(
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
            ),
        ),
        ClusterRefactorResponse(
            helper_name="h2",
            helper_docstring=CLUSTER_DOCSTRING,
            parameters=CLUSTER_PARAMETERS,
            helper_source=(
                "def h2(ctx: EvalContext, time_period: int) -> CellValue:\n"
                '    """doc"""\n'
                "    return 2.0\n"
            ),
            member_keys=(
                MemberKeys(
                    address="Engine!C7",
                    function_name="cell_engine_c7",
                    keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
                ),
                MemberKeys(
                    address="Engine!D7",
                    function_name="cell_engine_d7",
                    keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=2),),
                ),
            ),
        ),
    )
    (updated, _), full_parses = _count_full_module_parses(
        module.apply_cluster_collapses_batch,
        source,
        responses,
    )
    # One import-merge parse + one post-insert collapse parse — not one per response.
    assert full_parses == 2
    assert "EvalContext" in updated
    assert "CellValue" in updated
    assert "def h1" in updated
    assert "def h2" in updated


def test_pass_one_batches_independent_cluster_applies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Independent mechanical clusters in one layer share a single batch apply."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import FormulaCluster
    from src.mechanical_body import MechanicalBodyDraft

    class _Projection:
        def get_dependencies(self, _address: str) -> tuple[str, ...]:
            return ()

    clusters = (
        FormulaCluster(
            cluster_id=0,
            members=("Engine!C6", "Engine!D6"),
            canonical_template="=Inputs!{col}1",
            row=6,
        ),
        FormulaCluster(
            cluster_id=1,
            members=("Engine!C7", "Engine!D7"),
            canonical_template="=Inputs!{col}1",
            row=7,
        ),
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    return 1.0

def cell_engine_d6(ctx):
    return 2.0

def cell_engine_c7(ctx):
    return 3.0

def cell_engine_d7(ctx):
    return 4.0

"""
        + RESOLVER_SECTION,
        encoding="utf-8",
    )
    draft = MechanicalBodyDraft(
        body="return float(time_period)",
        renameable_locals=(),
        lookup_table_names=(),
        group_count=1,
    )
    batch_calls: list[int] = []
    real_batch = module.apply_cluster_collapses_batch

    def tracking_batch(
        source: str,
        responses: Sequence[ClusterRefactorResponse],
        ctx: ClusterRefactorContext | None = None,
    ) -> tuple[str, int]:
        response_seq = tuple(responses)
        batch_calls.append(len(response_seq))
        return real_batch(source, response_seq, ctx=ctx)

    def fake_build_cluster(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        helper = f"combined_row{cluster.row}"
        return SimpleNamespace(
            cluster_id=cluster.cluster_id,
            canonical_template=cluster.canonical_template,
            members=tuple(
                SimpleNamespace(
                    address=address,
                    function_name=address_to_function_name(address),
                    normalized_formula="=1",
                    python_source=f"def {address_to_function_name(address)}(ctx):\n    return 1.0\n",
                )
                for address in cluster.members
            ),
            naming_hints={},
            allowed_runtime_symbols=(),
            expected_helper_name=helper,
            contract="member_sweep",
        )

    def fake_mechanical_response(
        ctx: SimpleNamespace, _draft: object, **_kwargs: object
    ) -> ClusterRefactorResponse:
        row = int("".join(ch for ch in ctx.expected_helper_name if ch.isdigit()) or "0")
        return ClusterRefactorResponse(
            helper_name=ctx.expected_helper_name,
            helper_docstring=CLUSTER_DOCSTRING,
            parameters=CLUSTER_PARAMETERS,
            helper_source=(
                f"def {ctx.expected_helper_name}(ctx, time_period):\n"
                f"    return float(time_period)\n"
            ),
            member_keys=tuple(
                MemberKeys(
                    address=member.address,
                    function_name=member.function_name,
                    keys=(
                        MemberKeyEntry(
                            dimension_id="TIME_PERIOD",
                            value=1 if member.address.endswith("C" + str(row)) else 2,
                        ),
                    ),
                )
                for member in ctx.members
            ),
        )

    monkeypatch.setattr(module, "apply_cluster_collapses_batch", tracking_batch)
    monkeypatch.setattr(module, "build_cluster_refactor_context", fake_build_cluster)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: draft)
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(
        module, "build_mechanical_cluster_response", fake_mechanical_response
    )
    monkeypatch.setattr(
        module, "validate_cluster_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        module,
        "compute_refactor_schedule",
        lambda *_a, **_k: tuple(
            SimpleNamespace(
                parent_cluster_id=cluster.cluster_id,
                refactor_group_id=index,
                members=cluster.members,
                as_formula_cluster=lambda c=cluster: c,
            )
            for index, cluster in enumerate(clusters)
        ),
    )
    monkeypatch.setattr(
        module,
        "allocate_schedule_helper_names",
        lambda *_a, **_k: tuple(f"combined_row{cluster.row}" for cluster in clusters),
    )
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(
        module,
        "_refresh_mechanical_cluster_results",
        lambda results, **_k: list(results),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, _Projection()),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!C6": "row6",
            "Engine!D6": "row6",
            "Engine!C7": "row7",
            "Engine!D7": "row7",
        },
    )

    assert batch_calls == [2]
    source = internals_path.read_text(encoding="utf-8")
    assert "def combined_row6" in source
    assert "def combined_row7" in source
    assert "def cell_engine_c6" not in source
    assert "def cell_engine_d7" not in source


def test_apply_cluster_collapse_parses_full_module_once_with_dispatch_updates() -> None:
    """Non-engine collapses that rewrite ``_ADDRESS_DISPATCH`` still use one module parse."""
    source = (
        RUNTIME_IMPORT
        + """
# --- Formula cell functions ---

def cell_outputs_c6(ctx):
    return 1.0

def consumer(ctx):
    return cell_outputs_c6(ctx)

"""
        + RESOLVER_SECTION
    )
    response = ClusterRefactorResponse(
        helper_name="output_passthrough",
        helper_docstring=(
            "Return an output measure.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n"
            "    time_period: Projection period.\n"
        ),
        parameters=CLUSTER_PARAMETERS,
        helper_source=(
            "def output_passthrough(ctx, time_period):\n    return float(time_period)\n"
        ),
        member_keys=(
            MemberKeys(
                address="Outputs!C6",
                function_name="cell_outputs_c6",
                keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
            ),
        ),
    )
    (updated, rewrite_count), full_parses = _count_full_module_parses(
        apply_cluster_collapse,
        source,
        response,
    )
    assert full_parses == 1
    assert rewrite_count == 1
    assert "def cell_outputs_c6" not in updated
    assert "def output_passthrough" in updated
    assert "Outputs!C6" in updated
    assert "output_passthrough" in updated
    assert "_ADDRESS_DISPATCH" in updated


def test_apply_singleton_refactor_plan_parses_full_module_once() -> None:
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=1",
        normalized_formula="=1",
        python_source="def cell_engine_c20(ctx):\n    return 1.0\n",
        dependency_addresses=("Engine!C10",),
        external_dependencies=("shock_active",),
        semantic_dependencies=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
        expected_helper_name="projected_debt_to_gdp",
        call_sites=(),
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
    (updated, rewrite_count), full_parses = _count_full_module_parses(
        apply_singleton_refactor_plan,
        INTERNALS_WITH_SINGLETON_CALLER,
        response,
        ctx,
    )
    assert full_parses == 1
    assert rewrite_count == 1
    assert "def projected_debt_to_gdp" in updated
    assert "xl_number(projected_debt_to_gdp(ctx))" in updated


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


# --- RED: stranded identity-passthrough dependency resolution -----------------
#
# Live failure mode (full-pipeline run, clusters 28/49/67/33/34): each cluster is
# a single-slot identity passthrough onto a Baseline engine cell, e.g.
#
#     def cell_hot_adapted_aa32(ctx):
#         '''Formula: =Baseline!AA33.'''
#         return xl_eval(ctx, 'Baseline!AA33', cell_baseline_aa33)
#
# In isolation these synthesize fine — the xl_eval point-read matches the slot by
# address. But by the time they are scheduled (orders 117-122), the Baseline
# engine family has already been collapsed into the `baseline_interest_rate` /
# `baseline_engine_indicators` semantic helpers: `cell_baseline_aa33` is gone and
# the caller body has been rewritten to a bare helper call. `resolve_semantic_
# dependencies` then fails to map `Baseline!AA33` back to its helper (the docstring
# "Covers" heuristic does not name the address, or two helpers match ambiguously),
# so the fingerprint slot stays `xl_cell`, `_collect_read_sites` finds nothing to
# claim it, and mechanical synthesis raises `slots_without_read_sites:[0]` -> LLM
# fallback (which also fails: #VALUE! / KeyError).
#
# `address_to_series_id` already maps `Baseline!AA33` -> `baseline_interest_rate`,
# and helper names *are* series ids, so resolution should never strand a
# dependency whose series is an existing semantic helper — regardless of whether
# the docstring/dispatch heuristics happen to recover it.


def test_resolve_semantic_dependencies_resolves_stranded_passthrough_by_series_id() -> (
    None
):
    """A collapsed passthrough dependency resolves via its series id even when the
    helper's advertised coverage does not name the address."""
    source = (
        RUNTIME_IMPORT
        + '''
# --- Formula cell functions ---

def baseline_interest_rate(ctx, time_period):
    """Return the baseline interest rate for the period.

    Note:
        Covers Baseline!C33:H33.
    """
    return 0.0
'''
        + RESOLVER_SECTION
    )
    # cell_baseline_aa33 has been collapsed away; column AA (index 27) is outside
    # the advertised C33:H33 coverage, so the docstring heuristic cannot recover
    # it and the dependency strands as unresolved.
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!AA33"],
        address_to_series_id={"Baseline!AA33": "baseline_interest_rate"},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "baseline_interest_rate"
    ]
    assert "Baseline!AA33" in resolved[0].addresses


def test_resolve_semantic_dependencies_resolves_stranded_passthrough_when_ambiguous() -> (  # noqa: E501
    None
):
    """When two helpers advertise overlapping coverage of the dependency's row the
    docstring heuristic is ambiguous (returns None); the series id disambiguates."""
    source = (
        RUNTIME_IMPORT
        + '''
# --- Formula cell functions ---

def baseline_interest_rate(ctx, time_period):
    """Note: Covers Baseline!A33:BZ33."""
    return 0.0

def baseline_real_interest_rate(ctx, time_period):
    """Note: Covers Baseline!A33:BZ33."""
    return 0.0
'''
        + RESOLVER_SECTION
    )
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!AA33"],
        address_to_series_id={"Baseline!AA33": "baseline_interest_rate"},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "baseline_interest_rate"
    ]


# --- RED: series-id fallback must respect the helper's provable key domain (#139)
#
# Live failure mode: `Baseline!D12:CP12` (labour productivity growth) is one
# output series with two formula regimes — `D12:Q12` reads `Productivity!J6:W6`,
# `R12:CP12` is the GDP/employment ratio. Only the ratio regime becomes the
# `baseline_labour_productivity_growth` helper, so the helper serves 2023-2099
# while `address_to_series_id` still maps `Baseline!D12` (2009) to it.
#
# The #134 series-id fallback then resolves the stranded `Baseline!D12` read to
# `baseline_labour_productivity_growth(ctx, time_period=2009)`. That helper (and
# everything it calls, down to `demography_working_age_population`, whose
# period->column tables start at 2021) indexes literal period tables, so the
# generated call raises `KeyError: 2009` at evaluation time.
#
# The fallback has no coverage evidence at all, so it must at least refuse a
# helper that *provably* cannot serve the dependency's key.

_PERIOD_TABLE_HELPER = '''
def demography_working_age_population_low(ctx, time_period):
    """Working-age population, low variant.

    Note:
        Covers Demography!BV10:EV10.
    """
    start_col = {2021: 74, 2022: 75, 2023: 76}
    end_col = {2021: 224, 2022: 225, 2023: 226}
    return xl_number(start_col[time_period] + end_col[time_period])
'''


def test_resolve_semantic_dependencies_rejects_series_id_helper_outside_key_domain() -> (
    None
):
    """The series-id fallback refuses a helper whose literal key tables cannot
    serve the dependency's bound key, rather than emitting a KeyError call."""
    source = RUNTIME_IMPORT + _PERIOD_TABLE_HELPER + RESOLVER_SECTION
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Demography!BJ10"],
        address_to_series_id={
            "Demography!BJ10": "demography_working_age_population_low"
        },
        bound_address_keys={"Demography!BJ10": {"TIME_PERIOD": 2009}},
    )
    assert resolved == ()
    assert unresolved == ("cell_demography_bj10",)


def test_resolve_semantic_dependencies_keeps_series_id_helper_inside_key_domain() -> (
    None
):
    """A dependency whose bound key is in the helper's literal tables still
    resolves through the series-id fallback."""
    source = RUNTIME_IMPORT + _PERIOD_TABLE_HELPER + RESOLVER_SECTION
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Demography!BX10"],
        address_to_series_id={
            "Demography!BX10": "demography_working_age_population_low"
        },
        bound_address_keys={"Demography!BX10": {"TIME_PERIOD": 2023}},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "demography_working_age_population_low"
    ]


def test_resolve_semantic_dependencies_rejects_out_of_domain_key_dispatch_helper() -> (
    None
):
    """A ``raise``-terminated equality dispatch is just as provable as a literal
    lookup table: keys outside its branches must not route to the helper."""
    source = (
        RUNTIME_IMPORT
        + '''
def baseline_labour_productivity_growth(ctx, time_period):
    """Note: Covers Baseline!R12:S12."""
    if time_period == 2023:
        return xl_number(1.0)
    if time_period == 2024:
        return xl_number(2.0)
    raise ValueError(time_period)
'''
        + RESOLVER_SECTION
    )
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!D12"],
        address_to_series_id={"Baseline!D12": "baseline_labour_productivity_growth"},
        bound_address_keys={"Baseline!D12": {"TIME_PERIOD": 2009}},
    )
    assert resolved == ()
    assert unresolved == ("cell_baseline_d12",)


def test_resolve_semantic_dependencies_keeps_series_id_helper_without_key_tables() -> (
    None
):
    """#134 regression: a helper with no provable key domain still resolves, even
    when its advertised coverage does not name the address."""
    source = (
        RUNTIME_IMPORT
        + '''
def baseline_interest_rate(ctx, time_period):
    """Note: Covers Baseline!C33:H33."""
    return xl_number(0.0)
'''
        + RESOLVER_SECTION
    )
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!AA33"],
        address_to_series_id={"Baseline!AA33": "baseline_interest_rate"},
        bound_address_keys={"Baseline!AA33": {"TIME_PERIOD": 2035}},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "baseline_interest_rate"
    ]


def test_resolve_semantic_dependencies_ignores_key_domain_without_bound_keys() -> None:
    """Without ``bound_address_keys`` there is no key to check, so the #134
    fallback behaviour is unchanged."""
    source = RUNTIME_IMPORT + _PERIOD_TABLE_HELPER + RESOLVER_SECTION
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Demography!BJ10"],
        address_to_series_id={
            "Demography!BJ10": "demography_working_age_population_low"
        },
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "demography_working_age_population_low"
    ]


def test_helper_static_key_domain_reads_literal_period_tables() -> None:
    helper_def = _single_function_def(_PERIOD_TABLE_HELPER)
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "time_period") == frozenset(
        {2021, 2022, 2023}
    )


def test_helper_static_key_domain_unions_membership_dispatch_branches() -> None:
    helper_def = _single_function_def(
        '''
def helper(ctx, time_period):
    """Note: Covers Baseline!D12:F12."""
    if time_period in {2009, 2010}:
        return xl_number(1.0)
    if time_period == 2011:
        return xl_number(2.0)
    raise ValueError(time_period)
'''
    )
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "time_period") == frozenset(
        {2009, 2010, 2011}
    )


def test_helper_static_key_domain_is_unbounded_with_a_fallthrough_return() -> None:
    """A dispatch chain that falls through to a default body serves every key."""
    helper_def = _single_function_def(
        '''
def helper(ctx, time_period):
    """Note: Covers Baseline!D11:CP11."""
    if time_period in {2022, 2023}:
        return xl_number(1.0)
    return xl_number(2.0)
'''
    )
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "time_period") is None


def test_helper_static_key_domain_is_unbounded_behind_a_non_parameter_branch() -> None:
    """`*_engine_indicators` guards on ``(indicator, time_period)`` tuples before
    its fall-through body; keys routed through those branches never reach the
    table, so no domain can be proven for either parameter."""
    helper_def = _single_function_def(
        '''
def engine_indicators(ctx, indicator, time_period):
    """Note: Covers Engine!C18:D26."""
    if (indicator, time_period) in {('gross_debt_lcu', 2030)}:
        return xl_number(1.0)
    columns = {'interest_expenditure_pct_gdp': 'C'}
    return xl_cell(ctx, f'Engine!{columns[indicator]}18')
'''
    )
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "indicator") is None
    assert helper_static_key_domain(helper_def, "time_period") is None


def test_helper_static_key_domain_ignores_tables_inside_a_branch() -> None:
    """A table read only some keys reach proves nothing about the rest."""
    helper_def = _single_function_def(
        '''
def helper(ctx, time_period):
    """Note: Covers Baseline!D11:CP11."""
    if time_period == 2022:
        columns = {2022: 'Q'}
        return xl_cell(ctx, f'Baseline!{columns[time_period]}11')
    return xl_number(0.0)
'''
    )
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "time_period") is None


def test_helper_static_key_domain_ignores_tables_indexed_by_an_offset() -> None:
    """A lagged read cannot prove which key the caller needs, so stay unbounded."""
    helper_def = _single_function_def(
        '''
def helper(ctx, time_period):
    """Note: Covers Baseline!D11:CP11."""
    start_col = {2021: 74, 2022: 75}
    return xl_number(start_col[time_period - 1])
'''
    )
    assert helper_def is not None
    assert helper_static_key_domain(helper_def, "time_period") is None


def test_rehome_unrefactored_cell_functions_moves_residuals_out_of_alias_section() -> (
    None
):
    source = (
        RUNTIME_IMPORT
        + f"""
{FORMULA_SECTION_MARKER}

@xl_memoize
def shock_active(ctx, time_period):
    \"\"\"Covers Engine!C10:G10.\"\"\"
    return xl_cell(ctx, 'Inputs!B21')

def cell_engine_c11(ctx):
    return xl_cell(ctx, 'Inputs!C1')

{PROJECTION_ALIAS_SECTION_MARKER}

def cell_engine_c12(ctx):
    _t1 = shock_active(ctx, time_period=1)
    return xl_number(_t1)

"""
        + RESOLVER_SECTION
    )
    updated = rehome_unrefactored_cell_functions(source)
    assert PROJECTION_ALIAS_SECTION_MARKER not in updated
    assert UNREFACTORED_CELLS_SECTION_MARKER in updated
    formula_at = updated.index(FORMULA_SECTION_MARKER)
    unrefactored_at = updated.index(UNREFACTORED_CELLS_SECTION_MARKER)
    resolver_at = updated.index(RESOLVER_SECTION_MARKER)
    assert formula_at < unrefactored_at < resolver_at
    helper_region = updated[formula_at:unrefactored_at]
    residual_region = updated[unrefactored_at:resolver_at]
    assert "def shock_active" in helper_region
    assert "@xl_memoize" in helper_region
    assert "def cell_engine_c11" not in helper_region
    assert "def cell_engine_c12" not in helper_region
    assert "def cell_engine_c11" in residual_region
    assert "def cell_engine_c12" in residual_region
    assert "def shock_active" not in residual_region
    # Idempotent: a second pass keeps the same section layout.
    again = rehome_unrefactored_cell_functions(updated)
    assert again.index(UNREFACTORED_CELLS_SECTION_MARKER) == unrefactored_at
    assert PROJECTION_ALIAS_SECTION_MARKER not in again


def test_rehome_unrefactored_cell_functions_noop_without_section_markers() -> None:
    source = "def cell_engine_b2(ctx):\n    return 4.0\n"
    assert rehome_unrefactored_cell_functions(source) == source


def test_rehome_unrefactored_cell_functions_omits_section_when_no_residuals() -> None:
    source = (
        RUNTIME_IMPORT
        + f"""
{FORMULA_SECTION_MARKER}

def shock_active(ctx, time_period):
    return xl_cell(ctx, 'Inputs!B21')

{PROJECTION_ALIAS_SECTION_MARKER}

"""
        + RESOLVER_SECTION
    )
    updated = rehome_unrefactored_cell_functions(source)
    assert PROJECTION_ALIAS_SECTION_MARKER not in updated
    assert UNREFACTORED_CELLS_SECTION_MARKER not in updated
    assert "def shock_active" in updated
    assert updated.index(FORMULA_SECTION_MARKER) < updated.index(
        RESOLVER_SECTION_MARKER
    )


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
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id={"Engine!C20": "shock_passthrough"},
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
        expected_helper_name="projected_debt_to_gdp",
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
        expected_helper_name="projected_debt_to_gdp",
    )


def test_llm_refactor_singleton_wires_parity_into_post_validate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

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

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

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
            symbol_docstring=None,
            symbol_body=None,
            error=True,
            error_reason="   ",
        )


def test_singleton_llm_response_error_requires_null_success_fields() -> None:
    with pytest.raises(ValidationError, match="success fields must be null"):
        SingletonRefactorLLMResponse(
            symbol_docstring="Doc.",
            symbol_body="return 1.0",
            error=True,
            error_reason="Cannot proceed.",
        )


def test_singleton_llm_response_error_allows_null_success_fields() -> None:
    response = SingletonRefactorLLMResponse(
        symbol_docstring=None,
        symbol_body=None,
        error=True,
        error_reason="Unsupported independent operand variation.",
    )
    assert response.error is True
    assert response.symbol_docstring is None
    with pytest.raises(RefactorDeclaredError, match="Unsupported independent"):
        raise_if_llm_declared_error(
            response,
            kind="singleton",
            target="Engine!C20",
        )


def test_cluster_llm_response_error_allows_null_success_fields() -> None:
    response = ClusterRefactorLLMResponse(
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

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    prepare_calls = 0
    error_payload = {
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

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    dump_root = tmp_path / "failures"
    error_payload = {
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


def test_llm_refactor_singleton_multi_attempt_failure_dumps_full_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_c20(ctx):\n    return 1.0\n", encoding="utf-8"
    )
    ctx = _singleton_refactor_test_context(tmp_path)
    dump_root = tmp_path / "failures"
    bodies = ["return 1.0", "return 2.0", "return 3.0"]

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
            self.calls = 0

        def create(self, **kwargs: object) -> _FakeResponse:
            body = bodies[min(self.calls, len(bodies) - 1)]
            self.calls += 1
            payload = {
                "symbol_docstring": (
                    "Projected debt-to-GDP.\n\n"
                    "Args:\n    ctx: Workbook evaluation context.\n\n"
                    "Returns:\n    Projected debt-to-GDP ratio."
                ),
                "symbol_body": body,
                "error": False,
                "error_reason": None,
            }
            return _FakeResponse(json.dumps(payload))

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    fake_client = _FakeClient()
    parity_calls = 0

    def always_fail_parity(**kwargs: object) -> None:
        nonlocal parity_calls
        parity_calls += 1
        raise ParityError(f"parity mismatch on attempt {parity_calls}")

    monkeypatch.setattr(
        module,
        "build_client",
        lambda _model: (
            fake_client,
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
        lambda *_args, **_kwargs: "singleton context prompt",
    )
    monkeypatch.setattr(module, "REFACTOR_FAILURE_DUMP_DIR", dump_root)
    monkeypatch.setattr(
        "src.refactor_parity_gate.check_singleton_parity",
        always_fail_parity,
    )

    with pytest.raises(ValidatedJsonFailure, match="after 3 attempts"):
        llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source="def cell_engine_c20(ctx):\n    return 1.0\n",
            input_vectors=[{}],
        )

    assert parity_calls == 3
    assert fake_client.chat.completions.calls == 3
    dumps = list(dump_root.iterdir())
    assert len(dumps) == 1
    dump_dir = dumps[0]

    conversation = json.loads(
        (dump_dir / "conversation.json").read_text(encoding="utf-8")
    )
    assert conversation[1]["role"] == "user"
    assert "singleton context prompt" in conversation[1]["content"]
    assert conversation[2]["role"] == "assistant"
    assert "return 1.0" in conversation[2]["content"]
    assert conversation[3]["role"] == "user"
    assert "parity mismatch on attempt 1" in conversation[3]["content"]
    assert "return 2.0" in conversation[4]["content"]
    assert "parity mismatch on attempt 2" in conversation[5]["content"]
    assert "return 3.0" in conversation[6]["content"]

    for index, body in enumerate(bodies, start=1):
        attempt_dir = dump_dir / "attempts" / f"{index:02d}"
        error_text = (attempt_dir / "error.txt").read_text(encoding="utf-8")
        assert f"parity mismatch on attempt {index}" in error_text
        llm_response = json.loads(
            (attempt_dir / "llm_response.json").read_text(encoding="utf-8")
        )
        assert llm_response["symbol_body"] == body
        prepared = json.loads(
            (attempt_dir / "prepared_response.json").read_text(encoding="utf-8")
        )
        assert body in prepared["symbol_source"]

    # Compatibility: top-level artifacts still reflect the final attempt.
    final_llm = json.loads((dump_dir / "llm_response.json").read_text(encoding="utf-8"))
    assert final_llm["symbol_body"] == "return 3.0"


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
    expected_helper_name="indicator_change_from_reference",
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


TRADE_BALANCE_SERIES_MAP = {
    "Engine!B5": "trade_balance",
    "Engine!C5": "trade_balance",
    "Engine!D5": "trade_balance",
    # Operand series ids so unbound-ref geometry checks do not fall back
    # when Inputs cells sweep by row under TIME_PERIOD.
    "Inputs!B10": "exports",
    "Inputs!B11": "exports",
    "Inputs!B12": "exports",
    "Inputs!C10": "imports",
    "Inputs!C11": "imports",
    "Inputs!C12": "imports",
}

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


# A single-operand fixed-lag cluster (#132): each member reads the prior
# period's source cell, a relation mechanical synthesis reproduces cleanly. Used
# to prove that a cluster the operand-routing gate rejects is still rescued to
# member_sweep when verified synthesis covers it.
LAG_CLUSTER = FormulaCluster(
    cluster_id=9,
    members=("Engine!B2", "Engine!C2", "Engine!D2"),
    canonical_template="=Inputs!A1",
    row=2,
)

LAG_FORMULAS = {
    "Engine!B2": "=Inputs!A1",
    "Engine!C2": "=Inputs!B1",
    "Engine!D2": "=Inputs!C1",
}

LAG_SERIES_MAP = {
    "Engine!B2": "lagged_series",
    "Engine!C2": "lagged_series",
    "Engine!D2": "lagged_series",
    "Inputs!A1": "source_series",
    "Inputs!B1": "source_series",
    "Inputs!C1": "source_series",
}

LAG_OPERAND_KEYS: dict[str, dict[str, BindingKeyValue]] = {
    "Inputs!A1": {"TIME_PERIOD": 1},
    "Inputs!B1": {"TIME_PERIOD": 2},
    "Inputs!C1": {"TIME_PERIOD": 3},
    "Engine!B2": {"TIME_PERIOD": 2},
    "Engine!C2": {"TIME_PERIOD": 3},
    "Engine!D2": {"TIME_PERIOD": 4},
}

LAG_INTERNALS = (
    RUNTIME_IMPORT
    + """
# --- Formula cell functions ---

def cell_engine_b2(ctx):
    return xl_cell(ctx, 'Inputs!A1')

def cell_engine_c2(ctx):
    return xl_cell(ctx, 'Inputs!B1')

def cell_engine_d2(ctx):
    return xl_cell(ctx, 'Inputs!C1')
"""
)


def _lag_projection() -> ProjectionResult:
    dependencies = {
        "Engine!B2": ("Inputs!A1",),
        "Engine!C2": ("Inputs!B1",),
        "Engine!D2": ("Inputs!C1",),
    }
    return cast(
        ProjectionResult,
        _ClusterProjectionStub(LAG_FORMULAS, dependencies),
    )


def _write_lag_internals(tmp_path: Path) -> Path:
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(LAG_INTERNALS, encoding="utf-8")
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


def test_build_cluster_refactor_context_rescues_gate_rejection_via_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#132: the routing gate's rejection is a hint, not a hard skip.

    ``select_cluster_refactor_contract`` returns ``None`` for the variable
    country-pair cluster (no counterpart dimension id to route the second
    operand), and key-dispatch cannot rescue it either. Mechanical synthesis,
    however, reproduces every member via a ``REF_AREA`` lookup
    (``{US: CN, DE: FR, JP: KR}``), so the context builder now routes it as
    ``member_sweep`` instead of leaving ``cell_*`` wrappers behind.
    """
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.contract == "member_sweep"


def test_build_cluster_refactor_context_rescues_gate_rejection_offset_lag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#132: a gate-rejected fixed-lag cluster is rescued to member_sweep.

    With the gate and key-dispatch forced to reject, the context builder still
    attempts mechanical synthesis; the single-operand ``t-1`` offset verifies,
    so the cluster routes as ``member_sweep`` rather than being skipped.
    """
    import src.internals_refactor as module

    monkeypatch.setattr(
        module,
        "allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
    )
    monkeypatch.setattr(
        module, "select_cluster_refactor_contract", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "plan_key_dispatch", lambda *_a, **_k: None)

    ctx = build_cluster_refactor_context(
        _lag_projection(),
        LAG_CLUSTER,
        _write_lag_internals(tmp_path),
        bound_address_keys=LAG_OPERAND_KEYS,
        key_vocabulary=(KEY_VOCABULARY[0],),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=LAG_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.contract == "member_sweep"
    assert ctx.expected_helper_name == "lagged_series"


def test_build_cluster_refactor_context_keeps_skip_when_gate_rejection_fails_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#132: the rescue probe restores the skip when synthesis cannot verify.

    When the routing gate and key-dispatch both reject a cluster and mechanical
    synthesis also raises, the builder returns ``None`` -- the probe only
    rescues clusters verified synthesis can reproduce, so genuinely unroutable
    clusters keep today's skip rather than being forced onto a broken body.
    """
    import src.internals_refactor as module
    from src.mechanical_body import MechanicalSynthesisError

    monkeypatch.setattr(
        module,
        "allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
    )
    monkeypatch.setattr(
        module, "select_cluster_refactor_contract", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "plan_key_dispatch", lambda *_a, **_k: None)

    def _fail_synthesis(*_args: object, **_kwargs: object) -> object:
        raise MechanicalSynthesisError("forced_synthesis_failure")

    monkeypatch.setattr("src.mechanical_body.synthesize_cluster_body", _fail_synthesis)

    ctx = build_cluster_refactor_context(
        _lag_projection(),
        LAG_CLUSTER,
        _write_lag_internals(tmp_path),
        bound_address_keys=LAG_OPERAND_KEYS,
        key_vocabulary=(KEY_VOCABULARY[0],),
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=LAG_SERIES_MAP,
    )
    assert ctx is None


def test_build_cluster_refactor_context_selects_dimension_aware_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct dimension ids on the member cells unlock Contract B."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.contract == "dimension_aware"
    assert ctx.expected_member_keys["Engine!B5"] == {
        "REF_AREA": "US",
        "COUNTERPART_REF_AREA": "CN",
    }


def test_build_cluster_refactor_context_locks_helper_name_from_series_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.expected_helper_name == "trade_balance"


def test_build_cluster_refactor_context_defaults_to_member_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain sweep cluster keeps Contract A."""
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
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
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
            address_to_series_id=TRADE_BALANCE_SERIES_MAP,
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
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
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
        lambda *_args, **_kwargs: ALLOWED_RUNTIME_SYMBOLS,
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
        address_to_series_id=TRADE_BALANCE_SERIES_MAP,
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
    assert "## Fingerprint F1" in dump
    assert "cell_engine_b5" in dump
    assert "Exemplar translation" in dump
    assert "Member key space" in dump
    assert "Reference relations" in dump


def test_llm_refactor_singleton_uses_shared_index_without_rereads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")

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
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
        )

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
        return SimpleNamespace(source=updated, symbol_name=ctx.function_name)

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
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)

    refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!B3": "family_b",
            "Engine!C3": "family_c",
        },
    )

    # One initial index + one rebuild per successful singleton apply (4 units).
    assert len(from_source_sources) == 1 + apply_count["n"]
    assert apply_count["n"] == 4
    assert len(indices_seen) == 4
    assert indices_seen[0] is not indices_seen[1]
    assert from_source_sources[0] == version_sources[0]
    assert from_source_sources[1] == version_sources[1]


def _fake_mechanical_singleton_response(ctx: object) -> Any:
    """Minimal mechanical response stub including ``symbol_source`` for Pass 1."""
    from types import SimpleNamespace

    address = getattr(ctx, "address", "Engine!A1")
    symbol_name = "helper_" + str(address).replace("!", "_").lower()
    return SimpleNamespace(
        symbol_name=symbol_name,
        symbol_source=(
            f"def {symbol_name}(ctx):\n"
            f'    """Stub helper for {address}."""\n'
            f"    return 0.0\n"
        ),
    )


def test_pass_one_defers_internals_write_until_single_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass 1 applies units in memory; internals.py is flushed exactly once."""
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
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")

    events: list[tuple[str, str]] = []
    apply_count = {"n": 0}
    original_write_text = Path.write_text

    def tracking_write_text(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self == internals_path:
            events.append(("write", data))
        return original_write_text(
            self, data, encoding=encoding, errors=errors, newline=newline
        )

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        _source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_count["n"] += 1
        events.append(("apply", str(apply_count["n"])))
        return version_sources[apply_count["n"]], 0

    def fake_naming_pass(
        _pending_units: object,
        *,
        internals_index: InternalsSourceIndex,
        **_kwargs: object,
    ) -> tuple[InternalsSourceIndex, dict[str, object]]:
        events.append(("pass2", ""))
        return internals_index, {}

    monkeypatch.setattr(Path, "write_text", tracking_write_text)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(module, "_run_semantic_naming_pass", fake_naming_pass)
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!B3": "family_b",
            "Engine!C3": "family_c",
        },
    )

    # Four in-memory applies, then a single end-of-Pass-1 flush, then Pass 2,
    # then Phase C's own write — never a per-unit write.
    assert [kind for kind, _ in events] == [
        "apply",
        "apply",
        "apply",
        "apply",
        "write",
        "pass2",
        "write",
    ]
    assert events[4][1] == version_sources[4]


def test_pass_one_writes_mechanical_checkpoint_before_parity_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass 1 must persist mechanical source before the batched parity gate."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    checkpoint_path = module.mechanical_internals_checkpoint_path(internals_path)
    pristine = "def cell_engine_b2(ctx):\n    return 0.0\n"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(pristine, encoding="utf-8")
    version_sources[0] = pristine

    events: list[str] = []
    apply_count = {"n": 0}
    gate_kwargs: dict[str, object] = {}
    real_validate = module.validate_refactored_internals
    original_write_text = Path.write_text

    def tracking_validate(source: str) -> None:
        # Only the Pass 1 validate must precede the checkpoint; Phase C validates
        # again after promote and is outside this ordering contract.
        if "checkpoint" not in events:
            events.append("validate")
            assert not checkpoint_path.exists()
        real_validate(source)

    def tracking_write_text(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self == checkpoint_path:
            assert events[-1] == "validate"
            events.append("checkpoint")
        return original_write_text(
            self, data, encoding=encoding, errors=errors, newline=newline
        )

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        _source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_count["n"] += 1
        events.append("apply")
        return version_sources[apply_count["n"]], 0

    def fake_gate(**kwargs: object) -> None:
        events.append("parity_gate")
        gate_kwargs.update(kwargs)
        assert checkpoint_path.is_file(), "checkpoint missing before parity gate"
        assert checkpoint_path.read_text(encoding="utf-8") == version_sources[4]
        assert internals_path.read_text(encoding="utf-8") == pristine

    monkeypatch.setattr(module, "validate_refactored_internals", tracking_validate)
    monkeypatch.setattr(Path, "write_text", tracking_write_text)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))
    monkeypatch.setattr(
        "src.refactor_parity_gate.build_default_input_vectors",
        lambda **_kwargs: ({},),
    )
    monkeypatch.setattr(
        "src.refactor_parity_gate.check_batched_mechanical_parity",
        fake_gate,
    )

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=True,
        constraints={},
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!B3": "family_b",
            "Engine!C3": "family_c",
        },
    )

    assert events == [
        "apply",
        "apply",
        "apply",
        "apply",
        "validate",
        "checkpoint",
        "parity_gate",
    ]
    assert gate_kwargs["mechanical_source"] == version_sources[4]
    assert internals_path.read_text(encoding="utf-8") == version_sources[4]
    assert not checkpoint_path.exists()
    assert checkpoint_path.parent != internals_path.parent


def test_pass_one_parity_failure_keeps_package_internals_pristine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Parity failure must not promote the mechanical sidecar to package path."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    checkpoint_path = module.mechanical_internals_checkpoint_path(internals_path)
    pristine = "def cell_engine_b2(ctx):\n    return 0.0\n"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(pristine, encoding="utf-8")
    version_sources[0] = pristine
    apply_count = {"n": 0}

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        _source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_count["n"] += 1
        return version_sources[apply_count["n"]], 0

    def failing_gate(**_kwargs: object) -> None:
        raise ParityError("mechanical divergence")

    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("pass2 must not run")),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))
    monkeypatch.setattr(
        "src.refactor_parity_gate.build_default_input_vectors",
        lambda **_kwargs: ({},),
    )
    monkeypatch.setattr(
        "src.refactor_parity_gate.check_batched_mechanical_parity",
        failing_gate,
    )

    with pytest.raises(ParityError, match="mechanical divergence"):
        module.refactor_internals_all_clusters(
            cast(ProjectionResult, graph),
            clusters,
            internals_path=internals_path,
            bindings_path=tmp_path / "bindings",
            workbook_path=tmp_path / "workbook.xlsx",
            dry_run=False,
            parity_gate=True,
            constraints={},
            address_to_series_id={
                "Engine!B2": "family_b",
                "Engine!C2": "family_c",
                "Engine!B3": "family_b",
                "Engine!C3": "family_c",
            },
        )

    assert apply_count["n"] == 4
    assert checkpoint_path.is_file()
    assert checkpoint_path.read_text(encoding="utf-8") == version_sources[4]
    assert internals_path.read_text(encoding="utf-8") == pristine
    assert checkpoint_path.parent != internals_path.parent
    assert not (
        internals_path.parent / module.MECHANICAL_INTERNALS_CHECKPOINT_NAME
    ).exists()


def test_mechanical_checkpoint_namespaces_distinct_package_roots(
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    dist_internals = tmp_path / "dist" / "pkg" / "internals.py"
    lab_internals = tmp_path / "artifacts" / "refactor-lab" / "pkg" / "internals.py"
    dist_checkpoint = module.mechanical_internals_checkpoint_path(dist_internals)
    lab_checkpoint = module.mechanical_internals_checkpoint_path(lab_internals)

    assert dist_checkpoint != lab_checkpoint
    assert dist_checkpoint.name == module.MECHANICAL_INTERNALS_CHECKPOINT_NAME
    assert lab_checkpoint.name == module.MECHANICAL_INTERNALS_CHECKPOINT_NAME
    assert dist_checkpoint.parent.parent == module.DEFAULT_INTERNALS_CACHE_DIR
    assert lab_checkpoint.parent.parent == module.DEFAULT_INTERNALS_CACHE_DIR
    assert dist_internals.parent not in dist_checkpoint.parents
    assert lab_internals.parent not in lab_checkpoint.parents


def test_pass_one_defers_full_module_validate_until_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass 1 must not parse/compile the full module after every unit apply."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.wide_layer import wide_layer_graph

    graph, bindings = wide_layer_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")

    events: list[str] = []
    apply_count = {"n": 0}
    real_validate = module.validate_refactored_internals

    def tracking_validate(source: str) -> None:
        events.append("validate")
        real_validate(source)

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        _source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_count["n"] += 1
        events.append("apply")
        return version_sources[apply_count["n"]], 0

    monkeypatch.setattr(module, "validate_refactored_internals", tracking_validate)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!D2": "family_d",
            "Engine!E2": "family_e",
        },
    )

    assert apply_count["n"] == 4
    # Four applies, then one end-of-Pass-1 validate, then Phase C's validate —
    # never a validate interleaved with each apply.
    assert events == [
        "apply",
        "apply",
        "apply",
        "apply",
        "validate",
        "validate",
    ]


def test_pass_one_reindexes_lazily_per_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Units whose reads are untouched share one index; only dependents reseal.

    Layer 1 (B2/C2/D2) is independent, so all three contexts must come from the
    initial index with no rebuild in between. E2 reads all of layer 1, so it
    must see an index rebuilt from the accumulated source, and every apply must
    chain onto the previous apply's output rather than the stale index source.
    """
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.wide_layer import wide_layer_graph

    graph, bindings = wide_layer_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")

    from_source_sources: list[str] = []
    real_from_source = module.InternalsSourceIndex.from_source
    context_indices: list[tuple[str, module.InternalsSourceIndex]] = []
    apply_bases: list[str] = []
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
        address = cluster.members[0]
        context_indices.append((address, index))
        return SimpleNamespace(
            address=address,
            function_name=address_to_function_name(address),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_bases.append(source)
        apply_count["n"] += 1
        return version_sources[apply_count["n"]], 0

    monkeypatch.setattr(
        module.InternalsSourceIndex,
        "from_source",
        staticmethod(tracking_from_source),
    )
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!D2": "family_d",
            "Engine!E2": "family_e",
        },
    )

    assert apply_count["n"] == 4
    # Every apply chains onto the previous apply's output, not the index source.
    assert apply_bases == version_sources[:4]
    # Reindex cadence is per layer, not per unit: the initial file read, one
    # reseal when E2 reads the three dirty collapses, and the end-of-pass seal.
    assert from_source_sources == [
        version_sources[0],
        version_sources[3],
        version_sources[4],
    ]
    # Layer 1 contexts share the initial index; E2's context sees the rebuilt
    # index containing all upstream applies.
    by_address = dict(context_indices)
    assert len(context_indices) == 4
    layer_one = [index for address, index in context_indices if address != "Engine!E2"]
    assert len(layer_one) == 3
    assert layer_one[0] is layer_one[1] is layer_one[2]
    assert layer_one[0].source == version_sources[0]
    assert by_address["Engine!E2"].source == version_sources[3]
    assert context_indices[-1][0] == "Engine!E2"


def test_pass_one_fallback_applies_onto_accumulated_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An LLM-fallback unit with clean reads keeps the stale index for context
    but must apply onto the accumulated source, not the index's snapshot."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.wide_layer import wide_layer_graph

    graph, bindings = wide_layer_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")

    apply_bases: list[str] = []
    fallback_calls: list[tuple[str, module.InternalsSourceIndex]] = []
    apply_count = {"n": 0}
    fallback_address = "Engine!C2"

    def _next_version(base: str) -> str:
        apply_bases.append(base)
        apply_count["n"] += 1
        return version_sources[apply_count["n"]]

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_synthesize(ctx: SimpleNamespace) -> object | None:
        return None if ctx.address == fallback_address else object()

    def fake_fallback(
        ctx: SimpleNamespace,
        *,
        internals_path: Path,
        dry_run: bool = False,
        internals_index: object = None,
        apply_source: str | None = None,
        **_kwargs: object,
    ) -> SimpleNamespace:
        assert isinstance(internals_index, module.InternalsSourceIndex)
        assert apply_source is not None
        fallback_calls.append((apply_source, internals_index))
        return SimpleNamespace(
            source=_next_version(apply_source),
            symbol_name=ctx.function_name,
        )

    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", fake_synthesize)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(module, "refactor_internals_singleton", fake_fallback)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        module,
        "apply_singleton_refactor_plan",
        lambda source, _response, _ctx: (_next_version(source), 0),
    )
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!D2": "family_d",
            "Engine!E2": "family_e",
        },
    )

    assert apply_count["n"] == 4
    # Mechanical and fallback applies alike chain onto the accumulated source.
    assert apply_bases == version_sources[:4]
    assert len(fallback_calls) == 1
    fallback_apply_source, fallback_index = fallback_calls[0]
    # The fallback unit is in the independent layer, so its index may lag at
    # the initial snapshot — but its apply base must be fully up to date.
    assert fallback_index.source == version_sources[0]
    assert fallback_apply_source != fallback_index.source


def _run_wide_layer_mechanical_pass1(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Drive a four-singleton mechanical Pass 1 with stubbed synthesize/apply."""
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from tests.fixtures.wide_layer import wide_layer_graph

    graph, bindings = wide_layer_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    version_sources = [
        f"def cell_engine_b2(ctx):\n    return {n}.0\n" for n in range(5)
    ]
    internals_path.write_text(version_sources[0], encoding="utf-8")
    apply_count = {"n": 0}

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            address=cluster.members[0],
            function_name=address_to_function_name(cluster.members[0]),
            canonical_template="=1",
            normalized_formula="=1",
            python_source="return 1.0",
            allowed_runtime_symbols=(),
        )

    def fake_apply_plan(
        _source: str, _response: object, _ctx: object
    ) -> tuple[str, int]:
        apply_count["n"] += 1
        return version_sources[apply_count["n"]], 0

    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: object())
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)
    monkeypatch.setattr(
        module,
        "build_mechanical_singleton_response",
        lambda ctx, *_a, **_k: _fake_mechanical_singleton_response(ctx),
    )
    monkeypatch.setattr(
        module, "validate_singleton_refactor_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "apply_singleton_refactor_plan", fake_apply_plan)
    monkeypatch.setattr(
        module,
        "_run_semantic_naming_pass",
        lambda _pending, *, internals_index, **_k: (internals_index, {}),
    )
    monkeypatch.setattr(module, "apply_phase_c", lambda source, **_kwargs: (source, 0))

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=False,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!D2": "family_d",
            "Engine!E2": "family_e",
        },
    )
    return internals_path


def test_pass_one_unit_timing_observer_records_phases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Observer receives one record per applied unit with phase fields present."""
    import src.internals_refactor as module

    recorded: list[module.Pass1UnitTiming] = []
    module.set_pass1_unit_timing_observer(recorded.append)
    try:
        _run_wide_layer_mechanical_pass1(module, monkeypatch, tmp_path)
    finally:
        module.set_pass1_unit_timing_observer(None)

    assert len(recorded) == 4
    targets = {item.unit_id for item in recorded}
    assert targets == {
        "cluster_0_g0",
        "cluster_1_g1",
        "cluster_2_g2",
        "cluster_3_g3",
    }
    for item in recorded:
        assert item.kind == "singleton"
        assert item.member_count == 1
        assert item.mechanical is True
        assert item.context_s >= 0.0
        assert item.synthesize_s >= 0.0
        assert item.apply_s >= 0.0
        assert item.validate_s >= 0.0
        assert item.reindex_s >= 0.0
        assert item.apply_batch_size >= 1
        assert item.source_bytes > 0
        assert isinstance(item.reindexed, bool)
        assert isinstance(item.dirty_count, int)

    # Dependent layer-2 unit must reseal before context build.
    by_target = {item.unit_id: item for item in recorded}
    assert by_target["cluster_3_g3"].reindexed is True
    assert by_target["cluster_3_g3"].reindex_s >= 0.0
    assert all(
        not by_target[unit_id].reindexed
        for unit_id in ("cluster_0_g0", "cluster_1_g1", "cluster_2_g2")
    )


def test_pass_one_unit_timing_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without the env flag or observer, Pass 1 emits no per-unit timing lines."""
    import logging

    import src.internals_refactor as module

    monkeypatch.delenv("PASS1_UNIT_TIMERS", raising=False)
    monkeypatch.delenv("PASS1_UNIT_TIMERS_JSONL", raising=False)
    module.set_pass1_unit_timing_observer(None)
    with caplog.at_level(logging.INFO, logger="src.internals_refactor"):
        _run_wide_layer_mechanical_pass1(module, monkeypatch, tmp_path)

    assert not any("pass1 unit timing:" in message for message in caplog.messages)


def test_pass_one_unit_timing_logs_and_writes_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Env flag logs one INFO line per unit and appends JSONL when configured."""
    import logging

    import src.internals_refactor as module

    jsonl_path = tmp_path / "pass1-unit-timings.jsonl"
    monkeypatch.setenv("PASS1_UNIT_TIMERS", "1")
    monkeypatch.setenv("PASS1_UNIT_TIMERS_JSONL", str(jsonl_path))
    module.set_pass1_unit_timing_observer(None)

    with caplog.at_level(logging.INFO, logger="src.internals_refactor"):
        _run_wide_layer_mechanical_pass1(module, monkeypatch, tmp_path)

    timing_lines = [
        message
        for message in caplog.messages
        if message.startswith("pass1 unit timing:")
    ]
    assert len(timing_lines) == 4
    assert any("members=1" in line for line in timing_lines)
    assert any("synthesize=" in line for line in timing_lines)

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 4
    assert {row["unit_id"] for row in rows} == {
        "cluster_0_g0",
        "cluster_1_g1",
        "cluster_2_g2",
        "cluster_3_g3",
    }
    assert all("member_count" in row for row in rows)
    assert all("synthesize_s" in row for row in rows)


def test_llm_refactor_cluster_uses_dimension_aware_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(PRISTINE_CLUSTER, encoding="utf-8")
    recorded: dict[str, object] = {}
    llm_response = ClusterRefactorLLMResponse(
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
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=True,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!B3": "family_b",
            "Engine!C3": "family_c",
        },
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


def test_refactor_internals_all_clusters_records_spans_on_the_stage_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass 1 / parity-gate / Pass 2 wall clock lands on the caller's StageTimer."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import cluster_graph_formulas
    from src.pipeline_monitor import StageTimer
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_b2(ctx):\n    return 1.0\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        module,
        "build_singleton_refactor_context",
        lambda _projection, cluster, _internals_path, **_kwargs: SimpleNamespace(
            address=cluster.members[0]
        ),
    )
    monkeypatch.setattr(
        module, "refactor_internals_singleton", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(
        module, "build_cluster_refactor_context", lambda *_a, **_k: None
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)

    timer = StageTimer()
    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=True,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_b",
            "Engine!C2": "family_c",
            "Engine!B3": "family_b",
            "Engine!C3": "family_c",
        },
        timer=timer,
    )

    spans = timer.as_dict()
    # Leaf spans only: a ``pass1`` rollup would double-count with pass1_*.
    assert {
        "pass1_context",
        "pass1_synthesize",
        "pass1_apply",
        "pass1_validate",
        "pass1_reindex",
        "mechanical_parity_gate",
        "pass2_semantic_naming",
        "phase_c",
    } <= set(spans)
    assert "pass1" not in spans
    assert all(seconds >= 0.0 for seconds in spans.values())


def test_refactor_internals_all_clusters_passes_unique_allocated_helper_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Peel schedule units receive distinct expected_helper_name kwargs before LLM."""
    import src.internals_refactor as module
    from types import SimpleNamespace

    from src.formula_clustering import FormulaCluster
    from src.refactor_order import compute_refactor_schedule

    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2", "Engine!A3"),
        canonical_template="=PRIOR",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=Engine!A1",
        row=None,
    )

    class _ChainWithCrossHingeProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                "Engine!B1": ("Engine!A1",),
                "Engine!A2": ("Engine!A1", "Engine!B1"),
                "Engine!A3": ("Engine!A2",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _ChainWithCrossHingeProjection())
    clusters = (family_a, family_b)
    scheduled = [
        unit.members for unit in compute_refactor_schedule(projection, clusters)
    ]
    assert scheduled == [
        ("Engine!A1",),
        ("Engine!B1",),
        ("Engine!A2", "Engine!A3"),
    ]

    internals_path = tmp_path / "internals.py"
    # Existing semantic helper blocks the bare series_id for peels.
    internals_path.write_text(
        "def shocked_path_internal(ctx):\n    return 0.0\n"
        "def not_semantic(x):\n    return x\n",
        encoding="utf-8",
    )

    captured: list[tuple[tuple[str, ...], str | None, frozenset[str] | None]] = []

    def fake_build_singleton(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **kwargs: object,
    ) -> SimpleNamespace:
        captured.append(
            (
                cluster.members,
                cast(str | None, kwargs.get("expected_helper_name")),
                cast(frozenset[str] | None, kwargs.get("existing_helper_names")),
            )
        )
        return SimpleNamespace(address=cluster.members[0])

    def fake_build_cluster(
        _projection: object,
        cluster: FormulaCluster,
        _internals_path: Path,
        **kwargs: object,
    ) -> SimpleNamespace:
        captured.append(
            (
                cluster.members,
                cast(str | None, kwargs.get("expected_helper_name")),
                cast(frozenset[str] | None, kwargs.get("existing_helper_names")),
            )
        )
        return SimpleNamespace(members=cluster.members)

    monkeypatch.setattr(
        module, "build_singleton_refactor_context", fake_build_singleton
    )
    monkeypatch.setattr(module, "build_cluster_refactor_context", fake_build_cluster)
    monkeypatch.setattr(
        module,
        "refactor_internals_singleton",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        module,
        "refactor_internals_cluster",
        lambda *_a, **_k: SimpleNamespace(
            response=None,
            helper_name="unused",
            wrappers_applied=(),
            dry_run=True,
            source="",
        ),
    )
    monkeypatch.setattr(module, "_try_synthesize_singleton_body", lambda _ctx: None)
    monkeypatch.setattr(module, "_try_synthesize_cluster_body", lambda _ctx: None)

    module.refactor_internals_all_clusters(
        projection,
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        dry_run=True,
        parity_gate=False,
        address_to_series_id={
            "Engine!A1": "shocked_path_internal",
            "Engine!A2": "shocked_path_internal",
            "Engine!A3": "shocked_path_internal",
            "Engine!B1": "hinge_helper",
        },
    )

    helper_names = [name for _members, name, _reserved in captured]
    assert helper_names == [
        "shocked_path_internal_2",
        "hinge_helper",
        "shocked_path_internal_3",
    ]
    assert len(set(helper_names)) == len(helper_names)
    # Non-semantic `not_semantic` must not participate in schedule allocation blocking.
    first_reserved = captured[0][2]
    assert first_reserved is not None
    assert "not_semantic" not in first_reserved
    assert "shocked_path_internal" in first_reserved


def test_refactor_internals_all_clusters_forwards_bound_address_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multi-member units must receive caller keys, not the graph-rebuild fallback."""
    import src.internals_refactor as module

    from src.refactor_order import RefactorUnit
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = (
        FormulaCluster(
            cluster_id=0,
            canonical_template="=X",
            members=("Engine!B2", "Engine!C2"),
            row=2,
        ),
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_b2(ctx):\n    return 1.0\n"
        "def cell_engine_c2(ctx):\n    return 1.0\n",
        encoding="utf-8",
    )
    received: dict[str, object] = {}

    def fake_build_cluster(
        *_args: object,
        bound_address_keys: object = None,
        **_kwargs: object,
    ) -> None:
        received["bound_address_keys"] = bound_address_keys
        return None

    unit = RefactorUnit(
        parent_cluster_id=0,
        refactor_group_id=0,
        members=("Engine!B2", "Engine!C2"),
        canonical_template="=X",
        row=2,
    )
    monkeypatch.setattr(module, "compute_refactor_schedule", lambda *_a, **_k: (unit,))
    monkeypatch.setattr(module, "build_cluster_refactor_context", fake_build_cluster)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: None
    )

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        bound_address_keys=cast(dict[str, dict[str, BindingKeyValue]], bindings),
        dry_run=True,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_bc",
            "Engine!C2": "family_bc",
        },
    )

    assert received["bound_address_keys"] is bindings


def test_refactor_internals_all_clusters_forwards_key_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multi-member units must receive caller vocabulary, not the YAML-reload fallback."""
    import src.internals_refactor as module

    from src.refactor_order import RefactorUnit
    from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph

    graph, bindings = inter_cluster_cycle_graph()
    clusters = (
        FormulaCluster(
            cluster_id=0,
            canonical_template="=X",
            members=("Engine!B2", "Engine!C2"),
            row=2,
        ),
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def cell_engine_b2(ctx):\n    return 1.0\n"
        "def cell_engine_c2(ctx):\n    return 1.0\n",
        encoding="utf-8",
    )
    vocabulary = (
        KeyConceptSpec(
            dimension_id="TIME_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="time_period",
        ),
    )
    received: dict[str, object] = {}

    def fake_build_cluster(
        *_args: object,
        key_vocabulary: object = None,
        **_kwargs: object,
    ) -> None:
        received["key_vocabulary"] = key_vocabulary
        return None

    def boom_default_key_vocabulary(_bindings_path: Path) -> tuple[KeyConceptSpec, ...]:
        raise AssertionError("_default_key_vocabulary must not run")

    unit = RefactorUnit(
        parent_cluster_id=0,
        refactor_group_id=0,
        members=("Engine!B2", "Engine!C2"),
        canonical_template="=X",
        row=2,
    )
    monkeypatch.setattr(module, "compute_refactor_schedule", lambda *_a, **_k: (unit,))
    monkeypatch.setattr(module, "build_cluster_refactor_context", fake_build_cluster)
    monkeypatch.setattr(module, "_default_key_vocabulary", boom_default_key_vocabulary)
    monkeypatch.setattr(
        module, "build_singleton_refactor_context", lambda *_a, **_k: None
    )

    module.refactor_internals_all_clusters(
        cast(ProjectionResult, graph),
        clusters,
        internals_path=internals_path,
        bindings_path=tmp_path / "bindings",
        workbook_path=tmp_path / "workbook.xlsx",
        bound_address_keys=cast(dict[str, dict[str, BindingKeyValue]], bindings),
        key_vocabulary=vocabulary,
        dry_run=True,
        parity_gate=False,
        address_to_series_id={
            "Engine!B2": "family_bc",
            "Engine!C2": "family_bc",
        },
    )

    assert received["key_vocabulary"] is vocabulary


def test_sample_indices_for_prompt_keeps_small_sequences() -> None:
    assert sample_indices_for_prompt(12, limit=50) == tuple(range(12))


def test_sample_indices_for_prompt_caps_and_keeps_endpoints() -> None:
    indices = sample_indices_for_prompt(420, limit=50)
    assert len(indices) <= 50
    assert indices[0] == 0
    assert indices[-1] == 419
    assert indices == tuple(sorted(indices))
    assert len(set(indices)) == len(indices)


def test_complete_cluster_member_keys_fills_omitted_members() -> None:
    members = tuple(
        MemberContext(
            address=f"Engine!{column}6",
            function_name=f"cell_engine_{column.lower()}6",
            engine_column=column,
            normalized_formula=f"=Inputs!{column}1",
            python_source=(
                f"def cell_engine_{column.lower()}6(ctx):\n"
                f"    return xl_cell(ctx, 'Inputs!{column}1')\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        )
        for column in ("C", "D", "E")
    )
    ctx = replace(
        CLUSTER_CONTEXT,
        members=members,
        expected_member_keys={
            "Engine!C6": {"TIME_PERIOD": 1},
            "Engine!D6": {"TIME_PERIOD": 2},
            "Engine!E6": {"TIME_PERIOD": 3},
        },
    )
    partial = (
        MemberKeys(
            address="Engine!C6",
            function_name="cell_engine_c6",
            keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
        ),
    )
    completed = complete_cluster_member_keys_from_expected(
        partial,
        ctx,
        parameters=CLUSTER_PARAMETERS,
    )
    assert [entry.address for entry in completed] == [
        "Engine!C6",
        "Engine!D6",
        "Engine!E6",
    ]
    assert completed[1].keys == (MemberKeyEntry(dimension_id="TIME_PERIOD", value=2),)
    assert completed[2].function_name == "cell_engine_e6"


def test_prepare_cluster_refactor_response_accepts_sampled_member_keys() -> None:
    members = tuple(
        MemberContext(
            address=f"Engine!{column}6",
            function_name=f"cell_engine_{column.lower()}6",
            engine_column=column,
            normalized_formula=f"=Inputs!{column}1",
            python_source=(
                f"def cell_engine_{column.lower()}6(ctx):\n    return 1.0\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        )
        for column in ("C", "D", "E")
    )
    ctx = replace(
        CLUSTER_CONTEXT,
        members=members,
        expected_member_keys={
            "Engine!C6": {"TIME_PERIOD": 1},
            "Engine!D6": {"TIME_PERIOD": 2},
            "Engine!E6": {"TIME_PERIOD": 3},
        },
    )
    llm_response = ClusterRefactorLLMResponse(
        symbol_docstring=CLUSTER_DOCSTRING,
        symbol_body="return xl_cell(ctx, f'Inputs!{chr(66 + time_period)}1')",
        parameters=CLUSTER_PARAMETERS,
        member_keys=(
            MemberKeys(
                address="Engine!C6",
                function_name="cell_engine_c6",
                keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
            ),
        ),
        error=None,
        error_reason=None,
    )
    prepared = prepare_cluster_refactor_response(
        llm_response,
        ctx,
        runtime_source="def xl_cell(ctx, address):\n    return 0\n",
        internals_source="\n\n".join(member.python_source for member in members),
    )
    assert [entry.address for entry in prepared.member_keys] == [
        "Engine!C6",
        "Engine!D6",
        "Engine!E6",
    ]
    assert prepared.parameters[0].name == "time_period"


def test_prepare_cluster_refactor_response_ignores_llm_parameters_and_member_keys() -> (
    None
):
    """LLM-supplied parameters/member_keys are accepted for one version then ignored."""
    llm_response = ClusterRefactorLLMResponse(
        symbol_docstring=CLUSTER_DOCSTRING,
        symbol_body="return xl_cell(ctx, f'Inputs!{chr(66 + time_period)}1')",
        parameters=(
            HelperParameter(
                name="wrong_name",
                dimension_id="TIME_PERIOD",
                dtype="int",
            ),
        ),
        member_keys=(
            MemberKeys(
                address="Engine!C6",
                function_name="cell_engine_c6",
                keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=99),),
            ),
        ),
        error=None,
        error_reason=None,
    )
    prepared = prepare_cluster_refactor_response(
        llm_response,
        CLUSTER_CONTEXT,
        runtime_source="def xl_cell(ctx, address):\n    return 0\n",
        internals_source=VALID_CLUSTER_SOURCE,
    )
    assert prepared.parameters == (
        HelperParameter(
            name="time_period",
            dimension_id="TIME_PERIOD",
            dtype="int",
            concept="TIME_PERIOD",
        ),
    )
    assert prepared.member_keys[0].keys[0].value == 1
    assert prepared.member_keys[1].keys[0].value == 2


def test_build_cluster_refactor_prompt_context_uses_fingerprint_for_large_clusters(
    tmp_path: Path,
) -> None:
    from src.refactor_fingerprints import build_cluster_fingerprint_summary

    members = tuple(
        MemberContext(
            address=f"Engine!C{row}",
            function_name=f"cell_engine_c{row}",
            engine_column="C",
            normalized_formula=f"=Inputs!C{row}",
            python_source=(
                f"def cell_engine_c{row}(ctx):\n"
                f"    return xl_cell(ctx, 'Inputs!C{row}')\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        )
        for row in range(1, 61)
    )
    bound_keys = {
        **{
            member.address: {"TIME_PERIOD": index + 1}
            for index, member in enumerate(members)
        },
        **{f"Inputs!C{row}": {"TIME_PERIOD": row} for row in range(1, 61)},
    }
    expected_member_keys = {
        member.address: {"TIME_PERIOD": index + 1}
        for index, member in enumerate(members)
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected_member_keys,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "\n\n".join(member.python_source for member in members),
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    ctx = replace(
        CLUSTER_CONTEXT,
        members=members,
        expected_member_keys=expected_member_keys,
        fingerprint_summary=summary,
    )
    dump = build_cluster_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
        runtime_path=runtime_path,
        member_limit=5,
    )
    assert "showing 5 of 60 members" not in dump
    assert "60 of 60 members" in dump
    assert "Exemplar translation" in dump
    assert dump.count("def cell_engine_c") == 1


def test_build_cluster_refactor_prompt_context_falls_back_when_summary_unusable(
    tmp_path: Path,
) -> None:
    from src.refactor_fingerprints import ClusterFingerprintSummary

    members = tuple(
        MemberContext(
            address=f"Engine!C{row}",
            function_name=f"cell_engine_c{row}",
            engine_column="C",
            normalized_formula="=Inputs!C1",
            python_source=(
                f"def cell_engine_c{row}(ctx):\n    return xl_cell(ctx, 'Inputs!C1')\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        )
        for row in range(1, 61)
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "\n\n".join(member.python_source for member in members),
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.py"
    runtime_path.write_text(
        "def xl_cell(ctx, address):\n    return 0\n", encoding="utf-8"
    )
    ctx = replace(
        CLUSTER_CONTEXT,
        members=members,
        expected_member_keys={
            member.address: {"TIME_PERIOD": index + 1}
            for index, member in enumerate(members)
        },
        fingerprint_summary=ClusterFingerprintSummary(
            groups=(),
            key_space={"TIME_PERIOD": tuple(range(1, 61))},
            key_to_column=None,
            fallback_reason="missing_ref_key_values",
        ),
    )
    dump = build_cluster_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
        runtime_path=runtime_path,
        member_limit=5,
    )
    assert "showing 5 of 60 members" in dump
    assert dump.count("def cell_engine_c") == 5
    assert "cell_engine_c1" in dump
    assert "cell_engine_c60" in dump


def test_cluster_refactor_prompts_document_fingerprint_and_mechanical_fields() -> None:
    for contract in ("member_sweep", "dimension_aware", "key_dispatch"):
        prompt = load_cluster_refactor_prompt_fixed_portion(contract)
        assert "fingerprint" in prompt.lower() or "Reference relations" in prompt
        assert "mechanically" in prompt.lower()
        assert "Do not emit `parameters` or `member_keys`" in prompt


_READER_WITH_KWONLY = """\
def read_primary_balance_baseline(
    ctx: EvalContext,
    *,
    time_period: int,
) -> CellValue:
    \"\"\"Return the primary-balance baseline for a projection period.\"\"\"
    return xl_cell(ctx, f'Inputs!B{time_period}')
"""


def test_singleton_refactor_prompt_includes_reader_stub_with_keyword_only_args(
    tmp_path: Path,
) -> None:
    """Called read_* helpers from _readers.py must appear with real signatures."""
    cell_source = (
        "def cell_engine_c20(ctx):\n"
        "    return read_primary_balance_baseline(ctx, time_period=1)\n"
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(cell_source, encoding="utf-8")
    (tmp_path / "runtime.py").write_text(
        "def xl_cell(ctx, address):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "_readers.py").write_text(_READER_WITH_KWONLY, encoding="utf-8")
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=Inputs!B1",
        normalized_formula="=Inputs!B1",
        python_source=cell_source,
        dependency_addresses=("Inputs!B1",),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS
        + ("read_primary_balance_baseline",),
        naming_hints={},
        expected_helper_name="primary_balance_baseline",
    )

    dump = build_singleton_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
    )

    assert "def read_primary_balance_baseline(" in dump
    assert "*, time_period: int" in dump


def test_cluster_refactor_prompt_includes_reader_stub_with_keyword_only_args(
    tmp_path: Path,
) -> None:
    """Cluster dependency stubs must include called read_* keyword-only signatures."""
    member_sources = (
        (
            "def cell_engine_c6(ctx):\n"
            "    return read_primary_balance_baseline(ctx, time_period=1)\n"
        ),
        (
            "def cell_engine_d6(ctx):\n"
            "    return read_primary_balance_baseline(ctx, time_period=2)\n"
        ),
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text("\n\n".join(member_sources), encoding="utf-8")
    (tmp_path / "runtime.py").write_text(
        "def xl_cell(ctx, address):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "_readers.py").write_text(_READER_WITH_KWONLY, encoding="utf-8")
    members = (
        replace(
            CLUSTER_MEMBERS[0],
            python_source=member_sources[0],
            normalized_formula="=Inputs!B1",
        ),
        replace(
            CLUSTER_MEMBERS[1],
            python_source=member_sources[1],
            normalized_formula="=Inputs!B2",
        ),
    )
    ctx = replace(
        CLUSTER_CONTEXT,
        members=members,
        fingerprint_summary=None,
    )

    dump = build_cluster_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
    )

    assert "def read_primary_balance_baseline(" in dump
    assert "*, time_period: int" in dump


def test_refactor_prompt_omits_readers_when_readers_module_missing(
    tmp_path: Path,
) -> None:
    """Older exports without _readers.py remain a no-op for prompt stubs."""
    cell_source = (
        "def cell_engine_c20(ctx):\n"
        "    return read_primary_balance_baseline(ctx, time_period=1)\n"
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(cell_source, encoding="utf-8")
    (tmp_path / "runtime.py").write_text(
        "def xl_cell(ctx, address):\n    return 0\n",
        encoding="utf-8",
    )
    ctx = SingletonRefactorContext(
        address="Engine!C20",
        function_name="cell_engine_c20",
        canonical_template="=Inputs!B1",
        normalized_formula="=Inputs!B1",
        python_source=cell_source,
        dependency_addresses=("Inputs!B1",),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS
        + ("read_primary_balance_baseline",),
        naming_hints={},
        expected_helper_name="primary_balance_baseline",
    )

    dump = build_singleton_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
    )

    dependencies = dump.split("Dependencies:", 1)[1]
    assert "def read_primary_balance_baseline(" not in dependencies


def test_resolve_semantic_dependencies_prefers_scheduled_owner_over_series_id() -> None:
    """A peel-split series resolves to the unit that owns the address (issue #138).

    ``allocate_schedule_helper_names`` locks one helper name per schedule unit
    before the pass, so when a series is peeled the base name goes to one unit and
    ``_2`` to the other. The series-id fallback (#134) only knows the bare series
    id, so it would send an address owned by the later peel to the earlier peel's
    helper — a silently wrong read. The scheduled owner is authoritative.
    """
    source = (
        RUNTIME_IMPORT
        + '''
# --- Formula cell functions ---

def baseline_engine_indicators(ctx, time_period):
    """Note: Covers Baseline!D36:X36."""
    columns = {2028: 'W', 2029: 'X'}
    return xl_cell(ctx, f'Baseline!{columns[time_period]}36')

def baseline_engine_indicators_2(ctx, time_period):
    """Note: Covers Baseline!E42:X42."""
    columns = {2028: 'W', 2029: 'X'}
    return xl_cell(ctx, f'Baseline!{columns[time_period]}42')
'''
        + RESOLVER_SECTION
    )
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!X42"],
        address_to_series_id={"Baseline!X42": "baseline_engine_indicators"},
        address_to_helper_name={"Baseline!X42": "baseline_engine_indicators_2"},
        # Both peels can serve 2029, so the #139 key-domain guard passes and the
        # scheduled owner decides which one is read.
        bound_address_keys={"Baseline!X42": {"TIME_PERIOD": 2029}},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "baseline_engine_indicators_2"
    ]
    assert resolved[0].addresses == ("Baseline!X42",)


def test_resolve_semantic_dependencies_ignores_scheduled_owner_not_yet_published() -> (
    None
):
    """A planned helper name that no longer exists must not shadow the fallbacks.

    Units that fail to refactor keep their ``cell_*`` wrappers, and pass 2 renames
    helpers, so a scheduled name is only trusted while it names a live helper.
    """
    source = (
        RUNTIME_IMPORT
        + '''
# --- Formula cell functions ---

def baseline_interest_rate(ctx, time_period):
    """Note: Covers Baseline!C33:H33."""
    return 0.0
'''
        + RESOLVER_SECTION
    )
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Baseline!AA33"],
        address_to_series_id={"Baseline!AA33": "baseline_interest_rate"},
        address_to_helper_name={"Baseline!AA33": "baseline_interest_rate_2"},
    )
    assert unresolved == ()
    assert [dependency.helper_name for dependency in resolved] == [
        "baseline_interest_rate"
    ]


def test_resolve_semantic_dependencies_declines_scheduled_owner_outside_key_domain() -> (
    None
):
    """The scheduled owner is still bounded by the helper's provable key domain.

    The schedule says which unit owns an address, not which keys the published
    helper ended up serving, so a stale owner must not emit a call the helper's
    literal tables would raise ``KeyError`` on (#139). Nothing else claims the
    address here, so the dependency stays unresolved.
    """
    source = RUNTIME_IMPORT + _PERIOD_TABLE_HELPER + RESOLVER_SECTION
    resolved, unresolved = resolve_semantic_dependencies(
        source,
        ["Demography!BJ10"],
        address_to_series_id={
            "Demography!BJ10": "demography_working_age_population_low"
        },
        address_to_helper_name={
            "Demography!BJ10": "demography_working_age_population_low"
        },
        bound_address_keys={"Demography!BJ10": {"TIME_PERIOD": 2009}},
    )
    assert resolved == ()
    assert unresolved == ("cell_demography_bj10",)
