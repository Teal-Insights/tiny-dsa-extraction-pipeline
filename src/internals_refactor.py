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
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.formula_clustering import FormulaCluster
from src.llm_json import generate_validated_json
from src.llm_providers import build_client, model_from_env, provider_for_model
from src.pipeline_context import projection_layout as active_projection_layout
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address
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
from src.refactor_order import (
    compute_multi_member_cluster_refactor_order,
    compute_singleton_cluster_refactor_order,
)
from src.runtime_symbols import allowed_runtime_symbols
from src.semantic_naming import (
    SemanticLabelHints,
    cluster_naming_hints,
    collect_semantic_helper_names,
    semantic_helpers_available_for_calls,
    semantic_label_hints_from_metadata,
    validate_semantic_identifier,
)

repo_root = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

REFACTOR_MODEL_ENV = "REFACTOR_MODEL"
REFACTOR_PROMPT_VERSION = 15


def refactor_model() -> str:
    return model_from_env(REFACTOR_MODEL_ENV)


def _refactor_provider_key_present() -> bool:
    provider = provider_for_model(refactor_model())
    return bool(os.environ.get(provider.api_key_env))


REFACTOR_CACHE_PATH = repo_root / ".cache/internals-refactors.json"
FORMULA_SECTION_MARKER = "# --- Formula cell functions ---"
RESOLVER_SECTION_MARKER = "# --- Formula resolver ---"

AddressDispatch = dict[str, tuple[str, dict[str, BindingKeyValue]]]

