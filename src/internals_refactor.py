from __future__ import annotations

import ast
import builtins
import hashlib
import json
import logging
import os
import re
import textwrap
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.formula_clustering import FormulaCluster
from src.llm_json import DEFAULT_MAX_ATTEMPTS, generate_validated_json
from src.llm_providers import build_client, model_from_env, provider_for_model
from src.pipeline_context import projection_layout as active_projection_layout
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address
from src.internal_bindings import InternalBindingIndex, internal_binding_for_address
from src.refactor_bindings import (
    BindingKeyValue,
    KeyConceptSpec,
    build_bound_address_keys,
    engine_column_from_member_keys,
    expected_member_keys_for_cluster,
    format_binding_key_literal,
    helper_parameters_for_varying_keys,
    load_key_concept_vocabulary,
    render_literal_helper_call,
    resolve_dimension_key,
)
from src.refactor_contracts import (
    ClusterRefactorContract,
    concepts_with_multiple_dimensions,
    select_cluster_refactor_contract,
)
from src.refactor_fingerprints import (
    ClusterFingerprintSummary,
    SemanticDependencyRef,
    build_cluster_fingerprint_summary,
    format_cluster_fingerprint_dump,
)
from src.refactor_order import compute_refactor_schedule, refactor_failure_target
from src.refactor_return_types import (
    ALLOWED_REFACTOR_RETURN_TYPE_HINTS,
    KNOWN_RUNTIME_RETURN_HINTS,
    _binding_dtype_to_python,
    infer_refactor_return_type_hint,
    normalize_return_type_hint_for_allowlist,
    validate_scalar_return_type_hint,
)
from src.runtime_symbols import allowed_runtime_symbols
from src.semantic_naming import (
    BindingRecordHints,
    _is_semantic_helper_def,
    allocate_schedule_helper_names,
    cluster_binding_naming_hints,
    semantic_helpers_available_for_calls,
    binding_record_hints_from_cell,
    sole_series_id_for_addresses,
    validate_semantic_identifier,
)

repo_root = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

REFACTOR_MODEL_ENV = "REFACTOR_MODEL"
REFACTOR_PROMPT_VERSION = 29
CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT = 30
_FINGERPRINT_FALLBACK_COUNT = 0


def fingerprint_fallback_count() -> int:
    """Return how many clusters fell back to the legacy sampled dump this process."""
    return _FINGERPRINT_FALLBACK_COUNT


def reset_fingerprint_fallback_count() -> None:
    global _FINGERPRINT_FALLBACK_COUNT
    _FINGERPRINT_FALLBACK_COUNT = 0


def _record_fingerprint_fallback(reason: str, *, cluster_id: int) -> None:
    global _FINGERPRINT_FALLBACK_COUNT
    _FINGERPRINT_FALLBACK_COUNT += 1
    logger.info(
        "cluster %s fingerprint dump fallback (%s); using legacy sampled sources "
        "(fallback_count=%s)",
        cluster_id,
        reason,
        _FINGERPRINT_FALLBACK_COUNT,
    )


def refactor_model() -> str:
    return model_from_env(REFACTOR_MODEL_ENV)


def _refactor_provider_key_present() -> bool:
    provider = provider_for_model(refactor_model())
    return bool(os.environ.get(provider.api_key_env))


REFACTOR_CACHE_PATH = repo_root / ".cache/internals-refactors.json"
REFACTOR_FAILURE_DUMP_DIR = repo_root / ".cache" / "refactor_failures"
FORMULA_SECTION_MARKER = "# --- Formula cell functions ---"
RESOLVER_SECTION_MARKER = "# --- Formula resolver ---"

AddressDispatch = dict[str, tuple[str, dict[str, BindingKeyValue]]]

REFACTOR_ROW_ORDER: tuple[int, ...] = ()
"""Optional legacy row order hint; prefer ``compute_refactor_schedule``."""


def _refactor_failure_target_slug(
    *, kind: Literal["singleton", "cluster"], target: str
) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_")
    if kind == "cluster" and not slug.startswith("cluster_"):
        return f"cluster_{slug}"
    return slug or kind


