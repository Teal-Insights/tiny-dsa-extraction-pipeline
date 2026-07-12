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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    load_key_concept_vocabulary,
    render_literal_helper_call,
)
from src.refactor_order import compute_cluster_refactor_order
from src.runtime_symbols import allowed_runtime_symbols
from src.semantic_naming import (
    BindingRecordHints,
    cluster_binding_naming_hints,
    collect_semantic_helper_names,
    semantic_helpers_available_for_calls,
    binding_record_hints_from_cell,
    validate_semantic_identifier,
)

repo_root = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

REFACTOR_MODEL_ENV = "REFACTOR_MODEL"
REFACTOR_PROMPT_VERSION = 21


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
"""Optional legacy row order hint; prefer ``compute_cluster_refactor_order``."""


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


class HelperParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Python parameter name for the helper, e.g. time_period."
    )
    concept: str = Field(
        description="Binding key concept this parameter varies along, e.g. TIME_PERIOD."
    )
    dtype: str = Field(description="Expected Python dtype for the parameter.")


class MemberKeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept: str = Field(description="Binding key concept name, e.g. TIME_PERIOD.")
    value: str | int | float | bool = Field(
        description="Literal binding key value for this concept."
    )


class MemberKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(description="Workbook address this entry covers.")
    function_name: str = Field(description="Existing cell_* function being replaced.")
    keys: tuple[MemberKeyEntry, ...] = Field(
        description=(
            "Literal binding key values for this address; one entry per concept, "
            "excluding series-constant concepts."
        )
    )

    def keys_dict(self) -> dict[str, BindingKeyValue]:
        return {entry.concept: entry.value for entry in self.keys}


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
            "Economic parameters the helper varies along, tied to binding key concepts."
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


class ClusterRefactorLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_signature: str = Field(
        description=(
            "Python function signature, including `def` keyword, `snake_case` "
            "semantic name, `ctx: EvalContext`, typed economic parameters from "
            "`key_vocabulary`, and return type hint."
        )
    )
    symbol_docstring: str = Field(
        description="Google-style docstring. Include Args and Returns sections."
    )
    symbol_body: str = Field(description="Python function body.")
    parameters: tuple[HelperParameter, ...] = Field(
        description=(
            "Economic parameters the helper varies along, tied to binding key concepts."
        )
    )
    member_keys: tuple[MemberKeys, ...] = Field(
        description=(
            "One entry per cluster member with literal key values for that address."
        )
    )


CLUSTER_REFACTOR_PROMPT_FIXTURE = (
    repo_root / "tests" / "fixtures" / "cluster_refactor_prompt.md"
)


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

    symbol_signature: str = Field(
        description=(
            "Python function signature, including `def` keyword, `snake_case` "
            "semantic name, a single `ctx: EvalContext` argument, and return type hint."
        )
    )
    symbol_docstring: str = Field(
        description="Google-style docstring. Include Args and Returns sections."
    )
    symbol_body: str = Field(description="Python function body.")


ALLOWED_SINGLETON_RETURN_TYPE_HINTS = frozenset({"bool", "float", "int", "str"})
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
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] | None = None,
    key_vocabulary: tuple[KeyConceptSpec, ...] | None = None,
    workbook_path: Path,
    bindings_path: Path,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    layout: ProjectionColumnLayout | None = None,
) -> ClusterRefactorContext | None:
    if len(cluster.members) < 2:
        return None

    resolved_layout = _resolved_projection_layout(layout)

    source = internals_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    defined_functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }

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
                python_source=extract_function_source(source, function_name),
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

    external_dependency_addresses = sorted(
        {
            dependency
            for member in members
            for dependency in member.dependency_addresses
            if dependency not in member_addresses
        }
    )
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source, external_dependency_addresses
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    return ClusterRefactorContext(
        cluster_id=cluster.cluster_id,
        canonical_template=cluster.canonical_template,
        row=cluster.row,
        members=tuple(members),
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(source, member_addresses, member_functions),
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
    )