REFACTOR_ROW_ORDER: tuple[int, ...] = ()
"""Optional legacy row order hint; prefer ``compute_multi_member_cluster_refactor_order``."""


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
    table_labels: str | None = None
    row_labels: str | None = None
    column_labels: str | None = None


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
    uses_first_year_branch: bool = Field(
        description="True when helper branches on engine_column == first_year_column."
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


@dataclass(frozen=True)
class SingletonRefactorContext:
    address: str
    function_name: str
    canonical_template: str
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    external_dependencies: tuple[str, ...]
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

    _graph, _series_bindings, input_series, output_series = build_pipeline_graph(
        require_pipeline_config()
    )
    return build_bound_address_keys(input_series, output_series)


def _label_hints_for_address(
    source_graph: DependencyGraph | None,
    address: str,
) -> SemanticLabelHints:
    if source_graph is None:
        return SemanticLabelHints()
    node = source_graph.get_node(address)
    if node is None:
        return SemanticLabelHints()
    return semantic_label_hints_from_metadata(node.metadata)


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
    layout: ProjectionColumnLayout | None = None,
) -> ClusterRefactorContext | None:
    if len(cluster.members) < 2:
        return None

    resolved_source_graph = (
        source_graph if source_graph is not None else _default_source_graph()
    )
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

        label_hints = _label_hints_for_address(resolved_source_graph, address)

        members.append(
            MemberContext(
                address=address,
                function_name=function_name,
                engine_column=engine_column,
                normalized_formula=node.normalized_formula,
                python_source=extract_function_source(source, function_name),
                dependency_addresses=dependency_addresses,
                dependency_functions=dependency_functions,
                table_labels=label_hints.table_labels,
                row_labels=label_hints.row_labels,
                column_labels=label_hints.column_labels,
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
        naming_hints=cluster_naming_hints(
            tuple(
                SemanticLabelHints(
                    table_labels=member.table_labels,
                    row_labels=member.row_labels,
                    column_labels=member.column_labels,
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
) -> SingletonRefactorContext | None:
    if len(cluster.members) != 1:
        return None

    resolved_source_graph = (
        source_graph if source_graph is not None else _default_source_graph()
    )

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
    external_dependencies = tuple(
        sorted(
            address_to_function_name(dependency)
            for dependency in dependency_addresses
            if address_to_function_name(dependency) in defined_functions
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
        call_sites=scan_call_sites(
            source,
            frozenset({address}),
            {function_name},
        ),
        allowed_runtime_symbols=allowed_runtime_symbols(),
        naming_hints=_label_hints_for_address(
            resolved_source_graph, address
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
                "table_labels": member.table_labels,
                "row_labels": member.row_labels,
                "column_labels": member.column_labels,
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


_FIRST_YEAR_BRANCH_PATTERN = re.compile(
    r"(first_year_column|time_period\s*==\s*1|time_period\s*<=\s*1)"
)


def validate_uses_first_year_branch_flag(
    response: ClusterRefactorResponse,
) -> None:
    references = _FIRST_YEAR_BRANCH_PATTERN.search(response.helper_source) is not None
    if response.uses_first_year_branch and not references:
        raise ValueError(
            "uses_first_year_branch is True but helper_source does not reference "
            "first-year branching (first_year_column or time_period == 1)"
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
    validate_uses_first_year_branch_flag(response)

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


def singleton_prompt_payload(ctx: SingletonRefactorContext) -> dict[str, object]:
    return {
        "address": ctx.address,
        "function_name": ctx.function_name,
        "canonical_template": ctx.canonical_template,
        "normalized_formula": ctx.normalized_formula,
        "python_source": ctx.python_source,
        "dependency_addresses": list(ctx.dependency_addresses),
        "external_dependencies": list(ctx.external_dependencies),
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


def rename_function_references(
    source: str,
    old_name: str,
    new_name: str,
) -> tuple[str, int]:
    module = ast.parse(source)
    replacements: list[tuple[int, int, str, str]] = []

    for top_level in module.body:
        if not isinstance(top_level, ast.FunctionDef):
            continue
        visitor = _FunctionReferenceRenameVisitor(
            old_name=old_name,
            new_name=new_name,
            source=source,
            replacements=replacements,
        )
        visitor.visit(top_level)

    if not replacements:
        return source, 0
    return _apply_segment_replacements(source, replacements), len(replacements)


def apply_singleton_refactor_plan(
    source: str,
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> tuple[str, int]:
    updated = _replace_function_definition(
        source,
        ctx.function_name,
        response.symbol_source,
    )
    updated, rewrite_count = rename_function_references(
        updated,
        ctx.function_name,
        response.symbol_name,
    )
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
    updated = insert_helper_source(source, response.helper_source)
    bindings = collapse_bindings_for_response(response)
    updated, rewrite_count = substitute_collapse_bindings(updated, bindings)
    collapsed_functions = frozenset(
        entry.function_name for entry in response.member_keys
    )
    updated = _remove_function_definitions(updated, collapsed_functions)
    dispatch_updates = _dispatch_entries_for_collapse(response)
    if dispatch_updates:
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
) -> SingletonRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
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


def refactor_internals_all_singletons(
    projection: ProjectionResult,
    clusters: tuple[FormulaCluster, ...],
    *,
    internals_path: Path,
    dry_run: bool = False,
    source_graph: DependencyGraph | None = None,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
) -> tuple[SingletonRefactorApplyResult, ...]:
    results: list[SingletonRefactorApplyResult] = []
    for cluster in compute_singleton_cluster_refactor_order(projection, clusters):
        ctx = build_singleton_refactor_context(
            projection,
            cluster,
            internals_path,
            source_graph=source_graph,
        )
        if ctx is None:
            continue
        result = refactor_internals_singleton(
            ctx,
            internals_path=internals_path,
            dry_run=dry_run,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
        )
        results.append(result)
    return tuple(results)


def refactor_internals_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    response: ClusterRefactorResponse | None = None,
    dry_run: bool = False,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
) -> ClusterRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_cluster(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
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
    parity_gate: bool = True,
    layout: ProjectionColumnLayout | None = None,
) -> tuple[ClusterRefactorApplyResult, ...]:
    """Refactor singletons, then every multi-member cluster in dependency order.

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

    refactor_internals_all_singletons(
        projection,
        clusters,
        internals_path=internals_path,
        dry_run=dry_run,
        source_graph=source_graph,
        pristine_source=pristine_source,
        input_vectors=input_vectors,
    )
    ordered_clusters = compute_multi_member_cluster_refactor_order(projection, clusters)
    results: list[ClusterRefactorApplyResult] = []
    responses: list[ClusterRefactorResponse] = []
    for cluster in ordered_clusters:
        ctx = build_cluster_refactor_context(
            projection,
            cluster,
            internals_path,
            source_graph=source_graph,
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
        )
        results.append(result)
        responses.append(result.response)

    if not dry_run and responses:
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
) -> SingletonRefactorResponse:
    schema = SingletonRefactorResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    internals_source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(internals_source)
    cache = load_refactor_cache()
    cache_key = singleton_refactor_cache_key(ctx, internals_bytes, schema)

    def _prepare_and_validate_singleton(
        parsed: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        prepared = _prepare_singleton_refactor_response(parsed, ctx)
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

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "singleton refactor cache hit address=%s key=%s",
                ctx.address,
                cache_key[:12],
            )
            return _prepare_and_validate_singleton(
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
    payload = singleton_prompt_payload(ctx)
    logger.info(
        "singleton refactor LLM request address=%s model=%s prompt_version=%s",
        ctx.address,
        model,
        REFACTOR_PROMPT_VERSION,
    )
    parsed, _ = generate_validated_json(
        client=client,
        model=model,
        provider=provider,
        system_prompt=(
            "You rename and refactor one Excel-generated singleton helper "
            "into a semantic function. Return only JSON matching the schema. "
            "Preserve semantics exactly; do not algebraically simplify. "
            "Write Google-style docstrings with Args and Returns sections. "
            "Use only runtime symbols listed in constraints.allowed_runtime_symbols. "
            "Choose domain-meaningful snake_case names for the symbol and locals."
        ),
        user_prompt=_prompt_for_singleton_refactor(payload, schema),
        response_model=SingletonRefactorResponse,
        post_validate=_prepare_and_validate_singleton,
    )
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def llm_refactor_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
) -> ClusterRefactorResponse:
    schema = ClusterRefactorResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    internals_source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(internals_source)
    cache = load_refactor_cache()
    cache_key = refactor_cache_key(ctx, internals_bytes, schema)

    def _prepare_and_validate_cluster(
        parsed: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        prepared = _prepare_cluster_refactor_response(parsed, ctx)
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

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "cluster refactor cache hit cluster_id=%s key=%s",
                ctx.cluster_id,
                cache_key[:12],
            )
            return _prepare_and_validate_cluster(
                ClusterRefactorResponse.model_validate_json(cached_content)
            )
        except (ValueError, ValidationError) as error:
            # A cached response that no longer satisfies the gate is stale or
            # broken: drop it and regenerate (which re-prompts on failure).
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
    payload = prompt_payload(ctx)
    logger.info(
        "cluster refactor LLM request cluster_id=%s members=%d model=%s prompt_version=%s",
        ctx.cluster_id,
        len(ctx.members),
        model,
        REFACTOR_PROMPT_VERSION,
    )
    parsed, _ = generate_validated_json(
        client=client,
        model=model,
        provider=provider,
        system_prompt=(
            "You refactor parallel Excel-generated Python helpers into one "
            "parameterized function. Return only JSON matching the schema. "
            "Preserve semantics exactly; do not algebraically simplify. "
            "Write Google-style docstrings with Args and Returns sections. "
            "Use only runtime symbols listed in constraints.allowed_runtime_symbols. "
            "Parameter names must match suggested_param_name from key_vocabulary. "
            "Emit one helper function; do not nest helpers or import modules."
        ),
        user_prompt=_prompt_for_refactor(payload, schema),
        response_model=ClusterRefactorResponse,
        post_validate=_prepare_and_validate_cluster,
    )
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def _prompt_for_singleton_refactor(
    payload: dict[str, object], response_schema: dict[str, object]
) -> str:
    payload_json = json.dumps(payload, indent=2, default=str)
    schema_json = json.dumps(response_schema, indent=2)
    constraints = payload.get("constraints", {})
    if not isinstance(constraints, dict):
        constraints = {}
    raw_allowed = constraints.get("allowed_runtime_symbols", [])
    allowed_symbols = list(raw_allowed) if isinstance(raw_allowed, list) else []
    allowed_symbols_json = json.dumps(allowed_symbols, indent=2)
    return f"""
Rename and refactor one Excel-generated singleton helper into a semantic function.

Rules:
- Keep signature (ctx) exactly; do not add parameters.
- Do not rename dependency functions.
- Choose symbol_name as a clear snake_case semantic identifier informed by naming_hints.
- Rename local temporaries to domain-meaningful snake_case informed by naming_hints.
- Do not use excel-shaped locals such as _t1, t2, b21, col10, choose1, func_map, or input17.
- Do not reference cell_* helpers; call semantic helpers already present in internals.py.
- Call only these runtime symbols (plus semantic helpers from external_dependencies):
{allowed_symbols_json}
- Emit one complete symbol_source function with signature (ctx); no nested helpers or imports.
- symbol_source must include a Google-style docstring with Args and Returns sections.
- symbol_docstring must match the docstring embedded in symbol_source exactly.
- Include a Note section listing the workbook address and Excel formula.
- Return only JSON matching the response schema.

Example docstring shape:
\"\"\"
Look up the initial debt-to-GDP ratio for the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio from the country profile table.

Note:
    Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).
\"\"\"

Example case-switch shape (use a lookup table, not an if/elif ladder):
year_address = {{1991: 'SomeSheet!C1', 1992: 'SomeSheet!D1', 1993: 'SomeSheet!E1', 1994: 'SomeSheet!F1', 1995: 'SomeSheet!G1'}}.get(time_period)
if year_address is None:
    return XlError.VALUE
year = xl_cell(ctx, year_address)

Singleton context:
{payload_json}

Response schema:
{schema_json}
""".strip()


def _prompt_for_refactor(
    payload: dict[str, object], response_schema: dict[str, object]
) -> str:
    payload_json = json.dumps(payload, indent=2, default=str)
    schema_json = json.dumps(response_schema, indent=2)
    constraints = payload.get("constraints", {})
    if not isinstance(constraints, dict):
        constraints = {}
    raw_allowed = constraints.get("allowed_runtime_symbols", [])
    allowed_symbols = list(raw_allowed) if isinstance(raw_allowed, list) else []
    allowed_symbols_json = json.dumps(allowed_symbols, indent=2)
    return f"""
Refactor one parallel formula cluster into a single parameterized helper.

Rules:
- Declare parameters[] using binding key concepts from key_vocabulary; do not use column letters.
- Use suggested_param_name from key_vocabulary as each parameter's Python name.
- For each cluster member, emit member_keys[] with keys[] entries copied from expected_keys.
- member_keys[].keys[].concept must use binding concept names (e.g. TIME_PERIOD), not parameter names.
- Series-constant binding keys (scope: series) are not parameters; bake them into the helper.
- Emit helper_source with signature (ctx, <parameters>) using the suggested parameter names.
- Set uses_first_year_branch true when the helper branches on first-year {{PRIOR_DEBT}} logic.
- Choose helper_name as a clear snake_case semantic identifier informed by naming_hints.
- Use time_period == 1 or engine_column == first_year_column for first-year branching when needed.
- Map time_period to workbook columns internally when reading xl_cell addresses.
- Keep xl_eval only for leaf inputs read with xl_cell; never for refactored cells.
- Do not rename dependency functions.
- Rename local temporaries to domain-meaningful snake_case informed by naming_hints.
- Do not use excel-shaped locals such as _t1, t2, b21, col10, choose1, func_map, or input17.
- Do not reference cell_* helpers anywhere in the body.
- Call only these runtime symbols (plus semantic helpers from external_dependencies):
{allowed_symbols_json}
- For every entry in semantic_dependencies, replace reads with call_form using pass-through
  parameter names, e.g. shock_active(ctx, time_period=time_period).
- Emit one complete helper_source function; no nested helpers or imports.
- helper_source must include a Google-style docstring with Args and Returns sections.
- helper_docstring must match the docstring embedded in helper_source exactly.
- Include a Note section listing covered workbook addresses and the Excel formula.
- Return only JSON matching the response schema.

Example docstring shape:
\"\"\"
Return 1.0 when the shock is active for the given projection period.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection period (1 for the first year).

Returns:
    1.0 if the projection year is at or after the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{{col}}5>=Inputs!$B$21,1,0).
\"\"\"

Example case-switch shape (use a lookup table, not an if/elif ladder):
year_address = {{1991: 'SomeSheet!C1', 1992: 'SomeSheet!D1', 1993: 'SomeSheet!E1', 1994: 'SomeSheet!F1', 1995: 'SomeSheet!G1'}}.get(time_period)
if year_address is None:
    return XlError.VALUE
year = xl_cell(ctx, year_address)

Cluster context:
{payload_json}

Response schema:
{schema_json}
""".strip()


class _FunctionReferenceRenameVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        old_name: str,
        new_name: str,
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.old_name = old_name
        self.new_name = new_name
        self.source = source
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "xl_eval"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Name)
            and node.args[2].id == self.old_name
        ):
            segment = ast.get_source_segment(self.source, node.args[2])
            if segment is not None:
                self.replacements.append(
                    (
                        node.args[2].lineno,
                        node.args[2].end_lineno or node.args[2].lineno,
                        segment,
                        self.new_name,
                    )
                )
        elif isinstance(node.func, ast.Name) and node.func.id == self.old_name:
            segment = ast.get_source_segment(self.source, node.func)
            if segment is not None:
                self.replacements.append(
                    (
                        node.func.lineno,
                        node.func.end_lineno or node.func.lineno,
                        segment,
                        self.new_name,
                    )
                )
        self.generic_visit(node)


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