def write_refactor_failure_diagnostic(
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
    error: BaseException,
    dump_dir: Path | None = None,
    user_prompt: str | None = None,
    llm_response: Mapping[str, Any] | None = None,
    prepared_response: Mapping[str, Any] | None = None,
    raw_content: str | None = None,
    source: Literal["llm", "cache"] = "llm",
    model: str | None = None,
) -> Path:
    """Persist refactor failure artifacts for offline diagnosis."""
    root = dump_dir if dump_dir is not None else REFACTOR_FAILURE_DUMP_DIR
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = _refactor_failure_target_slug(kind=kind, target=target)
    failure_dir = root / f"{timestamp}_{slug}"
    failure_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    if llm_response is not None:
        files["llm_response"] = "llm_response.json"
        (failure_dir / files["llm_response"]).write_text(
            json.dumps(dict(llm_response), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if prepared_response is not None:
        files["prepared_response"] = "prepared_response.json"
        (failure_dir / files["prepared_response"]).write_text(
            json.dumps(dict(prepared_response), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if raw_content is not None:
        files["raw_content"] = "raw_content.json"
        (failure_dir / files["raw_content"]).write_text(
            json.dumps({"content": raw_content}, indent=2) + "\n",
            encoding="utf-8",
        )
    if user_prompt is not None:
        files["user_prompt"] = "user_prompt.md"
        (failure_dir / files["user_prompt"]).write_text(
            user_prompt,
            encoding="utf-8",
        )

    error_path = "error.txt"
    files["error"] = error_path
    (failure_dir / error_path).write_text(str(error) + "\n", encoding="utf-8")

    manifest = {
        "kind": kind,
        "target": target,
        "source": source,
        "model": model,
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "timestamp": timestamp,
        "files": files,
    }
    (failure_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return failure_dir


@dataclass(frozen=True)
class CallSite:
    caller_function: str
    caller_address: str | None
    callee_function: str
    callee_address: str
    pattern: Literal["xl_eval", "direct"]
    line: int
    snippet: str


@dataclass(frozen=True)
class MemberContext:
    address: str
    function_name: str
    engine_column: str
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    dependency_functions: tuple[str, ...]
    binding_keys: dict[str, BindingKeyValue] | None = None
    binding_record: dict[str, BindingKeyValue] | None = None


@dataclass(frozen=True)
class SemanticDependency:
    """Maps a refactored upstream cell range to the semantic helper that covers it."""

    helper_name: str
    call_form: str
    address_template: str
    columns: tuple[str, ...]
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class ClusterRefactorContext:
    cluster_id: int
    canonical_template: str
    row: int | None
    members: tuple[MemberContext, ...]
    external_dependencies: tuple[str, ...]
    semantic_dependencies: tuple[SemanticDependency, ...]
    call_sites: tuple[CallSite, ...]
    first_year_column: str
    allowed_runtime_symbols: tuple[str, ...]
    key_vocabulary: tuple[KeyConceptSpec, ...]
    expected_member_keys: dict[str, dict[str, BindingKeyValue]]
    naming_hints: dict[str, object]
    expected_helper_name: str
    contract: ClusterRefactorContract = "member_sweep"
    fingerprint_summary: ClusterFingerprintSummary | None = None


class HelperParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Python parameter name for the helper, e.g. time_period."
    )
    dimension_id: str = Field(
        description=(
            "Effective binding dimension id this parameter varies along, "
            "e.g. PROJECTION_PERIOD or TIME_PERIOD."
        )
    )
    dtype: str = Field(description="Expected Python dtype for the parameter.")
    concept: str | None = Field(
        default=None,
        description=(
            "SDMX-style concept referenced by the dimension, e.g. TIME_PERIOD. "
            "Optional; filled from key_vocabulary when omitted."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_concept_as_dimension_id(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if payload.get("dimension_id") is None and payload.get("concept") is not None:
            payload["dimension_id"] = payload["concept"]
        return payload


class MemberKeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(
        description=(
            "Effective binding dimension id, e.g. PROJECTION_PERIOD or TIME_PERIOD."
        )
    )
    value: str | int | float | bool = Field(
        description="Literal binding key value for this dimension."
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_concept_as_dimension_id(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if payload.get("dimension_id") is None and payload.get("concept") is not None:
            payload["dimension_id"] = payload["concept"]
        payload.pop("concept", None)
        return payload


class MemberKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(description="Workbook address this entry covers.")
    function_name: str = Field(description="Existing cell_* function being replaced.")
    keys: tuple[MemberKeyEntry, ...] = Field(
        description=(
            "Literal binding key values for this address; one entry per dimension, "
            "excluding series-constant dimensions."
        )
    )

    def keys_dict(self) -> dict[str, BindingKeyValue]:
        return {entry.dimension_id: entry.value for entry in self.keys}


class ClusterRefactorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    helper_name: str = Field(
        description="New snake_case function in internals.py, e.g. shock_active."
    )
    helper_docstring: str = Field(
        description=(
            "Google-style docstring for the helper; must match the docstring "
            "embedded in helper_source exactly. Include Args and Returns sections."
        )
    )
    parameters: tuple[HelperParameter, ...] = Field(
        description=(
            "Economic parameters the helper varies along, tied to binding "
            "dimension ids."
        )
    )
    helper_source: str = Field(
        description=(
            "Complete Python function definition including def line, Google-style "
            "docstring, and body. Signature must be (ctx, <parameters>). "
            "Use only runtime symbols from allowed_runtime_symbols."
        )
    )
    member_keys: tuple[MemberKeys, ...] = Field(
        description=(
            "One entry per cluster member with literal key values for that address."
        )
    )


class RefactorDeclaredError(RuntimeError):
    """Raised when the LLM declares that a refactor cannot proceed safely."""

    def __init__(
        self,
        reason: str,
        *,
        kind: Literal["singleton", "cluster"],
        target: str,
    ) -> None:
        self.reason = reason
        self.kind = kind
        self.target = target
        super().__init__(reason)


def _validate_llm_response_error_or_success[T: BaseModel](
    response: T,
    *,
    success_fields: tuple[str, ...],
    optional_ignored_fields: tuple[str, ...] = (),
) -> T:
    error = getattr(response, "error")
    error_reason = getattr(response, "error_reason")
    if error is True:
        reason = error_reason.strip() if isinstance(error_reason, str) else ""
        if not reason:
            raise ValueError(
                "error_reason must be a non-empty string when error is true"
            )
        populated = [
            name for name in success_fields if getattr(response, name) is not None
        ]
        populated.extend(
            name
            for name in optional_ignored_fields
            if getattr(response, name, None) is not None
        )
        if populated:
            raise ValueError(
                "success fields must be null when error is true: "
                + ", ".join(populated)
            )
        return response
    if error_reason is not None:
        raise ValueError("error_reason must be null unless error is true")
    missing = [
        name
        for name in success_fields
        if getattr(response, name) is None
        or (
            isinstance(getattr(response, name), str)
            and not getattr(response, name).strip()
        )
    ]
    if missing:
        raise ValueError(
            "missing required fields for successful refactor: " + ", ".join(missing)
        )
    return response


def raise_if_llm_declared_error(
    response: BaseModel,
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
) -> None:
    """Abort immediately when the LLM sets ``error`` to true."""
    if getattr(response, "error") is not True:
        return
    error_reason = getattr(response, "error_reason")
    reason = error_reason.strip() if isinstance(error_reason, str) else ""
    if not reason:
        raise ValueError("error_reason must be a non-empty string when error is true")
    raise RefactorDeclaredError(reason, kind=kind, target=target)


class ClusterRefactorLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_docstring: str | None = Field(
        description=(
            "Google-style docstring. Include Args and Returns sections. "
            "Null when error is true."
        ),
    )
    symbol_body: str | None = Field(
        description="Python function body. Null when error is true.",
    )
    parameters: tuple[HelperParameter, ...] | None = Field(
        default=None,
        description=(
            "Deprecated: ignored when present. The pipeline synthesizes parameters "
            "from varying binding dimensions and key_vocabulary. Null when error "
            "is true or omitted on success."
        ),
    )
    member_keys: tuple[MemberKeys, ...] | None = Field(
        default=None,
        description=(
            "Deprecated: ignored when present. The pipeline synthesizes member_keys "
            "from expected binding keys. Null when error is true or omitted on success."
        ),
    )
    error: bool | None = Field(
        description=(
            "Set to true to abort this refactor and stop the pipeline when the "
            "cluster cannot be safely refactored. Null or false on success."
        ),
    )
    error_reason: str | None = Field(
        description=(
            "Human-readable explanation of why refactoring must abort. "
            "Non-empty when error is true; null otherwise."
        ),
    )

    @model_validator(mode="after")
    def _require_success_fields_or_declared_error(self) -> ClusterRefactorLLMResponse:
        return _validate_llm_response_error_or_success(
            self,
            success_fields=(
                "symbol_docstring",
                "symbol_body",
            ),
            optional_ignored_fields=("parameters", "member_keys"),
        )


CLUSTER_REFACTOR_PROMPT_FIXTURES: dict[ClusterRefactorContract, Path] = {
    "member_sweep": repo_root / "tests" / "fixtures" / "cluster_refactor_prompt.md",
    "dimension_aware": (
        repo_root / "tests" / "fixtures" / "cluster_refactor_prompt_dimension_aware.md"
    ),
}
CLUSTER_REFACTOR_PROMPT_FIXTURE = CLUSTER_REFACTOR_PROMPT_FIXTURES["member_sweep"]


@dataclass(frozen=True)
class SingletonRefactorContext:
    address: str
    function_name: str
    canonical_template: str
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    semantic_dependencies: tuple[SemanticDependency, ...]
    call_sites: tuple[CallSite, ...]
    allowed_runtime_symbols: tuple[str, ...]
    naming_hints: dict[str, object]
    expected_helper_name: str


class SingletonRefactorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_name: str = Field(
        description="Semantic snake_case function name, e.g. initial_debt_to_gdp."
    )
    symbol_docstring: str = Field(
        description=(
            "Google-style docstring; must match the docstring embedded in "
            "symbol_source exactly. Include Args and Returns sections."
        )
    )
    symbol_source: str = Field(
        description=(
            "Complete Python function definition including def line, Google-style "
            "docstring, and body. Signature must be (ctx)."
        )
    )


class SingletonRefactorLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_docstring: str | None = Field(
        description=(
            "Google-style docstring. Include Args and Returns sections. "
            "Null when error is true."
        ),
    )
    symbol_body: str | None = Field(
        description="Python function body. Null when error is true.",
    )
    error: bool | None = Field(
        description=(
            "Set to true to abort this refactor and stop the pipeline when the "
            "cell cannot be safely refactored. Null or false on success."
        ),
    )
    error_reason: str | None = Field(
        description=(
            "Human-readable explanation of why refactoring must abort. "
            "Non-empty when error is true; null otherwise."
        ),
    )

    @model_validator(mode="after")
    def _require_success_fields_or_declared_error(self) -> SingletonRefactorLLMResponse:
        return _validate_llm_response_error_or_success(
            self,
            success_fields=(
                "symbol_docstring",
                "symbol_body",
            ),
        )


ALLOWED_SINGLETON_RETURN_TYPE_HINTS = ALLOWED_REFACTOR_RETURN_TYPE_HINTS
ALLOWED_REFACTOR_TYPE_HINT_NAMES = frozenset({"CellValue", "EvalContext"})

SINGLETON_REFACTOR_PROMPT_FIXTURE = (
    repo_root / "tests" / "fixtures" / "singleton_refactor_prompt.md"
)


@dataclass(frozen=True)
class CollapseBinding:
    address: str
    function_name: str
    helper_name: str
    literal_call: str


@dataclass(frozen=True)
class ClusterRefactorApplyResult:
    source: str
    helper_name: str
    wrappers_applied: tuple[str, ...]
    dry_run: bool
    response: ClusterRefactorResponse
    phase_c_pruned: int = 0


@dataclass(frozen=True)
class SingletonRefactorApplyResult:
    source: str
    symbol_name: str
    old_function_name: str
    reference_rewrites: int
    dry_run: bool
    response: SingletonRefactorResponse


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def address_to_function_name(address: str) -> str:
    name: list[str] = []
    prev_underscore = False
    for character in address.lower():
        if character == "'":
            continue
        if "a" <= character <= "z" or "0" <= character <= "9":
            name.append(character)
            prev_underscore = False
        elif not prev_underscore:
            name.append("_")
            prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{base}"


def _resolved_projection_layout(
    layout: ProjectionColumnLayout | None = None,
) -> ProjectionColumnLayout | None:
    if layout is not None:
        return layout
    try:
        return active_projection_layout()
    except RuntimeError:
        return None


def _member_engine_column(
    address: str,
    layout: ProjectionColumnLayout | None,
) -> str | None:
    if layout is not None:
        column = layout.engine_column_for_address(address)
        if column is not None:
            return column
    _, column, _row = parse_workbook_address(address)
    return column


def _engine_column_for_time_period(
    time_period: int,
    layout: ProjectionColumnLayout | None,
) -> str | None:
    if layout is None:
        return None
    return layout.time_period_to_engine_column.get(time_period)


def _time_period_for_engine_column(
    column: str,
    layout: ProjectionColumnLayout | None,
) -> int | None:
    if layout is None:
        return None
    return layout.time_period_for_engine_column(column)


def _default_bound_address_keys() -> dict[str, dict[str, BindingKeyValue]]:
    """Fallback that rebuilds the pipeline graph solely to derive bound keys.

    Prefer passing ``bound_address_keys`` (and ``source_graph``) from the caller
    that already ran ``build_pipeline_graph``; otherwise a warm pipeline pays a
    second dependency-graph load plus ``derive_*_series`` work.
    """
    from src.extraction_pipeline import build_pipeline_graph
    from src.pipeline_context import require_pipeline_config

    graph_result = build_pipeline_graph(require_pipeline_config())
    return build_bound_address_keys(
        graph_result.input_series,
        graph_result.output_series,
        graph_result.internal_series,
    )


def _binding_hints_for_address(
    internal_binding_index: InternalBindingIndex | None,
    address: str,
) -> BindingRecordHints:
    if internal_binding_index is None:
        return BindingRecordHints()
    binding = internal_binding_for_address(internal_binding_index, address)
    if binding is None:
        return BindingRecordHints()
    return binding_record_hints_from_cell(
        {
            "key": binding.key,
            "record": binding.record,
        }
    )


def _default_source_graph() -> DependencyGraph | None:
    return None


def _default_key_vocabulary(bindings_path: Path) -> tuple[KeyConceptSpec, ...]:
    return load_key_concept_vocabulary(bindings_path)


def build_cluster_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None,
    key_vocabulary: tuple[KeyConceptSpec, ...] | None = None,
    workbook_path: Path,
    bindings_path: Path,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    layout: ProjectionColumnLayout | None = None,
    internals_index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    expected_helper_name: str | None = None,
    existing_helper_names: frozenset[str] | None = None,
) -> ClusterRefactorContext | None:
    if len(cluster.members) < 2:
        return None

    resolved_layout = _resolved_projection_layout(layout)

    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    defined_functions = index.functions

    member_addresses = frozenset(cluster.members)
    member_functions = {
        address_to_function_name(address) for address in member_addresses
    }

    members: list[MemberContext] = []
    for address in cluster.members:
        function_name = address_to_function_name(address)
        if function_name not in defined_functions:
            continue

        engine_column = _member_engine_column(address, resolved_layout)
        if engine_column is None:
            continue

        node = projection.get_node(address)
        if node is None or node.normalized_formula is None:
            continue

        dependency_addresses = tuple(sorted(projection.get_dependencies(address)))
        cluster_address_set = set(cluster.members)
        dependency_functions = tuple(
            sorted(
                address_to_function_name(dependency)
                for dependency in dependency_addresses
                if dependency in cluster_address_set
            )
        )

        binding_hints = _binding_hints_for_address(internal_binding_index, address)

        members.append(
            MemberContext(
                address=address,
                function_name=function_name,
                engine_column=engine_column,
                normalized_formula=node.normalized_formula,
                python_source=index.function_source(function_name),
                dependency_addresses=dependency_addresses,
                dependency_functions=dependency_functions,
                binding_keys=binding_hints.binding_keys,
                binding_record=binding_hints.binding_record,
            )
        )

    if len(members) < 2:
        return None

    resolved_bound_keys = (
        bound_address_keys
        if bound_address_keys is not None
        else _default_bound_address_keys()
    )
    resolved_vocabulary = (
        key_vocabulary
        if key_vocabulary is not None
        else _default_key_vocabulary(bindings_path)
    )
    member_address_list = tuple(member.address for member in members)
    expected_member_keys = expected_member_keys_for_cluster(
        member_address_list,
        bound_address_keys=resolved_bound_keys,
        workbook_path=workbook_path,
        layout=resolved_layout,
    )
    varying_dimension_ids = frozenset(
        dimension_id for keys in expected_member_keys.values() for dimension_id in keys
    )
    contract = select_cluster_refactor_contract(
        replace(cluster, members=member_address_list),
        {member.address: member.normalized_formula for member in members},
        resolved_bound_keys,
        varying_dimension_ids,
        key_vocabulary=resolved_vocabulary,
        workbook_path=workbook_path,
        layout=resolved_layout,
    )
    if contract is None:
        logger.warning(
            "cluster %s skipped: operand-level variation is not routable by the "
            "declared binding dimension ids (operand_level_variation_unsupported)",
            cluster.cluster_id,
        )
        return None

    external_dependency_addresses = sorted(
        {
            dependency
            for member in members
            for dependency in member.dependency_addresses
            if dependency not in member_addresses
        }
    )
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source, external_dependency_addresses, index=index
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    semantic_refs = tuple(
        SemanticDependencyRef(
            helper_name=dependency.helper_name,
            call_form=dependency.call_form,
            address_template=dependency.address_template,
            addresses=dependency.addresses,
        )
        for dependency in semantic_dependencies
    )
    fingerprint_summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected_member_keys,
        bound_address_keys=resolved_bound_keys,
        workbook_path=workbook_path,
        layout=resolved_layout,
        address_to_series_id=address_to_series_id,
        semantic_dependencies=semantic_refs,
    )
    if fingerprint_summary.fallback_reason is not None:
        _record_fingerprint_fallback(
            fingerprint_summary.fallback_reason,
            cluster_id=cluster.cluster_id,
        )

    if expected_helper_name is None:
        if address_to_series_id is None:
            raise ValueError(
                "address_to_series_id is required to lock cluster helper names"
            )
        expected_helper_name = sole_series_id_for_addresses(
            member_address_list,
            address_to_series_id,
        )
    reserved_names = (
        existing_helper_names if existing_helper_names is not None else frozenset()
    )
    validate_semantic_identifier(
        expected_helper_name,
        existing_names=reserved_names - {expected_helper_name},
    )

    return ClusterRefactorContext(
        cluster_id=cluster.cluster_id,
        canonical_template=cluster.canonical_template,
        row=cluster.row,
        members=tuple(members),
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(
            source, member_addresses, member_functions, index=index
        ),
        first_year_column=(
            resolved_layout.engine_columns[0]
            if resolved_layout is not None and resolved_layout.engine_columns
            else members[0].engine_column
        ),
        allowed_runtime_symbols=allowed_runtime_symbols(),
        key_vocabulary=resolved_vocabulary,
        expected_member_keys=expected_member_keys,
        naming_hints=cluster_binding_naming_hints(
            tuple(
                BindingRecordHints(
                    binding_keys=member.binding_keys,
                    binding_record=member.binding_record,
                )
                for member in members
            )
        ),
        expected_helper_name=expected_helper_name,
        contract=contract,
        fingerprint_summary=fingerprint_summary,
    )


def build_singleton_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    internals_index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    expected_helper_name: str | None = None,
    existing_helper_names: frozenset[str] | None = None,
) -> SingletonRefactorContext | None:
    if len(cluster.members) != 1:
        return None

    address = cluster.members[0]

    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    defined_functions = index.functions

    function_name = address_to_function_name(address)
    if function_name not in defined_functions:
        return None

    node = projection.get_node(address)
    if node is None or node.normalized_formula is None:
        return None

    dependency_addresses = tuple(sorted(projection.get_dependencies(address)))
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source, dependency_addresses, index=index
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    if expected_helper_name is None:
        if address_to_series_id is None:
            raise ValueError(
                "address_to_series_id is required to lock singleton helper names"
            )
        expected_helper_name = sole_series_id_for_addresses(
            (address,),
            address_to_series_id,
        )
    reserved_names = (
        existing_helper_names if existing_helper_names is not None else frozenset()
    )
    validate_semantic_identifier(
        expected_helper_name,
        existing_names=reserved_names - {expected_helper_name},
    )

    return SingletonRefactorContext(
        address=address,
        function_name=function_name,
        canonical_template=cluster.canonical_template,
        normalized_formula=node.normalized_formula,
        python_source=index.function_source(function_name),
        dependency_addresses=dependency_addresses,
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(
            source,
            frozenset({address}),
            {function_name},
            index=index,
        ),
        allowed_runtime_symbols=allowed_runtime_symbols(),
        naming_hints=_binding_hints_for_address(
            internal_binding_index, address
        ).to_payload(),
        expected_helper_name=expected_helper_name,
    )


@dataclass(frozen=True)
class InternalsSourceIndex:
    """Parse-once view of ``internals.py`` for refactor context construction."""

    source: str
    module: ast.Module
    lines_keepends: tuple[str, ...]
    lines: tuple[str, ...]
    functions: Mapping[str, ast.FunctionDef]
    semantic_helper_names: frozenset[str]
    address_dispatch: AddressDispatch
    symbol_dispatch: Mapping[str, str]

    @classmethod
    def from_source(cls, source: str) -> InternalsSourceIndex:
        module = ast.parse(source)
        functions = {
            node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
        }
        return cls(
            source=source,
            module=module,
            lines_keepends=tuple(source.splitlines(keepends=True)),
            lines=tuple(source.splitlines()),
            functions=functions,
            semantic_helper_names=frozenset(
                name
                for name, node in functions.items()
                if _is_semantic_helper_def(node)
            ),
            address_dispatch=_parse_address_dispatch(source, module=module) or {},
            symbol_dispatch=_parse_symbol_dispatch(source, module=module),
        )

    def function_source(self, function_name: str) -> str:
        node = self.functions.get(function_name)
        if node is None:
            raise KeyError(f"Function {function_name!r} not found in internals source")
        return "".join(self.lines_keepends[node.lineno - 1 : node.end_lineno])


def extract_function_source(source: str, function_name: str) -> str:
    return InternalsSourceIndex.from_source(source).function_source(function_name)


def _resolve_internals_index(
    internals_path: Path,
    *,
    internals_index: InternalsSourceIndex | None = None,
) -> InternalsSourceIndex:
    if internals_index is not None:
        return internals_index
    return InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))


def scan_call_sites(
    source: str,
    member_addresses: frozenset[str],
    member_functions: set[str],
    *,
    index: InternalsSourceIndex | None = None,
) -> tuple[CallSite, ...]:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    sites: list[CallSite] = []
    lines = list(resolved.lines)

    for top_level in resolved.module.body:
        if not isinstance(top_level, ast.FunctionDef):
            continue
        caller_function = top_level.name
        caller_address = _caller_address(caller_function)
        visitor = _CallSiteVisitor(
            caller_function=caller_function,
            caller_address=caller_address,
            member_addresses=member_addresses,
            member_functions=member_functions,
            lines=lines,
            sites=sites,
        )
        visitor.visit(top_level)

    return tuple(sorted(sites, key=lambda site: (site.line, site.callee_address)))


def _normalize_internals_bytes(internals_bytes: bytes) -> bytes:
    """Normalize line endings so cache keys are platform-independent."""
    return internals_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def refactor_cache_key(
    ctx: ClusterRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "model": refactor_model(),
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "contract": ctx.contract,
        "cluster_id": ctx.cluster_id,
        "canonical_template": ctx.canonical_template,
        "members": [
            {
                "address": member.address,
                "function_name": member.function_name,
                "formula_sha256": hashlib.sha256(
                    member.normalized_formula.encode()
                ).hexdigest(),
                "source_sha256": hashlib.sha256(
                    member.python_source.encode()
                ).hexdigest(),
            }
            for member in ctx.members
        ],
        "internals_sha256": hashlib.sha256(
            _normalize_internals_bytes(internals_bytes)
        ).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            stable_json(response_schema).encode()
        ).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def prompt_payload(ctx: ClusterRefactorContext) -> dict[str, object]:
    varying_dimension_ids = frozenset(
        dimension_id
        for keys in ctx.expected_member_keys.values()
        for dimension_id in keys
    )
    parameter_names = [
        item.suggested_param_name
        for item in ctx.key_vocabulary
        if item.dimension_id in varying_dimension_ids
    ]
    return {
        "cluster_id": ctx.cluster_id,
        "row": ctx.row,
        "canonical_template": ctx.canonical_template,
        "first_year_column": ctx.first_year_column,
        "key_vocabulary": [
            {
                "dimension_id": item.dimension_id,
                "concept": item.concept,
                "dtype": item.dtype,
                "suggested_param_name": item.suggested_param_name,
            }
            for item in ctx.key_vocabulary
        ],
        "members": [
            {
                "address": member.address,
                "function_name": member.function_name,
                "engine_column": member.engine_column,
                "expected_keys": ctx.expected_member_keys.get(member.address, {}),
                "normalized_formula": member.normalized_formula,
                "python_source": member.python_source,
                "dependency_addresses": member.dependency_addresses,
                "dependency_functions": member.dependency_functions,
                "binding_keys": member.binding_keys or {},
                "binding_record": member.binding_record or {},
            }
            for member in ctx.members
        ],
        "external_dependencies": list(ctx.external_dependencies),
        "semantic_dependencies": [
            {
                "address_template": dependency.address_template,
                "columns": list(dependency.columns),
                "helper_name": dependency.helper_name,
                "call_form": dependency.call_form,
            }
            for dependency in ctx.semantic_dependencies
        ],
        "call_sites": [
            {
                "caller_function": site.caller_function,
                "caller_address": site.caller_address,
                "callee_function": site.callee_function,
                "callee_address": site.callee_address,
                "pattern": site.pattern,
                "line": site.line,
                "snippet": site.snippet,
            }
            for site in ctx.call_sites
        ],
        "constraints": {
            "allowed_runtime_symbols": list(ctx.allowed_runtime_symbols),
            "parameters": parameter_names,
            "signature": f"(ctx, {', '.join(parameter_names)})",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "naming_hints": ctx.naming_hints,
        "expected_helper_name": ctx.expected_helper_name,
    }


def load_refactor_cache() -> dict[str, str]:
    if not REFACTOR_CACHE_PATH.exists():
        return {}
    return json.loads(REFACTOR_CACHE_PATH.read_text(encoding="utf-8"))


def save_refactor_cache(cache: dict[str, str]) -> None:
    REFACTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFACTOR_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def validate_google_style_docstring(docstring: str) -> None:
    text = docstring.strip()
    if not text:
        raise ValueError("helper docstring must be non-empty")
    if "Args:" not in text:
        raise ValueError("helper docstring must include an Args section")
    if "Returns:" not in text:
        raise ValueError("helper docstring must include a Returns section")
    lines = text.splitlines()
    if not lines[0].strip():
        raise ValueError("helper docstring must begin with a one-line summary")
    remainder_start = 1
    while remainder_start < len(lines) and not lines[remainder_start].strip():
        remainder_start += 1
    if remainder_start < len(lines):
        first_rest = lines[remainder_start].strip()
        if not first_rest.startswith(("Args:", "Returns:", "Note:")):
            raise ValueError(
                "helper docstring summary must be followed by Args, Returns, or Note"
            )


_GOOGLE_DOCSTRING_SECTION_HEADER = re.compile(r"^(Args:|Returns:|Note:)\s*$")


def _normalize_google_docstring(docstring: str) -> str:
    text = textwrap.dedent(docstring).strip()
    if not text:
        return text
    lines = text.splitlines()
    summary = lines[0].strip()
    first_section_idx: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if _GOOGLE_DOCSTRING_SECTION_HEADER.match(line.strip()):
            first_section_idx = index
            break
    if first_section_idx is None:
        return text
    normalized_lines = [summary, "", *lines[first_section_idx:]]
    return "\n".join(normalized_lines).rstrip() + "\n"


def _patch_function_docstring_in_source(source: str, docstring: str) -> str:
    module = ast.parse(source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        return source
    function_def = function_defs[0]
    if not function_def.body:
        function_def.body.insert(0, ast.Expr(value=ast.Constant(value=docstring)))
        return ast.unparse(module) + "\n"
    first = function_def.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            first.value.value = docstring
            return ast.unparse(module) + "\n"
    function_def.body.insert(0, ast.Expr(value=ast.Constant(value=docstring)))
    return ast.unparse(module) + "\n"


def _normalize_singleton_response_docstring(
    response: SingletonRefactorResponse,
) -> SingletonRefactorResponse:
    symbol_def = _single_function_def(response.symbol_source)
    if symbol_def is None:
        return response
    source_docstring = _function_docstring(symbol_def)
    if source_docstring is None:
        return response
    normalized = _normalize_google_docstring(source_docstring)
    if normalized == source_docstring:
        return response
    patched_source = _patch_function_docstring_in_source(
        response.symbol_source,
        normalized,
    )
    return response.model_copy(
        update={
            "symbol_source": patched_source,
            "symbol_docstring": normalized,
        }
    )


def _normalize_cluster_response_docstring(
    response: ClusterRefactorResponse,
) -> ClusterRefactorResponse:
    helper_def = _single_function_def(response.helper_source)
    if helper_def is None:
        return response
    source_docstring = _function_docstring(helper_def)
    if source_docstring is None:
        return response
    normalized = _normalize_google_docstring(source_docstring)
    if normalized == source_docstring:
        return response
    patched_source = _patch_function_docstring_in_source(
        response.helper_source,
        normalized,
    )
    return response.model_copy(
        update={
            "helper_source": patched_source,
            "helper_docstring": normalized,
        }
    )


def _function_docstring(function_def: ast.FunctionDef) -> str | None:
    if not function_def.body:
        return None
    first = function_def.body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _single_function_def(source: str) -> ast.FunctionDef | None:
    module = ast.parse(source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        return None
    return function_defs[0]


def _align_cluster_response_docstring(
    response: ClusterRefactorResponse,
) -> ClusterRefactorResponse:
    helper_def = _single_function_def(response.helper_source)
    if helper_def is None:
        return response
    source_docstring = _function_docstring(helper_def)
    if source_docstring is None or source_docstring == response.helper_docstring:
        return response
    return response.model_copy(update={"helper_docstring": source_docstring})


def _align_singleton_response_docstring(
    response: SingletonRefactorResponse,
) -> SingletonRefactorResponse:
    symbol_def = _single_function_def(response.symbol_source)
    if symbol_def is None:
        return response
    source_docstring = _function_docstring(symbol_def)
    if source_docstring is None or source_docstring == response.symbol_docstring:
        return response
    return response.model_copy(update={"symbol_docstring": source_docstring})


def _normalize_member_key_concepts(
    response: ClusterRefactorResponse,
    *,
    key_vocabulary: tuple[KeyConceptSpec, ...],
) -> ClusterRefactorResponse:
    normalized_parameters: list[HelperParameter] = []
    for parameter in response.parameters:
        dimension_id = resolve_dimension_key(parameter.dimension_id, key_vocabulary)
        vocab_entry = next(
            item for item in key_vocabulary if item.dimension_id == dimension_id
        )
        if parameter.concept is not None and parameter.concept != vocab_entry.concept:
            raise ValueError(
                f"parameter {parameter.name!r} concept {parameter.concept!r} "
                f"does not match vocabulary concept {vocab_entry.concept!r} "
                f"for dimension_id {dimension_id!r}"
            )
        normalized_parameters.append(
            parameter.model_copy(
                update={
                    "dimension_id": dimension_id,
                    "concept": vocab_entry.concept,
                }
            )
        )
    dimension_id_by_name = {
        parameter.name: parameter.dimension_id for parameter in normalized_parameters
    }
    normalized_entries: list[MemberKeys] = []
    for entry in response.member_keys:
        normalized_entries_list: list[MemberKeyEntry] = []
        for key_entry in entry.keys:
            raw_key = dimension_id_by_name.get(
                key_entry.dimension_id, key_entry.dimension_id
            )
            dimension_id = resolve_dimension_key(raw_key, key_vocabulary)
            normalized_entries_list.append(
                MemberKeyEntry(dimension_id=dimension_id, value=key_entry.value)
            )
        normalized_entries.append(
            entry.model_copy(update={"keys": tuple(normalized_entries_list)})
        )
    return response.model_copy(
        update={
            "parameters": tuple(normalized_parameters),
            "member_keys": tuple(normalized_entries),
        }
    )


def _prepare_cluster_refactor_response(
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> ClusterRefactorResponse:
    response = _normalize_member_key_concepts(
        response, key_vocabulary=ctx.key_vocabulary
    )
    response = _align_cluster_response_docstring(response)
    return _normalize_cluster_response_docstring(response)


def _prepare_singleton_refactor_response(
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> SingletonRefactorResponse:
    _ = ctx
    response = _align_singleton_response_docstring(response)
    return _normalize_singleton_response_docstring(response)


EXCEL_SHAPED_LOCAL_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^_t\d+$"),
    re.compile(r"^t\d+$"),
    re.compile(r"^b\d+$"),
    re.compile(r"^col\d+$"),
    re.compile(r"^choose\d+$"),
    re.compile(r"^chooser\d+$"),
    re.compile(r"^func_map$"),
    re.compile(r"^input_?\d+$"),
    re.compile(r"^term\d+"),
)


def _names_from_target(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_names_from_target(element))
        return names
    return set()


def _argument_names(args: ast.arguments) -> set[str]:
    names = {arg.arg for arg in args.posonlyargs}
    names.update(arg.arg for arg in args.args)
    names.update(arg.arg for arg in args.kwonlyargs)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _local_binding_names(function_def: ast.FunctionDef) -> set[str]:
    parameter_names = _argument_names(function_def.args)
    names: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_from_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.NamedExpr):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.AugAssign):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.For):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.comprehension):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.Lambda):
            names.update(_argument_names(node.args))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node is function_def:
                continue
            names.add(node.name)
            names.update(_argument_names(node.args))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names.update(_names_from_target(node.optional_vars))
    return names - parameter_names


def is_excel_shaped_local_name(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in EXCEL_SHAPED_LOCAL_NAME_PATTERNS)


def validate_semantic_local_names(function_def: ast.FunctionDef) -> None:
    excel_shaped = sorted(
        name
        for name in _local_binding_names(function_def)
        if is_excel_shaped_local_name(name)
    )
    if excel_shaped:
        raise ValueError(
            "function body uses excel-shaped local names "
            f"{excel_shaped}; use domain-meaningful snake_case instead"
        )


def validate_no_cell_function_references(function_def: ast.FunctionDef) -> None:
    cell_references = sorted(
        {
            node.id
            for node in ast.walk(function_def)
            if isinstance(node, ast.Name) and node.id.startswith("cell_")
        }
    )
    if cell_references:
        raise ValueError(
            "function body must not reference excel cell helpers "
            f"{cell_references}; use semantic helpers from upstream refactors"
        )


def _suggested_param_name_by_dimension_id(
    key_vocabulary: tuple[KeyConceptSpec, ...],
) -> dict[str, str]:
    return {item.dimension_id: item.suggested_param_name for item in key_vocabulary}


def validate_parameter_names_match_vocabulary(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
) -> None:
    suggested = _suggested_param_name_by_dimension_id(ctx.key_vocabulary)
    mismatches = sorted(
        {
            f"{parameter.dimension_id!r}: expected "
            f"{suggested[parameter.dimension_id]!r}, "
            f"got {parameter.name!r}"
            for parameter in response.parameters
            if parameter.dimension_id in suggested
            and parameter.name != suggested[parameter.dimension_id]
        }
    )
    if mismatches:
        raise ValueError(
            "parameter names must match suggested_param_name from key_vocabulary: "
            + "; ".join(mismatches)
        )


def validate_allowed_global_references(
    function_def: ast.FunctionDef,
    *,
    allowed_names: set[str],
) -> None:
    parameter_names = _argument_names(function_def.args)
    local_names = _local_binding_names(function_def) | parameter_names
    builtin_names = set(dir(builtins))
    disallowed: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if (
                node.id in local_names
                or node.id in builtin_names
                or node.id in allowed_names
            ):
                continue
            disallowed.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            root = node.value.id
            if root in local_names or root in allowed_names or root in builtin_names:
                continue
            disallowed.add(root)
    if disallowed:
        raise ValueError(
            "helper_source references disallowed global names "
            f"{sorted(disallowed)}; allowed: {sorted(allowed_names)}"
        )


def _without_cell_function_names(names: set[str]) -> set[str]:
    """Drop ``cell_*`` names from refactor allowlists.

    Helper bodies must not reference excel cell wrappers
    (``validate_no_cell_function_references``), so including those names in the
    allowlist only inflates validation errors for large range dependencies.
    """
    return {name for name in names if not name.startswith("cell_")}


def validate_cluster_refactor_response(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
) -> None:
    if response.helper_name != ctx.expected_helper_name:
        raise ValueError(
            "helper_name must equal locked helper name "
            f"{ctx.expected_helper_name!r}, got {response.helper_name!r}"
        )
    validate_semantic_identifier(
        response.helper_name,
        existing_names=existing_names,
        allow_name=ctx.expected_helper_name,
    )

    member_key_addresses = {entry.address for entry in response.member_keys}
    member_addresses = {member.address for member in ctx.members}
    if member_key_addresses != member_addresses:
        raise ValueError(
            "member_keys must cover cluster members exactly: "
            f"expected {sorted(member_addresses)}, got {sorted(member_key_addresses)}"
        )

    parameter_dimension_ids = {
        parameter.dimension_id for parameter in response.parameters
    }
    vocabulary_dimension_ids = {item.dimension_id for item in ctx.key_vocabulary}
    unknown_parameters = sorted(parameter_dimension_ids - vocabulary_dimension_ids)
    if unknown_parameters:
        raise ValueError(
            f"parameters reference unknown binding dimensions: {unknown_parameters}"
        )

    parameter_names = [parameter.name for parameter in response.parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ValueError("parameter names must be unique")
    for parameter in response.parameters:
        if not parameter.name.isidentifier():
            raise ValueError(
                f"parameter name is not a valid identifier: {parameter.name!r}"
            )

    dimension_sets = [
        frozenset(ctx.expected_member_keys[member.address].keys())
        for member in ctx.members
    ]
    if len(set(dimension_sets)) != 1:
        raise ValueError("cluster members must share one varying key dimension set")
    varying_dimension_ids = dimension_sets[0]
    if ctx.contract == "dimension_aware":
        collapsed = [
            f"concept {concept!r} requires one parameter per dimension id "
            f"{sorted(dimension_ids)}"
            for concept, dimension_ids in sorted(
                concepts_with_multiple_dimensions(
                    varying_dimension_ids, ctx.key_vocabulary
                ).items()
            )
            if not set(dimension_ids) <= parameter_dimension_ids
        ]
        if collapsed:
            raise ValueError(
                "dimension-aware response collapses distinct dimensions onto one "
                "concept parameter: " + "; ".join(collapsed)
            )
    if parameter_dimension_ids != varying_dimension_ids:
        raise ValueError(
            "parameters must match varying binding key dimensions for the cluster: "
            f"expected {sorted(varying_dimension_ids)}, "
            f"got {sorted(parameter_dimension_ids)}"
        )

    seen_member_key_combinations: set[tuple[tuple[str, BindingKeyValue], ...]] = set()
    for entry in response.member_keys:
        member = next(item for item in ctx.members if item.address == entry.address)
        if entry.function_name != member.function_name:
            raise ValueError(
                f"member_keys for {entry.address} has function_name "
                f"{entry.function_name!r}, expected {member.function_name!r}"
            )
        entry_keys = entry.keys_dict()
        extra_dimensions = set(entry_keys) - parameter_dimension_ids
        if extra_dimensions:
            raise ValueError(
                f"member_keys for {entry.address} must not include "
                f"series-constant dimensions: {sorted(extra_dimensions)}"
            )
        missing_dimensions = parameter_dimension_ids - set(entry_keys)
        if missing_dimensions:
            raise ValueError(
                f"member_keys for {entry.address} missing parameter dimensions: "
                f"{sorted(missing_dimensions)}"
            )
        key_combination = tuple(sorted(entry_keys.items()))
        # Possibly this expectation should be changed.
        # There may be times when two cell formulas are functionally identical
        # or can be represented with identical semantics, so unique cell-wise
        # triangulation is not required to route to the correct semantics.
        if key_combination in seen_member_key_combinations:
            raise ValueError(
                "member_keys must have a unique key combination for each member"
            )
        seen_member_key_combinations.add(key_combination)
        expected_keys = ctx.expected_member_keys[entry.address]
        for dimension_id, expected_value in expected_keys.items():
            actual_value = entry_keys.get(dimension_id)
            if actual_value != expected_value:
                raise ValueError(
                    f"member_keys for {entry.address} has "
                    f"{dimension_id}={actual_value!r}, "
                    f"expected {expected_value!r}"
                )
        layout = _resolved_projection_layout()
        if layout is not None:
            engine_column = engine_column_from_member_keys(
                entry_keys,
                address=entry.address,
                layout=layout,
            )
            if engine_column is None:
                raise ValueError(
                    f"member_keys for {entry.address} do not resolve "
                    "to an engine column"
                )
            if engine_column != member.engine_column:
                raise ValueError(
                    f"member_keys for {entry.address} resolve to engine column "
                    f"{engine_column!r}, expected {member.engine_column!r}"
                )

    if not response.helper_name.isidentifier():
        raise ValueError(
            f"helper_name is not a valid identifier: {response.helper_name!r}"
        )

    helper_module = ast.parse(response.helper_source)
    function_defs = [
        node for node in helper_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(function_defs) != 1:
        raise ValueError("helper_source must contain exactly one FunctionDef")
    helper_def = function_defs[0]
    if helper_def.name != response.helper_name:
        raise ValueError(
            "helper_source function name must match helper_name "
            f"({response.helper_name!r})"
        )

    source_docstring = _function_docstring(helper_def)
    if source_docstring is None:
        raise ValueError("helper_source must include a docstring")
    validate_google_style_docstring(source_docstring)
    if source_docstring != response.helper_docstring:
        raise ValueError(
            "helper_docstring must match the docstring embedded in helper_source"
        )

    validate_semantic_local_names(helper_def)
    validate_no_cell_function_references(helper_def)
    validate_parameter_names_match_vocabulary(ctx, response)

    arg_names = [arg.arg for arg in helper_def.args.args]
    expected_args = ["ctx", *[parameter.name for parameter in response.parameters]]
    if arg_names != expected_args:
        raise ValueError(
            f"helper must accept exactly ({', '.join(expected_args)}); "
            f"got ({', '.join(arg_names)})"
        )

    if any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.walk(helper_module)
    ):
        raise ValueError("helper_source must not contain imports")

    allowed_names = _without_cell_function_names(
        set(ctx.allowed_runtime_symbols)
        | set(ctx.external_dependencies)
        | {member.function_name for member in ctx.members}
        | {response.helper_name}
        | semantic_helpers_available_for_calls(internals_source, existing_names)
        | set(ALLOWED_REFACTOR_TYPE_HINT_NAMES)
        | {"ctx"}
        | {parameter.name for parameter in response.parameters}
    )
    builtin_names = set(dir(builtins))
    for node in ast.walk(helper_def):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in builtin_names or name in allowed_names:
            continue
        raise ValueError(
            f"helper_source calls disallowed function {name!r}; "
            f"allowed: {sorted(allowed_names)}"
        )

    validate_allowed_global_references(helper_def, allowed_names=allowed_names)


def singleton_refactor_cache_key(
    ctx: SingletonRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "kind": "singleton",
        "model": refactor_model(),
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "address": ctx.address,
        "canonical_template": ctx.canonical_template,
        "formula_sha256": hashlib.sha256(ctx.normalized_formula.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(ctx.python_source.encode()).hexdigest(),
        "internals_sha256": hashlib.sha256(
            _normalize_internals_bytes(internals_bytes)
        ).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            stable_json(response_schema).encode()
        ).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def load_singleton_refactor_prompt_fixed_portion() -> str:
    return SINGLETON_REFACTOR_PROMPT_FIXTURE.read_text(encoding="utf-8")


def strip_python_string_delimiters(docstring: str) -> str:
    stripped = docstring.strip()
    for quote in ('"""', "'''"):
        if stripped.startswith(quote) and stripped.endswith(quote):
            inner = stripped[len(quote) : -len(quote)]
            if inner.startswith("\n"):
                inner = inner[1:]
            return inner.rstrip("\n")
    return docstring


def append_refactor_note_section(
    docstring: str,
    *,
    address: str,
    formula: str,
) -> str:
    return f"{docstring.rstrip()}\n\nNote:\n    Covers {address}. Excel: {formula}."


def assemble_singleton_symbol_source(
    *,
    signature: str,
    docstring: str,
    body: str,
) -> str:
    normalized = textwrap.dedent(docstring).strip()
    if not normalized:
        raise ValueError("docstring must not be empty")
    body_block = "\n".join(f"    {line}" for line in body.splitlines()) + "\n"
    return f'{signature}\n    """{normalized}\n    """\n{body_block}'


def inject_signature_return_type_hint(signature: str, return_hint: str) -> str:
    """Replace or append an allowlisted return hint on a function signature line."""
    stripped = signature.strip()
    without_return = re.sub(r"\s*->\s*.+$", "", stripped).rstrip(":").rstrip()
    return f"{without_return} -> {return_hint}:"


def validate_singleton_return_type_hint(hint: str) -> None:
    validate_scalar_return_type_hint(hint)


def _parse_symbol_name_from_signature(signature: str) -> str:
    match = re.match(r"def\s+(\w+)\s*\(", signature.strip())
    if match is None:
        raise ValueError(f"invalid function signature: {signature!r}")
    return match.group(1)


def build_locked_helper_signature(
    helper_name: str,
    *,
    parameters: Sequence[HelperParameter] = (),
) -> str:
    """Build ``def {helper_name}(ctx: EvalContext, ...)`` from locked name + params."""
    parts = ["ctx: EvalContext"]
    for parameter in parameters:
        python_type = _binding_dtype_to_python(parameter.dtype) or parameter.dtype
        parts.append(f"{parameter.name}: {python_type}")
    return f"def {helper_name}({', '.join(parts)}):"


def prepare_singleton_refactor_response(
    llm_response: SingletonRefactorLLMResponse,
    ctx: SingletonRefactorContext,
    *,
    runtime_source: str,
    internals_source: str,
) -> SingletonRefactorResponse:
    if llm_response.symbol_docstring is None or llm_response.symbol_body is None:
        raise ValueError(
            "singleton refactor response is missing required success fields"
        )
    return_hint = infer_refactor_return_type_hint(
        python_sources=(ctx.python_source,),
        runtime_source=runtime_source,
        internals_source=internals_source,
        naming_hints=ctx.naming_hints,
    )
    signature = inject_signature_return_type_hint(
        build_locked_helper_signature(ctx.expected_helper_name),
        return_hint,
    )
    docstring = strip_python_string_delimiters(llm_response.symbol_docstring)
    docstring = append_refactor_note_section(
        docstring,
        address=ctx.address,
        formula=ctx.normalized_formula,
    )
    symbol_source = assemble_singleton_symbol_source(
        signature=signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    return SingletonRefactorResponse(
        symbol_name=ctx.expected_helper_name,
        symbol_docstring=docstring,
        symbol_source=symbol_source,
    )


def _format_binding_map_yaml(
    label: str,
    values: object,
) -> list[str]:
    if not isinstance(values, Mapping) or not values:
        return [f"{label}: {{}}"]
    lines = [f"{label}:"]
    for concept, value in sorted(values.items(), key=lambda item: item[0]):
        lines.append(f"  {concept}: {_yaml_scalar(value)}")
    return lines


def _format_cell_metadata_yaml(cell_metadata: Mapping[str, object]) -> str:
    lines = [f"address: {cell_metadata['address']}"]
    lines.extend(
        _format_binding_map_yaml("binding_keys", cell_metadata.get("binding_keys"))
    )
    lines.extend(
        _format_binding_map_yaml("binding_record", cell_metadata.get("binding_record"))
    )
    return "\n".join(lines)


def format_singleton_refactor_context_dump(
    *,
    function_source: str,
    cell_metadata: Mapping[str, object],
    dependency_stubs: str,
    helper_name: str | None = None,
) -> str:
    yaml_block = _format_cell_metadata_yaml(cell_metadata)
    header = "Function to refactor:\n\n"
    if helper_name is not None:
        header = f"Function to refactor (helper_name={helper_name}):\n\n"
    return (
        f"{header}"
        f"```python\n{function_source.strip()}\n```\n\n"
        "Cell metadata:\n\n"
        f"```yaml\n{yaml_block}\n```\n\n"
        "Dependencies:\n\n"
        f"```python\n{dependency_stubs.strip()}\n```"
    )


def _function_defs_by_name(
    source: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> dict[str, ast.FunctionDef]:
    if index is not None:
        return dict(index.functions)
    module = ast.parse(source)
    return {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }


def _called_function_names(function_source: str) -> tuple[str, ...]:
    module = ast.parse(function_source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        raise ValueError("function_source must contain exactly one FunctionDef")
    function_def = function_defs[0]
    names: list[str] = []
    for node in ast.walk(function_def):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    return tuple(dict.fromkeys(names))


def _strip_note_section(docstring: str) -> str:
    dedented = textwrap.dedent(docstring).strip()
    for pattern in (r"\n\nNote:\n.*\Z", r"\nNote:\n.*\Z"):
        match = re.search(pattern, dedented, re.DOTALL)
        if match is not None:
            return dedented[: match.start()].rstrip()
    return dedented


def _llm_dependency_return_suffix(function_def: ast.FunctionDef) -> str:
    known = KNOWN_RUNTIME_RETURN_HINTS.get(function_def.name)
    if known is not None:
        return f" -> {known}"
    if function_def.returns is None:
        return ""
    hint = ast.unparse(function_def.returns).strip()
    normalized = normalize_return_type_hint_for_allowlist(hint)
    if normalized is not None:
        return f" -> {normalized}"
    return ""


def _llm_dependency_signature_line(function_def: ast.FunctionDef) -> str:
    args = ast.unparse(function_def.args)
    return_suffix = _llm_dependency_return_suffix(function_def)
    return f"def {function_def.name}({args}){return_suffix}:"


def _format_runtime_dependency_stub(function_def: ast.FunctionDef) -> str:
    docstring = _function_docstring(function_def)
    lines = [_llm_dependency_signature_line(function_def)]
    if docstring is not None:
        if "\n" in docstring:
            compact = " ".join(docstring.split())
            lines.append(f'    """{compact}"""')
        else:
            lines.append(f'    """{docstring}"""')
    lines.append("    # ...")
    return "\n".join(lines)


def _format_semantic_dependency_stub(function_def: ast.FunctionDef) -> str:
    docstring = _function_docstring(function_def)
    lines = [f"def {function_def.name}(ctx: EvalContext) -> float:"]
    if docstring is not None:
        trimmed = _strip_note_section(docstring)
        lines.append('    """')
        for line in trimmed.splitlines():
            lines.append(f"    {line}" if line else "")
        lines.append('    """')
    lines.append("    # ...")
    return "\n".join(lines)


def _build_dependency_stubs(
    *,
    function_source: str,
    internals_source: str,
    runtime_source: str,
    index: InternalsSourceIndex | None = None,
) -> str:
    called = _called_function_names(function_source)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = _function_defs_by_name(internals_source, index=index)
    runtime_names = sorted(name for name in called if name in runtime_defs)
    semantic_names = sorted(
        name
        for name in called
        if name in internals_defs and not name.startswith("cell_")
    )
    stubs = [
        *(
            _format_runtime_dependency_stub(runtime_defs[name])
            for name in runtime_names
        ),
        *(
            _format_semantic_dependency_stub(internals_defs[name])
            for name in semantic_names
        ),
    ]
    return "\n\n".join(stubs)


def build_singleton_refactor_context_dump(
    *,
    function_name: str,
    address: str,
    internals_source: str,
    runtime_source: str,
    cell_metadata: Mapping[str, object],
    index: InternalsSourceIndex | None = None,
    function_source: str | None = None,
    helper_name: str | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    resolved_function_source = (
        function_source
        if function_source is not None
        else resolved.function_source(function_name)
    )
    metadata = dict(cell_metadata)
    metadata["address"] = address
    dependency_stubs = _build_dependency_stubs(
        function_source=resolved_function_source,
        internals_source=internals_source,
        runtime_source=runtime_source,
        index=resolved,
    )
    return format_singleton_refactor_context_dump(
        function_source=resolved_function_source,
        cell_metadata=metadata,
        dependency_stubs=dependency_stubs,
        helper_name=helper_name,
    )


def _cell_metadata_for_singleton_refactor(
    ctx: SingletonRefactorContext,
) -> dict[str, object]:
    metadata: dict[str, object] = {"address": ctx.address}
    binding_keys = ctx.naming_hints.get("binding_keys")
    binding_record = ctx.naming_hints.get("binding_record")
    metadata["binding_keys"] = (
        dict(binding_keys) if isinstance(binding_keys, Mapping) else {}
    )
    metadata["binding_record"] = (
        dict(binding_record) if isinstance(binding_record, Mapping) else {}
    )
    return metadata


def build_singleton_refactor_prompt_context(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    runtime_path: Path | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> str:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    return build_singleton_refactor_context_dump(
        function_name=ctx.function_name,
        address=ctx.address,
        internals_source=index.source,
        runtime_source=_read_runtime_source(internals_path, runtime_path=runtime_path),
        cell_metadata=_cell_metadata_for_singleton_refactor(ctx),
        index=index,
        function_source=ctx.python_source,
        helper_name=ctx.expected_helper_name,
    )


def load_cluster_refactor_prompt_fixed_portion(
    contract: ClusterRefactorContract = "member_sweep",
) -> str:
    return CLUSTER_REFACTOR_PROMPT_FIXTURES[contract].read_text(encoding="utf-8")


def append_cluster_refactor_note_section(
    docstring: str,
    *,
    covered_addresses: str,
    formula: str,
) -> str:
    return (
        f"{docstring.rstrip()}\n\n"
        f"Note:\n    Covers {covered_addresses}. Excel: {formula}."
    )


def format_cluster_covered_addresses(addresses: Sequence[str]) -> str:
    if not addresses:
        raise ValueError("addresses must not be empty")
    if len(addresses) == 1:
        return addresses[0]
    parsed = [parse_workbook_address(address) for address in addresses]
    sheets = {sheet for sheet, _column, _row in parsed}
    rows = {row for _sheet, _column, row in parsed}
    if len(sheets) != 1 or len(rows) != 1:
        return ", ".join(sorted(addresses))
    sheet = next(iter(sheets))
    row = next(iter(rows))
    columns = sorted(
        {column for _sheet, column, _row in parsed},
        key=_column_index,
    )
    column_indices = [_column_index(column) for column in columns]
    contiguous = all(
        later - earlier == 1
        for earlier, later in zip(column_indices, column_indices[1:], strict=False)
    )
    if contiguous:
        return f"{sheet}!{columns[0]}{row}:{columns[-1]}{row}"
    return ", ".join(sorted(addresses))


def assemble_cluster_symbol_source(
    *,
    signature: str,
    docstring: str,
    body: str,
) -> str:
    return assemble_singleton_symbol_source(
        signature=signature,
        docstring=docstring,
        body=body,
    )


def synthesize_cluster_parameters(
    ctx: ClusterRefactorContext,
) -> tuple[HelperParameter, ...]:
    """Build helper parameters from varying dimensions × key vocabulary."""
    dimension_sets = [
        frozenset(ctx.expected_member_keys[member.address].keys())
        for member in ctx.members
    ]
    if not dimension_sets:
        return ()
    if len(set(dimension_sets)) != 1:
        raise ValueError("cluster members must share one varying key dimension set")
    varying_dimension_ids = dimension_sets[0]
    specs = helper_parameters_for_varying_keys(
        varying_dimension_ids, ctx.key_vocabulary
    )
    return tuple(
        HelperParameter(
            name=spec.suggested_param_name,
            dimension_id=spec.dimension_id,
            dtype=spec.dtype,
            concept=spec.concept,
        )
        for spec in specs
    )


def synthesize_cluster_member_keys(
    ctx: ClusterRefactorContext,
    *,
    parameters: Sequence[HelperParameter],
) -> tuple[MemberKeys, ...]:
    """Build full-cluster member_keys from expected binding keys."""
    return complete_cluster_member_keys_from_expected(
        (),
        ctx,
        parameters=parameters,
    )


def prepare_cluster_refactor_response(
    llm_response: ClusterRefactorLLMResponse,
    ctx: ClusterRefactorContext,
    *,
    runtime_source: str,
    internals_source: str,
) -> ClusterRefactorResponse:
    if llm_response.symbol_docstring is None or llm_response.symbol_body is None:
        raise ValueError("cluster refactor response is missing required success fields")
    parameters = synthesize_cluster_parameters(ctx)
    member_keys = synthesize_cluster_member_keys(ctx, parameters=parameters)
    return_hint = infer_refactor_return_type_hint(
        python_sources=tuple(member.python_source for member in ctx.members),
        runtime_source=runtime_source,
        internals_source=internals_source,
        naming_hints=ctx.naming_hints,
    )
    signature = inject_signature_return_type_hint(
        build_locked_helper_signature(
            ctx.expected_helper_name,
            parameters=parameters,
        ),
        return_hint,
    )
    docstring = strip_python_string_delimiters(llm_response.symbol_docstring)
    covered_addresses = format_cluster_covered_addresses(
        tuple(member.address for member in ctx.members)
    )
    docstring = append_cluster_refactor_note_section(
        docstring,
        covered_addresses=covered_addresses,
        formula=ctx.canonical_template,
    )
    helper_source = assemble_cluster_symbol_source(
        signature=signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    return ClusterRefactorResponse(
        helper_name=ctx.expected_helper_name,
        helper_docstring=docstring,
        helper_source=helper_source,
        parameters=parameters,
        member_keys=member_keys,
    )


def _yaml_scalar(value: object) -> str:
    if isinstance(value, str):
        if (
            not value
            or value.isdigit()
            or value != value.strip()
            or value.startswith(("'", '"'))
            or ":" in value
            or "#" in value
        ):
            return json.dumps(value)
        return value
    return str(value)


def _format_label_group_yaml(labels: object) -> list[str]:
    if not isinstance(labels, list) or not labels:
        return ["[]"]
    lines: list[str] = []
    for item in labels:
        if not isinstance(item, Mapping):
            continue
        label = item.get("label")
        if label is None:
            continue
        lines.append(f"  - label: {_yaml_scalar(label)}")
        concept = item.get("concept")
        if concept is not None:
            lines.append(f"    concept: {concept}")
    return lines if lines else ["[]"]


def _format_key_vocabulary_yaml(
    key_vocabulary: Sequence[KeyConceptSpec],
) -> str:
    lines: list[str] = []
    for item in key_vocabulary:
        lines.append(f"- dimension_id: {item.dimension_id}")
        lines.append(f"  concept: {item.concept}")
        lines.append(f"  dtype: {item.dtype}")
        lines.append(f"  suggested_param_name: {item.suggested_param_name}")
    return "\n".join(lines)


def _format_member_metadata_yaml(
    member_metadata: Sequence[Mapping[str, object]],
) -> str:
    blocks: list[str] = []
    for entry in member_metadata:
        lines = [
            f"- address: {entry['address']}",
            f"  function_name: {entry['function_name']}",
            "  expected_keys:",
        ]
        expected_keys = entry.get("expected_keys", {})
        if isinstance(expected_keys, Mapping):
            for concept, value in expected_keys.items():
                lines.append(f"    {concept}: {value}")
        for map_key in ("binding_keys", "binding_record"):
            map_lines = _format_binding_map_yaml(map_key, entry.get(map_key))
            lines.extend(f"  {line}" for line in map_lines)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_cluster_refactor_context_dump(
    *,
    member_sources: str,
    key_vocabulary: Sequence[KeyConceptSpec],
    member_metadata: Sequence[Mapping[str, object]],
    dependency_stubs: str,
    shown_member_count: int | None = None,
    total_member_count: int | None = None,
) -> str:
    vocabulary_yaml = _format_key_vocabulary_yaml(key_vocabulary)
    metadata_yaml = _format_member_metadata_yaml(member_metadata)
    if (
        shown_member_count is not None
        and total_member_count is not None
        and shown_member_count < total_member_count
    ):
        heading = (
            f"Cluster to refactor (showing {shown_member_count} of "
            f"{total_member_count} members; remaining member_keys are filled "
            "mechanically):\n\n"
        )
    else:
        heading = "Cluster to refactor:\n\n"
    return (
        f"{heading}"
        f"```python\n{member_sources.strip()}\n```\n\n"
        "Key vocabulary:\n\n"
        f"```yaml\n{vocabulary_yaml}\n```\n\n"
        "Member metadata:\n\n"
        f"```yaml\n{metadata_yaml}\n```\n\n"
        "Dependencies:\n\n"
        f"```python\n{dependency_stubs.strip()}\n```"
    )


def _called_function_names_from_sources(
    function_sources: Iterable[str],
) -> tuple[str, ...]:
    names: list[str] = []
    for function_source in function_sources:
        names.extend(_called_function_names(function_source))
    return tuple(dict.fromkeys(names))


def _build_cluster_dependency_stubs(
    *,
    member_function_names: Sequence[str],
    internals_source: str,
    runtime_source: str,
    index: InternalsSourceIndex | None = None,
    member_function_sources: Mapping[str, str] | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    if member_function_sources is None:
        function_sources = [
            resolved.function_source(function_name)
            for function_name in member_function_names
        ]
    else:
        function_sources = [
            member_function_sources[function_name]
            for function_name in member_function_names
        ]
    called = _called_function_names_from_sources(function_sources)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = resolved.functions
    runtime_names = sorted(name for name in called if name in runtime_defs)
    semantic_names = sorted(
        name
        for name in called
        if name in internals_defs and not name.startswith("cell_")
    )
    stubs = [
        *(
            _format_runtime_dependency_stub(runtime_defs[name])
            for name in runtime_names
        ),
        *(
            _format_semantic_dependency_stub(internals_defs[name])
            for name in semantic_names
        ),
    ]
    return "\n\n".join(stubs)


def sample_indices_for_prompt(
    count: int,
    *,
    limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
) -> tuple[int, ...]:
    """Return up to ``limit`` indices spaced across ``0..count-1`` inclusive."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if count <= limit:
        return tuple(range(count))
    if limit == 1:
        return (0,)
    return tuple(
        dict.fromkeys(round(i * (count - 1) / (limit - 1)) for i in range(limit))
    )


def sample_members_for_prompt[T](
    members: Sequence[T],
    *,
    limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
) -> tuple[T, ...]:
    """Return a uniformly spaced subset of ``members`` capped at ``limit``."""
    indices = sample_indices_for_prompt(len(members), limit=limit)
    return tuple(members[index] for index in indices)


def complete_cluster_member_keys_from_expected(
    member_keys: Sequence[MemberKeys],
    ctx: ClusterRefactorContext,
    *,
    parameters: Sequence[HelperParameter],
) -> tuple[MemberKeys, ...]:
    """Fill missing member_keys from expected binding keys for the full cluster."""
    parameter_dimension_ids = tuple(
        dict.fromkeys(parameter.dimension_id for parameter in parameters)
    )
    by_address = {entry.address: entry for entry in member_keys}
    completed: list[MemberKeys] = []
    for member in ctx.members:
        existing = by_address.get(member.address)
        if existing is not None:
            completed.append(existing)
            continue
        expected = ctx.expected_member_keys.get(member.address, {})
        keys = tuple(
            MemberKeyEntry(dimension_id=dimension_id, value=expected[dimension_id])
            for dimension_id in parameter_dimension_ids
            if dimension_id in expected
        )
        completed.append(
            MemberKeys(
                address=member.address,
                function_name=member.function_name,
                keys=keys,
            )
        )
    return tuple(completed)


def build_cluster_refactor_context_dump(
    *,
    member_function_names: Sequence[str],
    internals_source: str,
    runtime_source: str,
    key_vocabulary: Sequence[KeyConceptSpec],
    member_metadata: Sequence[Mapping[str, object]],
    shown_member_count: int | None = None,
    total_member_count: int | None = None,
    index: InternalsSourceIndex | None = None,
    member_function_sources: Mapping[str, str] | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    if member_function_sources is None:
        resolved_member_sources = {
            function_name: resolved.function_source(function_name)
            for function_name in member_function_names
        }
    else:
        resolved_member_sources = {
            function_name: member_function_sources.get(
                function_name,
                resolved.function_source(function_name),
            )
            for function_name in member_function_names
        }
    member_sources = "\n\n\n".join(
        resolved_member_sources[function_name].strip()
        for function_name in member_function_names
    )
    dependency_stubs = _build_cluster_dependency_stubs(
        member_function_names=member_function_names,
        internals_source=internals_source,
        runtime_source=runtime_source,
        index=resolved,
        member_function_sources=resolved_member_sources,
    )
    return format_cluster_refactor_context_dump(
        member_sources=member_sources,
        key_vocabulary=key_vocabulary,
        member_metadata=member_metadata,
        dependency_stubs=dependency_stubs,
        shown_member_count=shown_member_count,
        total_member_count=total_member_count,
    )


def _member_metadata_for_cluster_refactor(
    ctx: ClusterRefactorContext,
    *,
    members: Sequence[MemberContext] | None = None,
) -> tuple[dict[str, object], ...]:
    selected_members = ctx.members if members is None else tuple(members)
    entries: list[dict[str, object]] = []
    for member in selected_members:
        entry: dict[str, object] = {
            "address": member.address,
            "function_name": member.function_name,
            "expected_keys": ctx.expected_member_keys.get(member.address, {}),
            "binding_keys": member.binding_keys or {},
            "binding_record": member.binding_record or {},
        }
        entries.append(entry)
    return tuple(entries)


def build_cluster_refactor_prompt_context(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    runtime_path: Path | None = None,
    member_limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
    internals_index: InternalsSourceIndex | None = None,
) -> str:
    varying_dimension_ids = frozenset(
        dimension_id
        for keys in ctx.expected_member_keys.values()
        for dimension_id in keys
    )
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    filtered_vocabulary = tuple(
        item
        for item in ctx.key_vocabulary
        if item.dimension_id in varying_dimension_ids
    )
    vocabulary_yaml = _format_key_vocabulary_yaml(filtered_vocabulary)
    runtime_source = _read_runtime_source(internals_path, runtime_path=runtime_path)

    summary = ctx.fingerprint_summary
    use_fingerprint = summary is not None and summary.fallback_reason is None
    if use_fingerprint:
        assert summary is not None
        exemplars = tuple(group.exemplar for group in summary.groups)
        member_function_sources = {
            member.function_name: member.python_source for member in exemplars
        }
        dependency_stubs = _build_cluster_dependency_stubs(
            member_function_names=tuple(member.function_name for member in exemplars),
            internals_source=index.source,
            runtime_source=runtime_source,
            index=index,
            member_function_sources=member_function_sources,
        )
        metadata_yaml = _format_member_metadata_yaml(
            _member_metadata_for_cluster_refactor(ctx, members=exemplars)
        )
        return format_cluster_fingerprint_dump(
            summary,
            exemplar_keys_by_address=ctx.expected_member_keys,
            key_vocabulary_yaml=vocabulary_yaml,
            member_metadata_yaml=metadata_yaml,
            dependency_stubs=dependency_stubs,
            helper_name=ctx.expected_helper_name,
        )

    prompt_members = sample_members_for_prompt(ctx.members, limit=member_limit)
    member_function_sources = {
        member.function_name: member.python_source for member in prompt_members
    }
    return build_cluster_refactor_context_dump(
        member_function_names=tuple(member.function_name for member in prompt_members),
        internals_source=index.source,
        runtime_source=runtime_source,
        key_vocabulary=filtered_vocabulary,
        member_metadata=_member_metadata_for_cluster_refactor(
            ctx, members=prompt_members
        ),
        shown_member_count=len(prompt_members),
        total_member_count=len(ctx.members),
        index=index,
        member_function_sources=member_function_sources,
    )


def _type_hint_runtime_imports(source: str) -> set[str]:
    """Collect allowlisted type-hint names that appear in refactored source."""
    return {name for name in ALLOWED_REFACTOR_TYPE_HINT_NAMES if name in source}


def _cluster_runtime_imports(response: ClusterRefactorResponse) -> set[str]:
    return _type_hint_runtime_imports(response.helper_source)


def ensure_cluster_refactor_imports(
    source: str,
    response: ClusterRefactorResponse,
) -> str:
    needed = _cluster_runtime_imports(response)
    if not needed:
        return source
    return _merge_runtime_imports(source, needed)


def _singleton_runtime_imports(response: SingletonRefactorResponse) -> set[str]:
    return _type_hint_runtime_imports(response.symbol_source)


def _merge_runtime_imports(source: str, symbols: set[str]) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "runtime" or node.level != 1:
            continue
        existing = {alias.name for alias in node.names}
        merged = sorted(existing | symbols)
        replacement = "from .runtime import " + ", ".join(merged) + "\n"
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        return "".join(lines[:start]) + replacement + "".join(lines[end:])
    return source


def ensure_singleton_refactor_imports(
    source: str,
    response: SingletonRefactorResponse,
) -> str:
    needed = _singleton_runtime_imports(response)
    if not needed:
        return source
    return _merge_runtime_imports(source, needed)


def singleton_prompt_payload(ctx: SingletonRefactorContext) -> dict[str, object]:
    return {
        "address": ctx.address,
        "function_name": ctx.function_name,
        "canonical_template": ctx.canonical_template,
        "normalized_formula": ctx.normalized_formula,
        "python_source": ctx.python_source,
        "dependency_addresses": list(ctx.dependency_addresses),
        "external_dependencies": list(ctx.external_dependencies),
        "semantic_dependencies": [
            {
                "address_template": dependency.address_template,
                "columns": list(dependency.columns),
                "helper_name": dependency.helper_name,
                "call_form": dependency.call_form,
            }
            for dependency in ctx.semantic_dependencies
        ],
        "call_sites": [
            {
                "caller_function": site.caller_function,
                "caller_address": site.caller_address,
                "callee_function": site.callee_function,
                "callee_address": site.callee_address,
                "pattern": site.pattern,
                "line": site.line,
                "snippet": site.snippet,
            }
            for site in ctx.call_sites
        ],
        "constraints": {
            "allowed_runtime_symbols": list(ctx.allowed_runtime_symbols),
            "signature": "(ctx)",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "naming_hints": ctx.naming_hints,
        "expected_helper_name": ctx.expected_helper_name,
    }


def validate_singleton_refactor_response(
    ctx: SingletonRefactorContext,
    response: SingletonRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
) -> None:
    if response.symbol_name != ctx.expected_helper_name:
        raise ValueError(
            "symbol_name must equal locked helper name "
            f"{ctx.expected_helper_name!r}, got {response.symbol_name!r}"
        )
    validate_semantic_identifier(
        response.symbol_name,
        existing_names=existing_names,
        allow_name=ctx.expected_helper_name,
    )

    if not response.symbol_name.isidentifier():
        raise ValueError(
            f"symbol_name is not a valid identifier: {response.symbol_name!r}"
        )

    symbol_module = ast.parse(response.symbol_source)
    function_defs = [
        node for node in symbol_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(function_defs) != 1:
        raise ValueError("symbol_source must contain exactly one FunctionDef")
    symbol_def = function_defs[0]
    if symbol_def.name != response.symbol_name:
        raise ValueError(
            "symbol_source function name must match symbol_name "
            f"({response.symbol_name!r})"
        )

    source_docstring = _function_docstring(symbol_def)
    if source_docstring is None:
        raise ValueError("symbol_source must include a docstring")
    validate_google_style_docstring(source_docstring)
    if source_docstring != response.symbol_docstring:
        raise ValueError(
            "symbol_docstring must match the docstring embedded in symbol_source"
        )

    validate_semantic_local_names(symbol_def)
    validate_no_cell_function_references(symbol_def)

    arg_names = [arg.arg for arg in symbol_def.args.args]
    if arg_names != ["ctx"]:
        raise ValueError(
            f"singleton must accept exactly (ctx); got ({', '.join(arg_names)})"
        )

    if any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.walk(symbol_module)
    ):
        raise ValueError("symbol_source must not contain imports")

    allowed_names = _without_cell_function_names(
        set(ctx.allowed_runtime_symbols)
        | set(ctx.external_dependencies)
        | {ctx.function_name}
        | {response.symbol_name}
        | semantic_helpers_available_for_calls(internals_source, existing_names)
        | set(ALLOWED_REFACTOR_TYPE_HINT_NAMES)
        | {"ctx"}
    )
    builtin_names = set(dir(builtins))
    for node in ast.walk(symbol_def):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in builtin_names or name in allowed_names:
            continue
        raise ValueError(
            f"symbol_source calls disallowed function {name!r}; "
            f"allowed: {sorted(allowed_names)}"
        )

    validate_allowed_global_references(symbol_def, allowed_names=allowed_names)


def _replace_function_definition(source: str, old_name: str, new_source: str) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == old_name:
            start = node.lineno - 1
            end = node.end_lineno or node.lineno
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            replacement = new_source.strip() + "\n\n"
            return "".join(lines[:start]) + replacement + "".join(lines[end:])
    raise KeyError(f"Function {old_name!r} not found")


def apply_singleton_refactor_plan(
    source: str,
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> tuple[str, int]:
    source = ensure_singleton_refactor_imports(source, response)
    updated = _replace_function_definition(
        source,
        ctx.function_name,
        response.symbol_source,
    )
    binding = CollapseBinding(
        address=ctx.address,
        function_name=ctx.function_name,
        helper_name=response.symbol_name,
        literal_call=f"{response.symbol_name}(ctx)",
    )
    updated, rewrite_count = substitute_collapse_bindings(updated, (binding,))
    if RESOLVER_SECTION_MARKER in updated:
        symbol_dispatch = _parse_symbol_dispatch(source)
        symbol_dispatch[ctx.address] = response.symbol_name
        updated = _replace_resolver_section(
            updated,
            _parse_address_dispatch(updated) or {},
            symbol_dispatch=symbol_dispatch,
        )
    return updated, rewrite_count


def apply_refactor_plan(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext | None = None,
) -> str:
    updated, _rewrite_count = apply_cluster_collapse(source, response, ctx)
    return updated


def collapse_bindings_for_response(
    response: ClusterRefactorResponse,
) -> tuple[CollapseBinding, ...]:
    parameter_pairs = tuple(
        (parameter.name, parameter.dimension_id) for parameter in response.parameters
    )
    return tuple(
        CollapseBinding(
            address=entry.address,
            function_name=entry.function_name,
            helper_name=response.helper_name,
            literal_call=render_literal_helper_call(
                response.helper_name,
                parameter_pairs,
                entry.keys_dict(),
            ),
        )
        for entry in response.member_keys
    )


def apply_cluster_collapse(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext | None = None,
) -> tuple[str, int]:
    _ = ctx
    source = ensure_cluster_refactor_imports(source, response)
    updated = insert_helper_source(source, response.helper_source)
    bindings = collapse_bindings_for_response(response)
    updated, rewrite_count = substitute_collapse_bindings(updated, bindings)
    collapsed_functions = frozenset(
        entry.function_name for entry in response.member_keys
    )
    updated = _remove_function_definitions(updated, collapsed_functions)
    dispatch_updates = _dispatch_entries_for_collapse(response)
    if dispatch_updates and RESOLVER_SECTION_MARKER in updated:
        dispatch = _parse_address_dispatch(updated) or {}
        dispatch.update(dispatch_updates)
        updated = _replace_resolver_section(
            updated,
            dispatch,
            symbol_dispatch=_parse_symbol_dispatch(source),
        )
    return updated, rewrite_count


def _parameter_literals(
    parameters: tuple[HelperParameter, ...],
    keys: dict[str, BindingKeyValue],
) -> dict[str, BindingKeyValue]:
    return {parameter.name: keys[parameter.dimension_id] for parameter in parameters}


def _dispatch_entries_for_collapse(
    response: ClusterRefactorResponse,
) -> AddressDispatch:
    dispatch: AddressDispatch = {}
    for entry in response.member_keys:
        if not address_needs_resolver_dispatch(entry.address):
            continue
        dispatch[entry.address] = (
            response.helper_name,
            _parameter_literals(response.parameters, entry.keys_dict()),
        )
    return dispatch


def substitute_collapse_bindings(
    source: str,
    bindings: tuple[CollapseBinding, ...],
) -> tuple[str, int]:
    if not bindings:
        return source, 0
    bindings_by_function = {binding.function_name: binding for binding in bindings}
    module = ast.parse(source)
    replacements: list[tuple[int, int, str, str]] = []
    visitor = _CollapseBindingsRewriteVisitor(
        bindings_by_function=bindings_by_function,
        source=source,
        replacements=replacements,
    )
    for function_def in _iter_function_defs(module.body):
        visitor.visit(function_def)
    if not replacements:
        return source, 0
    return _apply_segment_replacements(source, replacements), len(replacements)


def collect_static_cell_function_references(source: str) -> frozenset[str]:
    module = ast.parse(source)
    references: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "xl_eval"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Name)
        ):
            references.add(node.args[2].id)
        if isinstance(node.func, ast.Name) and node.func.id.startswith("cell_"):
            references.add(node.func.id)
    return frozenset(references)


def parse_thin_wrapper(function_def: ast.FunctionDef) -> tuple[str, str] | None:
    """Return ``(helper_name, engine_column)`` for legacy column-literal wrappers."""
    parsed = _parse_thin_helper_return(function_def)
    if parsed is None:
        return None
    helper_name, key_kwargs = parsed
    if len(key_kwargs) != 1 or "time_period" not in key_kwargs:
        return None
    column = _engine_column_for_time_period(
        int(key_kwargs["time_period"]),
        _resolved_projection_layout(),
    )
    if column is None:
        return None
    return helper_name, column


def parse_thin_literal_wrapper(
    function_def: ast.FunctionDef,
) -> tuple[str, dict[str, BindingKeyValue]] | None:
    """Return ``(helper_name, key_kwargs)`` for a one-line semantic helper wrapper."""
    return _parse_thin_helper_return(function_def)


def _parse_thin_helper_return(
    function_def: ast.FunctionDef,
) -> tuple[str, dict[str, BindingKeyValue]] | None:
    body = function_def.body
    start = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        start = 1
    executable_body = body[start:]
    if len(executable_body) != 1:
        return None
    statement = executable_body[0]
    if not isinstance(statement, ast.Return) or statement.value is None:
        return None
    call = statement.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "ctx"
    ):
        return None
    if call.keywords:
        key_kwargs: dict[str, BindingKeyValue] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                return None
            if not isinstance(keyword.value, ast.Constant):
                return None
            literal = keyword.value.value
            if not isinstance(literal, (str, int, float, bool)):
                return None
            key_kwargs[keyword.arg] = literal
        return call.func.id, key_kwargs
    if (
        len(call.args) == 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    ):
        time_period = _time_period_for_engine_column(
            call.args[1].value,
            _resolved_projection_layout(),
        )
        if time_period is None:
            return None
        return call.func.id, {"time_period": time_period}
    return None


_ENGINE_ADDRESS_PATTERN = re.compile(
    r"^(?P<sheet>.+!)(?P<column>[A-Za-z]+)(?P<row>\d+)$"
)


def _column_address_template(address: str) -> str:
    match = _ENGINE_ADDRESS_PATTERN.match(address)
    if match is None:
        return address
    return f"{match.group('sheet')}{{col}}{match.group('row')}"


def resolve_semantic_dependencies(
    source: str,
    dependency_addresses: Iterable[str],
    *,
    index: InternalsSourceIndex | None = None,
) -> tuple[tuple[SemanticDependency, ...], tuple[str, ...]]:
    """Resolve external ``cell_*`` dependencies to the semantic helpers wrapping them."""
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    defined_functions = resolved.functions
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unresolved: set[str] = set()
    semantic_helpers = resolved.semantic_helper_names
    for address in dependency_addresses:
        function_name = address_to_function_name(address)
        node = defined_functions.get(function_name)
        if node is not None:
            wrapper = parse_thin_wrapper(node)
            if wrapper is None or wrapper[0] not in semantic_helpers:
                unresolved.add(function_name)
                continue
            helper_name, column = wrapper
            grouped[helper_name].append((address, column))
            continue

        collapsed = _infer_collapsed_semantic_dependency(
            source, address, index=resolved
        )
        if collapsed is None:
            unresolved.add(function_name)
            continue
        helper_name, parameter_name = collapsed
        grouped[helper_name].append((address, parameter_name))

    semantic_dependencies = tuple(
        SemanticDependency(
            helper_name=helper_name,
            call_form=_helper_pass_through_call_form(
                source, helper_name, index=resolved
            ),
            address_template=_column_address_template(sorted(entries)[0][0]),
            columns=tuple(tag for _, tag in sorted(entries)),
            addresses=tuple(address for address, _ in sorted(entries)),
        )
        for helper_name, entries in sorted(grouped.items())
    )
    return semantic_dependencies, tuple(sorted(unresolved))


def _helper_pass_through_call_form(
    source: str,
    helper_name: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> str:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    node = resolved.functions.get(helper_name)
    if node is not None:
        parameter_names = [arg.arg for arg in node.args.args if arg.arg != "ctx"]
        if len(parameter_names) == 1:
            parameter_name = parameter_names[0]
            return f"{helper_name}(ctx, {parameter_name}={parameter_name})"
        return f"{helper_name}(ctx)"
    return f"{helper_name}(ctx)"


def _column_index(column: str) -> int:
    value = 0
    for character in column.upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def _covered_addresses_text(docstring: str) -> str:
    """Return only the ``Covers ...`` clause of a helper docstring's Note.

    The ``Excel:`` formula transcription is excluded so that addresses appearing
    inside a formula (which the helper reads, but does not compute) are never
    mistaken for cells the helper covers.
    """
    normalized = docstring.replace("$", "")
    covers_index = normalized.find("Covers")
    if covers_index == -1:
        return ""
    covered = normalized[covers_index + len("Covers") :]
    excel_index = covered.find("Excel:")
    if excel_index != -1:
        covered = covered[:excel_index]
    return covered


def _address_in_docstring_range(docstring: str, address: str) -> bool:
    covered = _covered_addresses_text(docstring)
    if not covered:
        return False
    if address.replace("$", "") in covered:
        return True
    match = _ENGINE_ADDRESS_PATTERN.match(address)
    if match is None:
        return False
    sheet = match.group("sheet")
    column = match.group("column").upper()
    row = match.group("row")
    column_index = _column_index(column)
    range_pattern = re.compile(
        rf"{re.escape(sheet)}(?P<start>[A-Z]+){row}:(?P<end>[A-Z]+){row}"
    )
    for range_match in range_pattern.finditer(covered):
        start_index = _column_index(range_match.group("start"))
        end_index = _column_index(range_match.group("end"))
        if start_index <= column_index <= end_index:
            return True
    return False


def _infer_collapsed_semantic_dependency(
    source: str,
    address: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> tuple[str, str] | None:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    dispatch = resolved.address_dispatch
    if address in dispatch:
        helper_name, key_kwargs = dispatch[address]
        if helper_name not in resolved.semantic_helper_names:
            return None
        if len(key_kwargs) == 1:
            return helper_name, next(iter(key_kwargs))
        return helper_name, next(iter(key_kwargs))

    symbol_dispatch = resolved.symbol_dispatch
    symbol_name = symbol_dispatch.get(address)
    if symbol_name is not None:
        if symbol_name not in resolved.semantic_helper_names:
            return None
        return symbol_name, ""

    defined_functions = resolved.functions
    matches: list[tuple[str, str]] = []
    for helper_name in sorted(resolved.semantic_helper_names):
        helper_def = defined_functions.get(helper_name)
        if helper_def is None:
            continue
        docstring = _function_docstring(helper_def)
        if docstring is None or not _address_in_docstring_range(docstring, address):
            continue
        parameter_names = [arg.arg for arg in helper_def.args.args if arg.arg != "ctx"]
        if len(parameter_names) == 1:
            matches.append((helper_name, parameter_names[0]))
        elif not parameter_names:
            matches.append((helper_name, ""))
    if len(matches) == 1:
        return matches[0]
    return None


def function_name_to_workbook_address(function_name: str) -> str | None:
    return _caller_address(function_name)


def address_needs_resolver_dispatch(address: str) -> bool:
    """Engine cells are reached via helpers; only external entry addresses need dispatch."""
    layout = _resolved_projection_layout()
    sheet, _, _ = parse_workbook_address(address)
    if layout is not None:
        return sheet != layout.engine_sheet
    return not address.startswith("Engine!")


def apply_phase_c(source: str) -> tuple[str, int]:
    """Drop unreferenced thin ``cell_*`` wrappers and route them via ``_ADDRESS_DISPATCH``."""
    referenced = collect_static_cell_function_references(source)
    module = ast.parse(source)
    dispatch: AddressDispatch = {}
    to_prune: set[str] = set()

    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("cell_"):
            continue
        thin_wrapper = parse_thin_literal_wrapper(node)
        if thin_wrapper is None:
            continue
        if node.name in referenced:
            continue
        address = function_name_to_workbook_address(node.name)
        if address is None:
            continue
        helper_name, key_kwargs = thin_wrapper
        if address_needs_resolver_dispatch(address):
            dispatch[address] = (helper_name, key_kwargs)
        to_prune.add(node.name)

    if not to_prune:
        return _trim_engine_dispatch_entries(source)

    updated = _remove_function_definitions(source, frozenset(to_prune))
    existing_dispatch = _parse_address_dispatch(updated) or {}
    existing_dispatch.update(dispatch)
    updated = _replace_resolver_section(
        updated,
        existing_dispatch,
        symbol_dispatch=_parse_symbol_dispatch(source),
    )
    updated, trimmed = _trim_engine_dispatch_entries(updated)
    return updated, len(to_prune) + trimmed


def _parse_address_dispatch(
    source: str,
    *,
    module: ast.Module | None = None,
) -> AddressDispatch | None:
    tree = module if module is not None else ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_ADDRESS_DISPATCH":
                if not isinstance(node.value, ast.Dict):
                    raise ValueError("_ADDRESS_DISPATCH must be a dict literal")
                dispatch: AddressDispatch = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value, ast.Tuple)
                        and len(value.elts) == 2
                        and isinstance(value.elts[0], ast.Constant)
                        and isinstance(value.elts[0].value, str)
                        and isinstance(value.elts[1], ast.Dict)
                    ):
                        raise ValueError("_ADDRESS_DISPATCH has unexpected entry shape")
                    key_kwargs: dict[str, BindingKeyValue] = {}
                    for kw_key, kw_value in zip(
                        value.elts[1].keys,
                        value.elts[1].values,
                    ):
                        if not (
                            isinstance(kw_key, ast.Constant)
                            and isinstance(kw_key.value, str)
                            and isinstance(kw_value, ast.Constant)
                        ):
                            raise ValueError(
                                "_ADDRESS_DISPATCH keyword args must be constant literals"
                            )
                        literal = kw_value.value
                        if not isinstance(literal, (str, int, float, bool)):
                            raise ValueError(
                                "_ADDRESS_DISPATCH keyword args must be scalar literals"
                            )
                        key_kwargs[kw_key.value] = literal
                    dispatch[key.value] = (value.elts[0].value, key_kwargs)
                return dispatch
    return None


def _trim_engine_dispatch_entries(source: str) -> tuple[str, int]:
    dispatch = _parse_address_dispatch(source)
    if dispatch is None:
        return source, 0
    trimmed = {
        address: spec
        for address, spec in dispatch.items()
        if address_needs_resolver_dispatch(address)
    }
    removed = len(dispatch) - len(trimmed)
    if removed == 0:
        return source, 0
    return _replace_resolver_section(
        source,
        trimmed,
        symbol_dispatch=_parse_symbol_dispatch(source),
    ), removed


def _parse_symbol_dispatch(
    source: str,
    *,
    module: ast.Module | None = None,
) -> dict[str, str]:
    tree = module if module is not None else ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_SYMBOL_DISPATCH":
                if not isinstance(node.value, ast.Dict):
                    raise ValueError("_SYMBOL_DISPATCH must be a dict literal")
                dispatch: dict[str, str] = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        raise ValueError("_SYMBOL_DISPATCH has unexpected entry shape")
                    dispatch[key.value] = value.value
                return dispatch
    return {}


def _remove_function_definitions(source: str, function_names: frozenset[str]) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            start = node.lineno - 1
            end = node.end_lineno or node.lineno
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            spans.append((start, end))
    for start, end in sorted(spans, key=lambda item: item[0], reverse=True):
        del lines[start:end]
    return "".join(lines)


def _replace_resolver_section(
    source: str,
    address_dispatch: AddressDispatch,
    *,
    symbol_dispatch: dict[str, str] | None = None,
) -> str:
    if symbol_dispatch is None:
        symbol_dispatch = _parse_symbol_dispatch(source)
    if RESOLVER_SECTION_MARKER not in source:
        raise ValueError(f"Missing section marker {RESOLVER_SECTION_MARKER!r}")
    head = source[: source.index(RESOLVER_SECTION_MARKER)]
    return head + _render_resolver_section(address_dispatch, symbol_dispatch)


def _render_resolver_section(
    address_dispatch: AddressDispatch,
    symbol_dispatch: dict[str, str] | None = None,
) -> str:
    symbol_dispatch = symbol_dispatch or {}
    dispatch_lines: list[str] = []
    for address, (helper, key_kwargs) in sorted(address_dispatch.items()):
        kw_parts = ", ".join(
            f"{name!r}: {format_binding_key_literal(value)}"
            for name, value in sorted(key_kwargs.items())
        )
        dispatch_lines.append(f"    {address!r}: ({helper!r}, {{{kw_parts}}}),")
    dispatch_body = "{\n" + "\n".join(dispatch_lines) + "\n}"
    symbol_lines = [
        f"    {address!r}: {symbol!r},"
        for address, symbol in sorted(symbol_dispatch.items())
    ]
    symbol_body = "{\n" + "\n".join(symbol_lines) + "\n}" if symbol_lines else "{}"
    return f"""{RESOLVER_SECTION_MARKER}
_RESOLVED_FORMULAS = {{}}
_ADDRESS_DISPATCH = {dispatch_body}
_SYMBOL_DISPATCH = {symbol_body}

def _address_to_func_name(address):
    name = []
    prev_underscore = False
    for ch in address.lower():
        if ch == "'":
            continue
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            name.append(ch)
            prev_underscore = False
        else:
            if not prev_underscore:
                name.append("_")
                prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{{base}}"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    dispatch = _ADDRESS_DISPATCH.get(address)
    if dispatch is not None:
        helper_name, key_kwargs = dispatch
        helper = globals()[helper_name]

        def _bound(ctx, _helper=helper, _key_kwargs=key_kwargs):
            return _helper(ctx, **_key_kwargs)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    symbol_name = _SYMBOL_DISPATCH.get(address)
    if symbol_name is not None:
        helper = globals()[symbol_name]

        def _bound(ctx, _helper=helper):
            return _helper(ctx)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
"""


def insert_helper_source(source: str, helper_source: str) -> str:
    helper_source = helper_source.strip()
    helper_module = ast.parse(helper_source)
    helper_defs = [
        node for node in helper_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(helper_defs) != 1:
        raise ValueError("helper_source must contain exactly one FunctionDef")
    helper_name = helper_defs[0].name
    if re.search(rf"^def {re.escape(helper_name)}\(", source, re.MULTILINE):
        return source
    if FORMULA_SECTION_MARKER not in source:
        raise ValueError(f"Missing section marker {FORMULA_SECTION_MARKER!r}")

    marker_index = source.index(FORMULA_SECTION_MARKER)
    insert_at = source.index("\n", marker_index) + 1
    while insert_at < len(source) and source[insert_at] == "\n":
        insert_at += 1
    return source[:insert_at] + helper_source + "\n\n" + source[insert_at:]


def _engine_row_from_address(address: str) -> int:
    match = re.search(r"\d+", address.split("!", 1)[1])
    if match is None:
        raise ValueError(f"Cannot parse engine row from address {address!r}")
    return int(match.group())


def _xl_eval_address_arg(node: ast.Call) -> str | None:
    if len(node.args) < 2:
        return None
    address_arg = node.args[1]
    if isinstance(address_arg, ast.Constant) and isinstance(address_arg.value, str):
        return address_arg.value
    if isinstance(address_arg, ast.JoinedStr):
        parts: list[str] = []
        for value in address_arg.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{col}")
        return "".join(parts)
    return None


def _xl_eval_callee_function(node: ast.Call) -> str | None:
    if len(node.args) < 3:
        return None
    callee_arg = node.args[2]
    if isinstance(callee_arg, ast.Name):
        return callee_arg.id
    return None


def _xl_eval_matches_collapse(
    node: ast.Call,
    binding: CollapseBinding,
) -> bool:
    if not (isinstance(node.func, ast.Name) and node.func.id == "xl_eval"):
        return False

    address_pattern = _xl_eval_address_arg(node)
    if address_pattern is None:
        return False

    callee_function = _xl_eval_callee_function(node)
    if address_pattern == binding.address:
        return callee_function == binding.function_name

    row = _engine_row_from_address(binding.address)
    if address_pattern == f"Engine!{{col}}{row}":
        return callee_function == binding.function_name
    return False


class _CollapseBindingsRewriteVisitor(ast.NodeVisitor):
    """Rewrite direct and xl_eval call sites for every collapse binding in one walk."""

    def __init__(
        self,
        *,
        bindings_by_function: dict[str, CollapseBinding],
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.bindings_by_function = bindings_by_function
        self.source = source
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        binding: CollapseBinding | None = None
        if isinstance(node.func, ast.Name) and node.func.id == "xl_eval":
            callee = _xl_eval_callee_function(node)
            if callee is not None:
                candidate = self.bindings_by_function.get(callee)
                if candidate is not None and _xl_eval_matches_collapse(node, candidate):
                    binding = candidate
        elif (
            isinstance(node.func, ast.Name)
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "ctx"
        ):
            binding = self.bindings_by_function.get(node.func.id)

        if binding is not None:
            segment = ast.get_source_segment(self.source, node)
            if segment is not None:
                self.replacements.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        segment,
                        binding.literal_call,
                    )
                )
        self.generic_visit(node)


def _apply_segment_replacements(
    source: str,
    replacements: list[tuple[int, int, str, str]],
) -> str:
    updated = source
    for _start_line, _end_line, old_segment, new_segment in sorted(
        replacements,
        key=lambda item: updated.find(item[2]) if item[2] in updated else -1,
        reverse=True,
    ):
        if old_segment not in updated:
            continue
        updated = updated.replace(old_segment, new_segment, 1)
    return updated


def _iter_function_defs(body: list[ast.stmt]) -> list[ast.FunctionDef]:
    return [node for node in body if isinstance(node, ast.FunctionDef)]


def validate_refactored_internals(source: str) -> None:
    ast.parse(source)
    compile(source, "internals.py", "exec")


def refactor_internals_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    response: SingletonRefactorResponse | None = None,
    dry_run: bool = False,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> SingletonRefactorApplyResult:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    existing_names = _function_names(source, index=index)
    if response is None:
        response = llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=index,
        )
    validate_singleton_refactor_response(
        ctx,
        response,
        existing_names=existing_names,
        internals_source=source,
    )
    updated, rewrite_count = apply_singleton_refactor_plan(source, response, ctx)
    validate_refactored_internals(updated)
    if not dry_run:
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
    return SingletonRefactorApplyResult(
        source=updated,
        symbol_name=response.symbol_name,
        old_function_name=ctx.function_name,
        reference_rewrites=rewrite_count,
        dry_run=dry_run,
        response=response,
    )


def refactor_internals_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    response: ClusterRefactorResponse | None = None,
    dry_run: bool = False,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> ClusterRefactorApplyResult:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    existing_names = _function_names(source, index=index)
    if response is None:
        response = llm_refactor_cluster(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=index,
        )
    validate_cluster_refactor_response(
        ctx,
        response,
        existing_names=existing_names,
        internals_source=source,
    )
    updated = apply_refactor_plan(source, response, ctx)
    validate_refactored_internals(updated)
    if not dry_run:
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
    return ClusterRefactorApplyResult(
        source=updated,
        helper_name=response.helper_name,
        wrappers_applied=tuple(entry.function_name for entry in response.member_keys),
        dry_run=dry_run,
        response=response,
    )


def refactor_internals_all_clusters(
    projection: ProjectionResult,
    clusters: tuple[FormulaCluster, ...],
    *,
    internals_path: Path,
    bindings_path: Path,
    workbook_path: Path,
    address_to_series_id: Mapping[str, str],
    dry_run: bool = False,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] | None = None,
    parity_gate: bool = True,
    layout: ProjectionColumnLayout | None = None,
) -> tuple[ClusterRefactorApplyResult, ...]:
    """Refactor every eligible cluster in unified dependency order.

    When ``parity_gate`` is enabled, each refactored helper is checked against the
    pristine pre-refactor cell semantics across several input vectors before its
    transaction is committed; a divergence rolls back and re-prompts the model.

    Pass ``bound_address_keys`` from the extract stage when available so cluster
    context construction does not call ``_default_bound_address_keys`` (which
    rebuilds the pipeline graph).
    """
    pristine_source: str | None = None
    input_vectors: Sequence[Mapping[str, object]] | None = None
    internals_index = _resolve_internals_index(internals_path)
    if parity_gate:
        from src.refactor_parity_gate import build_default_input_vectors

        pristine_source = internals_index.source
        input_vectors = build_default_input_vectors()

    ordered_units = compute_refactor_schedule(projection, clusters)
    existing_helper_names = _function_names(
        internals_index.source, index=internals_index
    )
    allocated_helper_names = allocate_schedule_helper_names(
        tuple(unit.members for unit in ordered_units),
        address_to_series_id,
        existing_names=existing_helper_names,
    )
    results: list[ClusterRefactorApplyResult] = []
    responses: list[ClusterRefactorResponse] = []
    refactored_any = False
    for unit, helper_name in zip(ordered_units, allocated_helper_names, strict=True):
        cluster = unit.as_formula_cluster()
        diagnostic_target = refactor_failure_target(unit)
        reserved_for_others = (
            frozenset(allocated_helper_names) | existing_helper_names
        ) - {helper_name}
        if len(cluster.members) == 1:
            ctx = build_singleton_refactor_context(
                projection,
                cluster,
                internals_path,
                source_graph=source_graph,
                internal_binding_index=internal_binding_index,
                internals_index=internals_index,
                address_to_series_id=address_to_series_id,
                expected_helper_name=helper_name,
                existing_helper_names=reserved_for_others,
            )
            if ctx is None:
                continue
            singleton_result = refactor_internals_singleton(
                ctx,
                internals_path=internals_path,
                dry_run=dry_run,
                pristine_source=pristine_source,
                input_vectors=input_vectors,
                source_graph=source_graph,
                diagnostic_target=diagnostic_target,
                internals_index=internals_index,
            )
            if not dry_run:
                internals_index = InternalsSourceIndex.from_source(
                    singleton_result.source
                )
            refactored_any = True
            continue

        ctx = build_cluster_refactor_context(
            projection,
            cluster,
            internals_path,
            source_graph=source_graph,
            internal_binding_index=internal_binding_index,
            bound_address_keys=bound_address_keys,
            bindings_path=bindings_path,
            workbook_path=workbook_path,
            layout=layout,
            internals_index=internals_index,
            address_to_series_id=address_to_series_id,
            expected_helper_name=helper_name,
            existing_helper_names=reserved_for_others,
        )
        if ctx is None:
            continue
        result = refactor_internals_cluster(
            ctx,
            internals_path=internals_path,
            dry_run=dry_run,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=internals_index,
        )
        if not dry_run:
            internals_index = InternalsSourceIndex.from_source(result.source)
        results.append(result)
        responses.append(result.response)
        refactored_any = True

    if not dry_run and refactored_any:
        source = internals_path.read_text(encoding="utf-8")
        updated, phase_c_pruned = apply_phase_c(source)
        validate_refactored_internals(updated)
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
        if results:
            last = results[-1]
            results[-1] = ClusterRefactorApplyResult(
                source=updated,
                helper_name=last.helper_name,
                wrappers_applied=last.wrappers_applied,
                dry_run=last.dry_run,
                response=last.response,
                phase_c_pruned=phase_c_pruned,
            )

    return tuple(results)


load_dotenv(repo_root / ".env")


def _read_runtime_source(
    internals_path: Path,
    *,
    runtime_path: Path | None = None,
) -> str:
    """Load runtime (and optional ``_readers``) for dependency stubs and return hints."""
    parts: list[str] = []
    resolved_runtime = (
        runtime_path
        if runtime_path is not None
        else internals_path.parent / "runtime.py"
    )
    if resolved_runtime.is_file():
        parts.append(resolved_runtime.read_text(encoding="utf-8"))
    readers_path = internals_path.parent / "_readers.py"
    if readers_path.is_file():
        parts.append(readers_path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def llm_refactor_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> SingletonRefactorResponse:
    llm_schema = SingletonRefactorLLMResponse.model_json_schema()
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    internals_bytes = index.source.encode("utf-8")
    internals_source = index.source
    runtime_source = _read_runtime_source(internals_path)
    existing_names = _function_names(internals_source, index=index)
    cache = load_refactor_cache()
    cache_key = singleton_refactor_cache_key(ctx, internals_bytes, llm_schema)
    failure_target = diagnostic_target or ctx.address

    def _apply_singleton_refactor_validation(
        prepared: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        prepared = _prepare_singleton_refactor_response(prepared, ctx)
        validate_singleton_refactor_response(
            ctx,
            prepared,
            existing_names=existing_names,
            internals_source=internals_source,
        )
        if pristine_source is not None and input_vectors is not None:
            from src.refactor_parity_gate import check_singleton_parity

            check_singleton_parity(
                pristine_source=pristine_source,
                current_source=internals_source,
                response=prepared,
                ctx=ctx,
                input_vectors=input_vectors,
            )
        return prepared

    def _finalize_singleton_from_llm(
        parsed: SingletonRefactorLLMResponse,
    ) -> SingletonRefactorResponse:
        prepared = prepare_singleton_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        return _apply_singleton_refactor_validation(prepared)

    def _validate_cached_singleton_response(
        cached_response: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        try:
            return _apply_singleton_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="singleton",
                target=failure_target,
                error=error,
                llm_response=cached_response.model_dump(),
                prepared_response=cached_response.model_dump(),
                source="cache",
            )
            logger.error(
                "singleton refactor failed address=%s source=cache diagnostic=%s: %s",
                ctx.address,
                dump_dir,
                error,
            )
            raise

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "singleton refactor cache hit address=%s key=%s",
                ctx.address,
                cache_key[:12],
            )
            return _validate_cached_singleton_response(
                SingletonRefactorResponse.model_validate_json(cached_content)
            )
        except (ValueError, ValidationError) as error:
            if not _refactor_provider_key_present():
                raise
            logger.warning(
                "singleton refactor cache stale address=%s key=%s: %s",
                ctx.address,
                cache_key[:12],
                error,
            )
            del cache[cache_key]
            save_refactor_cache(cache)

    model = refactor_model()
    client, provider = build_client(model)
    context_dump = build_singleton_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
        internals_index=index,
    )
    user_prompt = _prompt_for_singleton_refactor(context_dump)
    last_attempt: dict[str, Any] = {}
    validated_prepared: SingletonRefactorResponse | None = None

    def _post_validate_singleton_llm(
        parsed: SingletonRefactorLLMResponse,
    ) -> SingletonRefactorLLMResponse:
        nonlocal validated_prepared
        last_attempt["llm_response"] = parsed.model_dump()
        raise_if_llm_declared_error(
            parsed,
            kind="singleton",
            target=failure_target,
        )
        prepared = prepare_singleton_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        last_attempt["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_singleton_refactor_validation(prepared)
        return parsed

    logger.info(
        "singleton refactor LLM request address=%s model=%s prompt_version=%s",
        ctx.address,
        model,
        REFACTOR_PROMPT_VERSION,
    )
    try:
        llm_parsed, raw_content = generate_validated_json(
            client=client,
            model=model,
            provider=provider,
            system_prompt=(
                "You rename and refactor one Excel-generated singleton helper into a "
                "domain-aware semantic function. Return only JSON matching the schema."
            ),
            user_prompt=user_prompt,
            response_model=SingletonRefactorLLMResponse,
            post_validate=_post_validate_singleton_llm,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )
    except RefactorDeclaredError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="singleton",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_attempt.get("llm_response"),
            prepared_response=last_attempt.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "singleton refactor aborted by LLM address=%s reason=%s diagnostic=%s",
            ctx.address,
            error.reason,
            dump_dir,
        )
        raise
    except RuntimeError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="singleton",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_attempt.get("llm_response"),
            prepared_response=last_attempt.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "singleton refactor LLM request failed address=%s attempts=%s diagnostic=%s: %s",
            ctx.address,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    parsed = (
        validated_prepared
        if validated_prepared is not None
        else _finalize_singleton_from_llm(llm_parsed)
    )
    _ = raw_content
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def llm_refactor_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> ClusterRefactorResponse:
    llm_schema = ClusterRefactorLLMResponse.model_json_schema()
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    internals_bytes = index.source.encode("utf-8")
    internals_source = index.source
    runtime_source = _read_runtime_source(internals_path)
    existing_names = _function_names(internals_source, index=index)
    cache = load_refactor_cache()
    cache_key = refactor_cache_key(ctx, internals_bytes, llm_schema)
    failure_target = diagnostic_target or f"cluster_{ctx.cluster_id}"

    def _apply_cluster_refactor_validation(
        prepared: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        prepared = _prepare_cluster_refactor_response(prepared, ctx)
        validate_cluster_refactor_response(
            ctx,
            prepared,
            existing_names=existing_names,
            internals_source=internals_source,
        )
        if pristine_source is not None and input_vectors is not None:
            from src.refactor_parity_gate import check_cluster_parity

            check_cluster_parity(
                pristine_source=pristine_source,
                current_source=internals_source,
                response=prepared,
                ctx=ctx,
                input_vectors=input_vectors,
            )
        return prepared

    def _finalize_cluster_from_llm(
        parsed: ClusterRefactorLLMResponse,
    ) -> ClusterRefactorResponse:
        prepared = prepare_cluster_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        return _apply_cluster_refactor_validation(prepared)

    def _validate_cached_cluster_response(
        cached_response: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        try:
            return _apply_cluster_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="cluster",
                target=failure_target,
                error=error,
                llm_response=cached_response.model_dump(),
                prepared_response=cached_response.model_dump(),
                source="cache",
            )
            logger.error(
                "cluster refactor failed cluster_id=%s source=cache diagnostic=%s: %s",
                ctx.cluster_id,
                dump_dir,
                error,
            )
            raise

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "cluster refactor cache hit cluster_id=%s key=%s",
                ctx.cluster_id,
                cache_key[:12],
            )
            return _validate_cached_cluster_response(
                ClusterRefactorResponse.model_validate_json(cached_content)
            )
        except (ValueError, ValidationError) as error:
            if not _refactor_provider_key_present():
                raise
            logger.warning(
                "cluster refactor cache stale cluster_id=%s key=%s: %s",
                ctx.cluster_id,
                cache_key[:12],
                error,
            )
            del cache[cache_key]
            save_refactor_cache(cache)

    model = refactor_model()
    client, provider = build_client(model)
    context_dump = build_cluster_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
        internals_index=index,
    )
    user_prompt = _prompt_for_refactor(context_dump, contract=ctx.contract)
    last_attempt: dict[str, Any] = {}
    validated_prepared: ClusterRefactorResponse | None = None

    def _post_validate_cluster_llm(
        parsed: ClusterRefactorLLMResponse,
    ) -> ClusterRefactorLLMResponse:
        nonlocal validated_prepared
        last_attempt["llm_response"] = parsed.model_dump()
        raise_if_llm_declared_error(
            parsed,
            kind="cluster",
            target=failure_target,
        )
        prepared = prepare_cluster_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        last_attempt["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_cluster_refactor_validation(prepared)
        return parsed

    prompt_member_count = len(
        sample_indices_for_prompt(
            len(ctx.members), limit=CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT
        )
    )
    logger.info(
        "cluster refactor LLM request cluster_id=%s members=%d prompt_members=%d "
        "model=%s prompt_version=%s",
        ctx.cluster_id,
        len(ctx.members),
        prompt_member_count,
        model,
        REFACTOR_PROMPT_VERSION,
    )
    try:
        llm_parsed, raw_content = generate_validated_json(
            client=client,
            model=model,
            provider=provider,
            system_prompt=(
                "You refactor parallel Excel-generated Python helpers into one "
                "domain-aware parameterized function. Return only JSON matching the schema."
            ),
            user_prompt=user_prompt,
            response_model=ClusterRefactorLLMResponse,
            post_validate=_post_validate_cluster_llm,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )
    except RefactorDeclaredError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="cluster",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_attempt.get("llm_response"),
            prepared_response=last_attempt.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "cluster refactor aborted by LLM cluster_id=%s reason=%s diagnostic=%s",
            ctx.cluster_id,
            error.reason,
            dump_dir,
        )
        raise
    except RuntimeError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="cluster",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_attempt.get("llm_response"),
            prepared_response=last_attempt.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "cluster refactor LLM request failed cluster_id=%s attempts=%s diagnostic=%s: %s",
            ctx.cluster_id,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    parsed = (
        validated_prepared
        if validated_prepared is not None
        else _finalize_cluster_from_llm(llm_parsed)
    )
    _ = raw_content
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def _prompt_for_singleton_refactor(
    payload_or_context: dict[str, object] | str,
    response_schema: dict[str, object] | None = None,
) -> str:
    _ = response_schema
    fixed = load_singleton_refactor_prompt_fixed_portion().strip()
    if isinstance(payload_or_context, str):
        return f"{fixed}\n\n{payload_or_context.strip()}"
    payload_json = json.dumps(payload_or_context, indent=2, default=str)
    return f"{fixed}\n\nSingleton context:\n{payload_json}"


def _prompt_for_refactor(
    payload_or_context: dict[str, object] | str,
    response_schema: dict[str, object] | None = None,
    *,
    contract: ClusterRefactorContract = "member_sweep",
) -> str:
    _ = response_schema
    fixed = load_cluster_refactor_prompt_fixed_portion(contract).strip()
    if isinstance(payload_or_context, str):
        return f"{fixed}\n\n{payload_or_context.strip()}"
    payload_json = json.dumps(payload_or_context, indent=2, default=str)
    return f"{fixed}\n\nCluster context:\n{payload_json}"


class _CallSiteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        caller_function: str,
        caller_address: str | None,
        member_addresses: frozenset[str],
        member_functions: set[str],
        lines: list[str],
        sites: list[CallSite],
    ) -> None:
        self.caller_function = caller_function
        self.caller_address = caller_address
        self.member_addresses = member_addresses
        self.member_functions = member_functions
        self.lines = lines
        self.sites = sites

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "xl_eval"
            and len(node.args) >= 3
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and isinstance(node.args[2], ast.Name)
        ):
            address = node.args[1].value
            callee_function = node.args[2].id
            if (
                address in self.member_addresses
                and callee_function in self.member_functions
            ):
                self._append_site(
                    callee_function=callee_function,
                    callee_address=address,
                    pattern="xl_eval",
                    line=node.lineno,
                )
        elif isinstance(node.func, ast.Name) and node.func.id in self.member_functions:
            callee_function = node.func.id
            callee_address = _function_to_primary_address(
                callee_function,
                self.member_addresses,
            )
            if callee_address is not None:
                self._append_site(
                    callee_function=callee_function,
                    callee_address=callee_address,
                    pattern="direct",
                    line=node.lineno,
                )
        self.generic_visit(node)

    def _append_site(
        self,
        *,
        callee_function: str,
        callee_address: str,
        pattern: Literal["xl_eval", "direct"],
        line: int,
    ) -> None:
        snippet = self.lines[line - 1].strip()
        self.sites.append(
            CallSite(
                caller_function=self.caller_function,
                caller_address=self.caller_address,
                callee_function=callee_function,
                callee_address=callee_address,
                pattern=pattern,
                line=line,
                snippet=snippet,
            )
        )


def _caller_address(function_name: str) -> str | None:
    if not function_name.startswith("cell_"):
        return None
    remainder = function_name.removeprefix("cell_")
    parts = remainder.split("_", 1)
    if len(parts) != 2:
        return None
    sheet, colrow = parts[0], parts[1]
    return f"{sheet.title()}!{colrow.upper()}"


def _function_to_primary_address(
    function_name: str,
    member_addresses: frozenset[str],
) -> str | None:
    matches = [
        address
        for address in member_addresses
        if address_to_function_name(address) == function_name
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _function_names(
    source: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> frozenset[str]:
    if index is not None:
        return frozenset(index.functions)
    module = ast.parse(source)
    return frozenset(
        node.name for node in module.body if isinstance(node, ast.FunctionDef)
    )


def _collect_assigned_and_loaded_names(function_def: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names