def build_singleton_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
) -> SingletonRefactorContext | None:
    if len(cluster.members) != 1:
        return None

    address = cluster.members[0]

    source = internals_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    defined_functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }

    function_name = address_to_function_name(address)
    if function_name not in defined_functions:
        return None

    node = projection.get_node(address)
    if node is None or node.normalized_formula is None:
        return None

    dependency_addresses = tuple(sorted(projection.get_dependencies(address)))
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source, dependency_addresses
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    return SingletonRefactorContext(
        address=address,
        function_name=function_name,
        canonical_template=cluster.canonical_template,
        normalized_formula=node.normalized_formula,
        python_source=extract_function_source(source, function_name),
        dependency_addresses=dependency_addresses,
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(
            source,
            frozenset({address}),
            {function_name},
        ),
        allowed_runtime_symbols=allowed_runtime_symbols(),
        naming_hints=_binding_hints_for_address(
            internal_binding_index, address
        ).to_payload(),
    )


def extract_function_source(source: str, function_name: str) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise KeyError(f"Function {function_name!r} not found in internals source")


def scan_call_sites(
    source: str,
    member_addresses: frozenset[str],
    member_functions: set[str],
) -> tuple[CallSite, ...]:
    module = ast.parse(source)
    lines = source.splitlines()
    sites: list[CallSite] = []

    for top_level in module.body:
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
    varying_concepts = frozenset(
        concept for keys in ctx.expected_member_keys.values() for concept in keys
    )
    parameter_names = [
        item.suggested_param_name
        for item in ctx.key_vocabulary
        if item.concept in varying_concepts
    ]
    return {
        "cluster_id": ctx.cluster_id,
        "row": ctx.row,
        "canonical_template": ctx.canonical_template,
        "first_year_column": ctx.first_year_column,
        "key_vocabulary": [
            {
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
) -> ClusterRefactorResponse:
    concept_by_name = {
        parameter.name: parameter.concept for parameter in response.parameters
    }
    normalized_entries: list[MemberKeys] = []
    for entry in response.member_keys:
        normalized_entries_list: list[MemberKeyEntry] = []
        for key_entry in entry.keys:
            concept = concept_by_name.get(key_entry.concept, key_entry.concept)
            normalized_entries_list.append(
                MemberKeyEntry(concept=concept, value=key_entry.value)
            )
        normalized_entries.append(
            entry.model_copy(update={"keys": tuple(normalized_entries_list)})
        )
    return response.model_copy(update={"member_keys": tuple(normalized_entries)})


def _prepare_cluster_refactor_response(
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> ClusterRefactorResponse:
    _ = ctx
    response = _normalize_member_key_concepts(response)
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


def _local_binding_names(function_def: ast.FunctionDef) -> set[str]:
    parameter_names = {arg.arg for arg in function_def.args.args}
    names: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_from_target(target))
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


def _suggested_param_name_by_concept(
    key_vocabulary: tuple[KeyConceptSpec, ...],
) -> dict[str, str]:
    return {item.concept: item.suggested_param_name for item in key_vocabulary}


def validate_parameter_names_match_vocabulary(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
) -> None:
    suggested = _suggested_param_name_by_concept(ctx.key_vocabulary)
    mismatches = sorted(
        {
            f"{parameter.concept!r}: expected {suggested[parameter.concept]!r}, "
            f"got {parameter.name!r}"
            for parameter in response.parameters
            if parameter.concept in suggested
            and parameter.name != suggested[parameter.concept]
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
    parameter_names = {arg.arg for arg in function_def.args.args}
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


def validate_cluster_refactor_response(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
) -> None:
    validate_semantic_identifier(
        response.helper_name,
        existing_names=existing_names,
    )

    member_key_addresses = {entry.address for entry in response.member_keys}
    member_addresses = {member.address for member in ctx.members}
    if member_key_addresses != member_addresses:
        raise ValueError(
            "member_keys must cover cluster members exactly: "
            f"expected {sorted(member_addresses)}, got {sorted(member_key_addresses)}"
        )

    parameter_concepts = {parameter.concept for parameter in response.parameters}
    vocabulary_concepts = {item.concept for item in ctx.key_vocabulary}
    unknown_parameters = sorted(parameter_concepts - vocabulary_concepts)
    if unknown_parameters:
        raise ValueError(
            f"parameters reference unknown binding concepts: {unknown_parameters}"
        )

    parameter_names = [parameter.name for parameter in response.parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ValueError("parameter names must be unique")
    for parameter in response.parameters:
        if not parameter.name.isidentifier():
            raise ValueError(
                f"parameter name is not a valid identifier: {parameter.name!r}"
            )

    concept_sets = [
        frozenset(ctx.expected_member_keys[member.address].keys())
        for member in ctx.members
    ]
    if len(set(concept_sets)) != 1:
        raise ValueError("cluster members must share one varying key concept set")
    varying_concepts = concept_sets[0]
    if parameter_concepts != varying_concepts:
        raise ValueError(
            "parameters must match varying binding key concepts for the cluster: "
            f"expected {sorted(varying_concepts)}, got {sorted(parameter_concepts)}"
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
        extra_concepts = set(entry_keys) - parameter_concepts
        if extra_concepts:
            raise ValueError(
                f"member_keys for {entry.address} must not include "
                f"series-constant concepts: {sorted(extra_concepts)}"
            )
        missing_concepts = parameter_concepts - set(entry_keys)
        if missing_concepts:
            raise ValueError(
                f"member_keys for {entry.address} missing parameter concepts: "
                f"{sorted(missing_concepts)}"
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
        for concept, expected_value in expected_keys.items():
            actual_value = entry_keys.get(concept)
            if actual_value != expected_value:
                raise ValueError(
                    f"member_keys for {entry.address} has {concept}={actual_value!r}, "
                    f"expected {expected_value!r}"
                )
        engine_column = engine_column_from_member_keys(
            entry_keys,
            address=entry.address,
            layout=_resolved_projection_layout(),
        )
        if engine_column is None:
            raise ValueError(
                f"member_keys for {entry.address} do not resolve to an engine column"
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

    allowed_names = (
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


def parse_singleton_return_type_hint(signature: str) -> str:
    match = re.search(r"->\s*(.+?)\s*:?\s*$", signature.strip())
    if match is None:
        raise ValueError(f"no return type hint in signature: {signature!r}")
    return match.group(1).strip().removesuffix(":")


def validate_singleton_return_type_hint(hint: str) -> None:
    parts = [part.strip() for part in hint.split("|")]
    for part in parts:
        if part not in ALLOWED_SINGLETON_RETURN_TYPE_HINTS:
            raise ValueError(f"unsupported return type hint: {hint!r}")


def _parse_symbol_name_from_signature(signature: str) -> str:
    match = re.match(r"def\s+(\w+)\s*\(", signature.strip())
    if match is None:
        raise ValueError(f"invalid function signature: {signature!r}")
    return match.group(1)


def prepare_singleton_refactor_response(
    llm_response: SingletonRefactorLLMResponse,
    ctx: SingletonRefactorContext,
) -> SingletonRefactorResponse:
    validate_singleton_return_type_hint(
        parse_singleton_return_type_hint(llm_response.symbol_signature)
    )
    docstring = strip_python_string_delimiters(llm_response.symbol_docstring)
    docstring = append_refactor_note_section(
        docstring,
        address=ctx.address,
        formula=ctx.normalized_formula,
    )
    symbol_source = assemble_singleton_symbol_source(
        signature=llm_response.symbol_signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    return SingletonRefactorResponse(
        symbol_name=_parse_symbol_name_from_signature(llm_response.symbol_signature),
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
) -> str:
    yaml_block = _format_cell_metadata_yaml(cell_metadata)
    return (
        "Function to refactor:\n\n"
        f"```python\n{function_source.strip()}\n```\n\n"
        "Cell metadata:\n\n"
        f"```yaml\n{yaml_block}\n```\n\n"
        "Dependencies:\n\n"
        f"```python\n{dependency_stubs.strip()}\n```"
    )


def _function_defs_by_name(source: str) -> dict[str, ast.FunctionDef]:
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


def _function_signature_line(function_def: ast.FunctionDef) -> str:
    args = ast.unparse(function_def.args)
    returns = (
        f" -> {ast.unparse(function_def.returns)}"
        if function_def.returns is not None
        else ""
    )
    return f"def {function_def.name}({args}){returns}:"


def _strip_note_section(docstring: str) -> str:
    dedented = textwrap.dedent(docstring).strip()
    for pattern in (r"\n\nNote:\n.*\Z", r"\nNote:\n.*\Z"):
        match = re.search(pattern, dedented, re.DOTALL)
        if match is not None:
            return dedented[: match.start()].rstrip()
    return dedented


def _format_runtime_dependency_stub(function_def: ast.FunctionDef) -> str:
    docstring = _function_docstring(function_def)
    lines = [_function_signature_line(function_def)]
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
) -> str:
    called = _called_function_names(function_source)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = _function_defs_by_name(internals_source)
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
) -> str:
    function_source = extract_function_source(internals_source, function_name)
    metadata = dict(cell_metadata)
    metadata["address"] = address
    dependency_stubs = _build_dependency_stubs(
        function_source=function_source,
        internals_source=internals_source,
        runtime_source=runtime_source,
    )
    return format_singleton_refactor_context_dump(
        function_source=function_source,
        cell_metadata=metadata,
        dependency_stubs=dependency_stubs,
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
) -> str:
    resolved_runtime_path = (
        runtime_path
        if runtime_path is not None
        else internals_path.parent / "runtime.py"
    )
    return build_singleton_refactor_context_dump(
        function_name=ctx.function_name,
        address=ctx.address,
        internals_source=internals_path.read_text(encoding="utf-8"),
        runtime_source=resolved_runtime_path.read_text(encoding="utf-8"),
        cell_metadata=_cell_metadata_for_singleton_refactor(ctx),
    )


def load_cluster_refactor_prompt_fixed_portion() -> str:
    return CLUSTER_REFACTOR_PROMPT_FIXTURE.read_text(encoding="utf-8")


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


def parse_cluster_return_type_hint(signature: str) -> str:
    return parse_singleton_return_type_hint(signature)


validate_cluster_return_type_hint = validate_singleton_return_type_hint


def prepare_cluster_refactor_response(
    llm_response: ClusterRefactorLLMResponse,
    ctx: ClusterRefactorContext,
) -> ClusterRefactorResponse:
    validate_singleton_return_type_hint(
        parse_cluster_return_type_hint(llm_response.symbol_signature)
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
        signature=llm_response.symbol_signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    return ClusterRefactorResponse(
        helper_name=_parse_symbol_name_from_signature(llm_response.symbol_signature),
        helper_docstring=docstring,
        helper_source=helper_source,
        parameters=llm_response.parameters,
        member_keys=llm_response.member_keys,
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
        lines.append(f"- concept: {item.concept}")
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
) -> str:
    vocabulary_yaml = _format_key_vocabulary_yaml(key_vocabulary)
    metadata_yaml = _format_member_metadata_yaml(member_metadata)
    return (
        "Cluster to refactor:\n\n"
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
) -> str:
    function_sources = [
        extract_function_source(internals_source, function_name)
        for function_name in member_function_names
    ]
    called = _called_function_names_from_sources(function_sources)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = _function_defs_by_name(internals_source)
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


def build_cluster_refactor_context_dump(
    *,
    member_function_names: Sequence[str],
    internals_source: str,
    runtime_source: str,
    key_vocabulary: Sequence[KeyConceptSpec],
    member_metadata: Sequence[Mapping[str, object]],
) -> str:
    member_sources = "\n\n\n".join(
        extract_function_source(internals_source, function_name).strip()
        for function_name in member_function_names
    )
    dependency_stubs = _build_cluster_dependency_stubs(
        member_function_names=member_function_names,
        internals_source=internals_source,
        runtime_source=runtime_source,
    )
    return format_cluster_refactor_context_dump(
        member_sources=member_sources,
        key_vocabulary=key_vocabulary,
        member_metadata=member_metadata,
        dependency_stubs=dependency_stubs,
    )


def _member_metadata_for_cluster_refactor(
    ctx: ClusterRefactorContext,
) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    for member in ctx.members:
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
) -> str:
    varying_concepts = frozenset(
        concept for keys in ctx.expected_member_keys.values() for concept in keys
    )
    resolved_runtime_path = (
        runtime_path
        if runtime_path is not None
        else internals_path.parent / "runtime.py"
    )
    return build_cluster_refactor_context_dump(
        member_function_names=tuple(member.function_name for member in ctx.members),
        internals_source=internals_path.read_text(encoding="utf-8"),
        runtime_source=resolved_runtime_path.read_text(encoding="utf-8"),
        key_vocabulary=tuple(
            item for item in ctx.key_vocabulary if item.concept in varying_concepts
        ),
        member_metadata=_member_metadata_for_cluster_refactor(ctx),
    )


def _cluster_runtime_imports(response: ClusterRefactorResponse) -> set[str]:
    imports: set[str] = set()
    if "EvalContext" in response.helper_source:
        imports.add("EvalContext")
    if "CellValue" in response.helper_source:
        imports.add("CellValue")
    return imports


def ensure_cluster_refactor_imports(
    source: str,
    response: ClusterRefactorResponse,
) -> str:
    needed = _cluster_runtime_imports(response)
    if not needed:
        return source
    return _merge_runtime_imports(source, needed)


def _singleton_runtime_imports(response: SingletonRefactorResponse) -> set[str]:
    imports: set[str] = set()
    if "EvalContext" in response.symbol_source:
        imports.add("EvalContext")
    return imports


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
    }


def validate_singleton_refactor_response(
    ctx: SingletonRefactorContext,
    response: SingletonRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
) -> None:
    _ = ctx
    validate_semantic_identifier(
        response.symbol_name,
        existing_names=existing_names,
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

    allowed_names = (
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
        (parameter.name, parameter.concept) for parameter in response.parameters
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
    return {parameter.name: keys[parameter.concept] for parameter in parameters}


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
    rewrite_count = 0
    updated = source
    for binding in bindings:
        updated, count = _rewrite_xl_eval_collapse_binding(updated, binding)
        rewrite_count += count
        updated, count = _rewrite_direct_collapse_binding(updated, binding)
        rewrite_count += count
    return updated, rewrite_count


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
) -> tuple[tuple[SemanticDependency, ...], tuple[str, ...]]:
    """Resolve external ``cell_*`` dependencies to the semantic helpers wrapping them."""
    module = ast.parse(source)
    defined_functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unresolved: set[str] = set()
    semantic_helpers = collect_semantic_helper_names(source)
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

        collapsed = _infer_collapsed_semantic_dependency(source, address)
        if collapsed is None:
            unresolved.add(function_name)
            continue
        helper_name, parameter_name = collapsed
        grouped[helper_name].append((address, parameter_name))

    semantic_dependencies = tuple(
        SemanticDependency(
            helper_name=helper_name,
            call_form=_helper_pass_through_call_form(source, helper_name),
            address_template=_column_address_template(sorted(entries)[0][0]),
            columns=tuple(tag for _, tag in sorted(entries)),
            addresses=tuple(address for address, _ in sorted(entries)),
        )
        for helper_name, entries in sorted(grouped.items())
    )
    return semantic_dependencies, tuple(sorted(unresolved))


def _helper_pass_through_call_form(source: str, helper_name: str) -> str:
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == helper_name:
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
) -> tuple[str, str] | None:
    dispatch = _parse_address_dispatch(source) or {}
    if address in dispatch:
        helper_name, key_kwargs = dispatch[address]
        if helper_name not in collect_semantic_helper_names(source):
            return None
        if len(key_kwargs) == 1:
            return helper_name, next(iter(key_kwargs))
        return helper_name, next(iter(key_kwargs))

    symbol_dispatch = _parse_symbol_dispatch(source)
    symbol_name = symbol_dispatch.get(address)
    if symbol_name is not None:
        if symbol_name not in collect_semantic_helper_names(source):
            return None
        return symbol_name, ""

    module = ast.parse(source)
    defined_functions = {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }
    matches: list[tuple[str, str]] = []
    for helper_name in sorted(collect_semantic_helper_names(source)):
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


def _parse_address_dispatch(source: str) -> AddressDispatch | None:
    module = ast.parse(source)
    for node in module.body:
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


def _parse_symbol_dispatch(source: str) -> dict[str, str]:
    module = ast.parse(source)
    for node in module.body:
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


def _rewrite_xl_eval_collapse_binding(
    source: str,
    binding: CollapseBinding,
) -> tuple[str, int]:
    module = ast.parse(source)
    replacements: list[tuple[int, int, str, str]] = []
    for function_def in _iter_function_defs(module.body):
        visitor = _XlEvalCollapseRewriteVisitor(
            binding=binding,
            source=source,
            replacements=replacements,
        )
        visitor.visit(function_def)
    if not replacements:
        return source, 0
    return _apply_segment_replacements(source, replacements), len(replacements)


def _rewrite_direct_collapse_binding(
    source: str,
    binding: CollapseBinding,
) -> tuple[str, int]:
    module = ast.parse(source)
    replacements: list[tuple[int, int, str, str]] = []
    for function_def in _iter_function_defs(module.body):
        visitor = _DirectCollapseRewriteVisitor(
            binding=binding,
            source=source,
            replacements=replacements,
        )
        visitor.visit(function_def)
    if not replacements:
        return source, 0
    return _apply_segment_replacements(source, replacements), len(replacements)


class _XlEvalCollapseRewriteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        binding: CollapseBinding,
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.binding = binding
        self.source = source
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        if _xl_eval_matches_collapse(node, self.binding):
            segment = ast.get_source_segment(self.source, node)
            if segment is not None:
                self.replacements.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        segment,
                        self.binding.literal_call,
                    )
                )
        self.generic_visit(node)


class _DirectCollapseRewriteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        binding: CollapseBinding,
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.binding = binding
        self.source = source
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == self.binding.function_name
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "ctx"
        ):
            segment = ast.get_source_segment(self.source, node)
            if segment is not None:
                self.replacements.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        segment,
                        self.binding.literal_call,
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
) -> SingletonRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
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
) -> ClusterRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_cluster(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
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
    dry_run: bool = False,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    parity_gate: bool = True,
    layout: ProjectionColumnLayout | None = None,
) -> tuple[ClusterRefactorApplyResult, ...]:
    """Refactor every eligible cluster in unified dependency order.

    When ``parity_gate`` is enabled, each refactored helper is checked against the
    pristine pre-refactor cell semantics across several input vectors before its
    transaction is committed; a divergence rolls back and re-prompts the model.
    """
    pristine_source: str | None = None
    input_vectors: Sequence[Mapping[str, object]] | None = None
    if parity_gate:
        from src.refactor_parity_gate import build_default_input_vectors

        pristine_source = internals_path.read_text(encoding="utf-8")
        input_vectors = build_default_input_vectors()

    ordered_clusters = compute_cluster_refactor_order(projection, clusters)
    results: list[ClusterRefactorApplyResult] = []
    responses: list[ClusterRefactorResponse] = []
    refactored_any = False
    for cluster in ordered_clusters:
        if len(cluster.members) == 1:
            ctx = build_singleton_refactor_context(
                projection,
                cluster,
                internals_path,
                source_graph=source_graph,
                internal_binding_index=internal_binding_index,
            )
            if ctx is None:
                continue
            refactor_internals_singleton(
                ctx,
                internals_path=internals_path,
                dry_run=dry_run,
                pristine_source=pristine_source,
                input_vectors=input_vectors,
                source_graph=source_graph,
            )
            refactored_any = True
            continue

        ctx = build_cluster_refactor_context(
            projection,
            cluster,
            internals_path,
            source_graph=source_graph,
            internal_binding_index=internal_binding_index,
            bindings_path=bindings_path,
            workbook_path=workbook_path,
            layout=layout,
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
        )
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


def llm_refactor_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
) -> SingletonRefactorResponse:
    llm_schema = SingletonRefactorLLMResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    internals_source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(internals_source)
    cache = load_refactor_cache()
    cache_key = singleton_refactor_cache_key(ctx, internals_bytes, llm_schema)

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
        prepared = prepare_singleton_refactor_response(parsed, ctx)
        return _apply_singleton_refactor_validation(prepared)

    def _validate_cached_singleton_response(
        cached_response: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        try:
            return _apply_singleton_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="singleton",
                target=ctx.address,
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
    )
    user_prompt = _prompt_for_singleton_refactor(context_dump)
    last_attempt: dict[str, Any] = {}
    validated_prepared: SingletonRefactorResponse | None = None

    def _post_validate_singleton_llm(
        parsed: SingletonRefactorLLMResponse,
    ) -> SingletonRefactorLLMResponse:
        nonlocal validated_prepared
        prepared = prepare_singleton_refactor_response(parsed, ctx)
        last_attempt["llm_response"] = parsed.model_dump()
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
    except RuntimeError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="singleton",
            target=ctx.address,
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
) -> ClusterRefactorResponse:
    llm_schema = ClusterRefactorLLMResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    internals_source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(internals_source)
    cache = load_refactor_cache()
    cache_key = refactor_cache_key(ctx, internals_bytes, llm_schema)

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
        prepared = prepare_cluster_refactor_response(parsed, ctx)
        return _apply_cluster_refactor_validation(prepared)

    def _validate_cached_cluster_response(
        cached_response: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        try:
            return _apply_cluster_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="cluster",
                target=f"cluster_{ctx.cluster_id}",
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
    )
    user_prompt = _prompt_for_refactor(context_dump)
    last_attempt: dict[str, Any] = {}
    validated_prepared: ClusterRefactorResponse | None = None

    def _post_validate_cluster_llm(
        parsed: ClusterRefactorLLMResponse,
    ) -> ClusterRefactorLLMResponse:
        nonlocal validated_prepared
        prepared = prepare_cluster_refactor_response(parsed, ctx)
        last_attempt["llm_response"] = parsed.model_dump()
        last_attempt["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_cluster_refactor_validation(prepared)
        return parsed

    logger.info(
        "cluster refactor LLM request cluster_id=%s members=%d model=%s prompt_version=%s",
        ctx.cluster_id,
        len(ctx.members),
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
    except RuntimeError as error:
        dump_dir = write_refactor_failure_diagnostic(
            kind="cluster",
            target=f"cluster_{ctx.cluster_id}",
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
) -> str:
    _ = response_schema
    fixed = load_cluster_refactor_prompt_fixed_portion().strip()
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


def _function_names(source: str) -> frozenset[str]:
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
