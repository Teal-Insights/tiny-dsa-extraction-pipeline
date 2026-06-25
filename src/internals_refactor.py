from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import re
import textwrap
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from src.formula_clustering import (
    ENGINE_COLUMNS,
    EngineColumn,
    FormulaCluster,
    engine_column_for_address,
    parse_workbook_address,
)
from src.phase_b_plugin import PhaseBPlugin, apply_phase_b_plugins
from src.refactor_bindings import (
    BindingKeyValue,
    KeyConceptSpec,
    TIME_PERIOD_TO_ENGINE_COLUMN,
    build_bound_address_keys,
    engine_column_from_member_keys,
    expected_member_keys_for_cluster,
    format_binding_key_literal,
    load_key_concept_vocabulary,
    render_literal_helper_call,
)
from src.refactor_order import compute_multi_member_cluster_refactor_order

repo_root = Path(__file__).resolve().parents[1]
BINDINGS_PATH = repo_root / "bindings"
DEFAULT_WORKBOOK_PATH = repo_root / "data/tiny-dsa.xlsx"

REFACTOR_MODEL = "deepseek-v4-pro"
REFACTOR_PROMPT_VERSION = 6
REFACTOR_CACHE_PATH = repo_root / ".cache/internals-refactors.json"
FORMULA_SECTION_MARKER = "# --- Formula cell functions ---"
PROJECTION_ALIASES_MARKER = "# --- Projection public address aliases ---"
RESOLVER_SECTION_MARKER = "# --- Formula resolver ---"

AddressDispatch = dict[str, tuple[str, dict[str, BindingKeyValue]]]

REFACTOR_ROW_ORDER: tuple[int, ...] = (10, 16, 6, 20, 14)
"""Legacy Tiny DSA apply order; prefer ``compute_multi_member_cluster_refactor_order``."""

ROW_HELPER_NAMES: dict[int, str] = {
    6: "baseline_debt",
    10: "shock_active",
    14: "output_delta",
    16: "primary_balance_shocked",
    20: "debt_to_gdp",
}

DOMAIN_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("initial_debt_to_gdp", "Inputs!B6 — country initial debt-to-GDP ratio"),
    ("growth_baseline", "Inputs!C16:G16 — real GDP growth rates"),
    ("interest_baseline", "Inputs!C17:G17 — real interest rates"),
    ("primary_balance_baseline", "Inputs!C18:G18 — primary balance"),
    ("shock_year", "Inputs!B21 — first projection year the shock applies"),
    ("shock_type", "Inputs!B22 — shocked parameter selector (1–3)"),
    ("shock_magnitude_resolved", "Engine!B9 — resolved shock magnitude"),
    (
        "shock_active",
        "Engine!C10:G10 — 1.0 when the shock is active for the column, else 0.0",
    ),
    ("primary_balance_shocked", "Engine!C16:G16 — primary balance including the shock"),
    ("baseline_debt", "Engine!C6:G6 — baseline debt-to-GDP path"),
    ("baseline_path", "Engine!C6:G6 — baseline debt-to-GDP path"),
    ("debt_to_gdp", "Engine!C20:G20 — shocked debt-to-GDP path"),
    ("shocked_path", "Engine!C20:G20 — shocked debt-to-GDP path"),
    ("output_delta", "Outputs!B14:F14 — shocked minus baseline debt-to-GDP"),
)

SINGLETON_SYMBOL_NAMES: dict[str, str] = {
    "Inputs!B6": "initial_debt_to_gdp",
    "Engine!B9": "shock_magnitude_resolved",
}

SINGLETON_REFACTOR_ORDER: tuple[str, ...] = ("Inputs!B6", "Engine!B9")

SEMANTIC_HELPER_NAMES: frozenset[str] = frozenset(
    ROW_HELPER_NAMES.values()
) | frozenset(SINGLETON_SYMBOL_NAMES.values())

ALLOWED_RUNTIME_SYMBOLS: tuple[str, ...] = (
    "XlError",
    "np",
    "to_bool",
    "to_int",
    "xl_add",
    "xl_cell",
    "xl_div",
    "xl_eval",
    "xl_ge",
    "xl_index_ref",
    "xl_match",
    "xl_mul",
    "xl_offset",
    "xl_sub",
)


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
    engine_column: EngineColumn
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    dependency_functions: tuple[str, ...]


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


class HelperParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Python parameter name for the helper, e.g. time_period."
    )
    concept: str = Field(
        description="Binding key concept this parameter varies along, e.g. TIME_PERIOD."
    )
    dtype: str = Field(description="Expected Python dtype for the parameter.")


class MemberKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(description="Workbook address this entry covers.")
    function_name: str = Field(description="Existing cell_* function being replaced.")
    keys: dict[str, BindingKeyValue] = Field(
        description=(
            "Literal binding key values for this address; keys must match "
            "helper parameters and exclude series-constant concepts."
        )
    )


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
    symbol_name: str
    canonical_template: str
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    call_sites: tuple[CallSite, ...]
    allowed_runtime_symbols: tuple[str, ...]


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
class HelperRewriteBinding:
    address: str
    function_name: str
    engine_column: EngineColumn
    helper_name: str


@dataclass(frozen=True)
class ClusterRefactorApplyResult:
    source: str
    helper_name: str
    wrappers_applied: tuple[str, ...]
    dry_run: bool
    response: ClusterRefactorResponse
    phase_b_rewrites: int = 0
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


def deterministic_helper_name(ctx: ClusterRefactorContext) -> str:
    if ctx.row is not None and ctx.row in ROW_HELPER_NAMES:
        return ROW_HELPER_NAMES[ctx.row]
    if ctx.row is not None:
        return f"engine_row_{ctx.row}"
    return f"cluster_{ctx.cluster_id}"


def deterministic_singleton_name(address: str) -> str | None:
    return SINGLETON_SYMBOL_NAMES.get(address)


def wrapper_source(helper_name: str, engine_column: str) -> str:
    return f'return {helper_name}(ctx, "{engine_column}")'


def _default_bound_address_keys() -> dict[str, dict[str, BindingKeyValue]]:
    from src.extraction_pipeline import input_series, output_series

    return build_bound_address_keys(input_series, output_series)


def _default_key_vocabulary() -> tuple[KeyConceptSpec, ...]:
    return load_key_concept_vocabulary(BINDINGS_PATH)


def build_cluster_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] | None = None,
    key_vocabulary: tuple[KeyConceptSpec, ...] | None = None,
    workbook_path: Path = DEFAULT_WORKBOOK_PATH,
) -> ClusterRefactorContext | None:
    if len(cluster.members) < 2:
        return None

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

        engine_column = engine_column_for_address(address)
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

        members.append(
            MemberContext(
                address=address,
                function_name=function_name,
                engine_column=engine_column,
                normalized_formula=node.normalized_formula,
                python_source=extract_function_source(source, function_name),
                dependency_addresses=dependency_addresses,
                dependency_functions=dependency_functions,
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
        key_vocabulary if key_vocabulary is not None else _default_key_vocabulary()
    )
    member_address_list = tuple(member.address for member in members)
    expected_member_keys = expected_member_keys_for_cluster(
        member_address_list,
        bound_address_keys=resolved_bound_keys,
        workbook_path=workbook_path,
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
        first_year_column=ENGINE_COLUMNS[0],
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        key_vocabulary=resolved_vocabulary,
        expected_member_keys=expected_member_keys,
    )


def build_singleton_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
) -> SingletonRefactorContext | None:
    if len(cluster.members) != 1:
        return None

    address = cluster.members[0]
    symbol_name = deterministic_singleton_name(address)
    if symbol_name is None:
        return None

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
        symbol_name=symbol_name,
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
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
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


def refactor_cache_key(
    ctx: ClusterRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "model": REFACTOR_MODEL,
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
        "internals_sha256": hashlib.sha256(internals_bytes).hexdigest(),
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
            "helper_name": deterministic_helper_name(ctx),
            "parameters": parameter_names,
            "signature": f"(ctx, {', '.join(parameter_names)})",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "domain_glossary": [
            {"symbol": symbol, "description": description}
            for symbol, description in DOMAIN_GLOSSARY
        ],
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
        normalized_keys: dict[str, BindingKeyValue] = {}
        for key, value in entry.keys.items():
            concept = concept_by_name.get(key, key)
            normalized_keys[concept] = value
        normalized_entries.append(entry.model_copy(update={"keys": normalized_keys}))
    return response.model_copy(update={"member_keys": tuple(normalized_entries)})


def _prepare_cluster_refactor_response(
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> ClusterRefactorResponse:
    expected_name = deterministic_helper_name(ctx)
    if response.helper_name != expected_name:
        response = response.model_copy(update={"helper_name": expected_name})
    response = _normalize_member_key_concepts(response)
    response = _align_cluster_response_docstring(response)
    return _normalize_cluster_response_docstring(response)


def _prepare_singleton_refactor_response(
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> SingletonRefactorResponse:
    if response.symbol_name != ctx.symbol_name:
        response = response.model_copy(update={"symbol_name": ctx.symbol_name})
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


def validate_cluster_refactor_response(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
    *,
    existing_names: frozenset[str],
) -> None:
    expected_helper_name = deterministic_helper_name(ctx)
    if response.helper_name != expected_helper_name:
        raise ValueError(
            f"helper_name must be {expected_helper_name!r}, got {response.helper_name!r}"
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
        extra_concepts = set(entry.keys) - parameter_concepts
        if extra_concepts:
            raise ValueError(
                f"member_keys for {entry.address} must not include "
                f"series-constant concepts: {sorted(extra_concepts)}"
            )
        missing_concepts = parameter_concepts - set(entry.keys)
        if missing_concepts:
            raise ValueError(
                f"member_keys for {entry.address} missing parameter concepts: "
                f"{sorted(missing_concepts)}"
            )
        expected_keys = ctx.expected_member_keys[entry.address]
        for concept, expected_value in expected_keys.items():
            actual_value = entry.keys.get(concept)
            if actual_value != expected_value:
                raise ValueError(
                    f"member_keys for {entry.address} has {concept}={actual_value!r}, "
                    f"expected {expected_value!r}"
                )
        engine_column = engine_column_from_member_keys(
            entry.keys, address=entry.address
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
    if (
        response.helper_name in existing_names
        and response.helper_name != expected_helper_name
    ):
        raise ValueError(
            f"helper_name {response.helper_name!r} collides with existing function"
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
        | _applied_helper_names(existing_names)
        | _applied_singleton_names(existing_names)
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


def singleton_refactor_cache_key(
    ctx: SingletonRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "kind": "singleton",
        "model": REFACTOR_MODEL,
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "address": ctx.address,
        "canonical_template": ctx.canonical_template,
        "formula_sha256": hashlib.sha256(ctx.normalized_formula.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(ctx.python_source.encode()).hexdigest(),
        "internals_sha256": hashlib.sha256(internals_bytes).hexdigest(),
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
            "symbol_name": ctx.symbol_name,
            "signature": "(ctx)",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "domain_glossary": [
            {"symbol": symbol, "description": description}
            for symbol, description in DOMAIN_GLOSSARY
        ],
    }


def validate_singleton_refactor_response(
    ctx: SingletonRefactorContext,
    response: SingletonRefactorResponse,
    *,
    existing_names: frozenset[str],
) -> None:
    if response.symbol_name != ctx.symbol_name:
        raise ValueError(
            f"symbol_name must be {ctx.symbol_name!r}, got {response.symbol_name!r}"
        )

    if not response.symbol_name.isidentifier():
        raise ValueError(
            f"symbol_name is not a valid identifier: {response.symbol_name!r}"
        )
    if (
        response.symbol_name in existing_names
        and response.symbol_name != ctx.symbol_name
    ):
        raise ValueError(
            f"symbol_name {response.symbol_name!r} collides with existing function"
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
        | _applied_helper_names(existing_names)
        | _applied_singleton_names(existing_names)
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
    ctx: ClusterRefactorContext,
    *,
    phase_b: bool = False,
) -> str:
    updated, _rewrite_count = apply_cluster_collapse(source, response, ctx)
    if phase_b:
        updated, _extra = apply_phase_b_for_response(updated, response, plugins=())
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
                entry.keys,
            ),
        )
        for entry in response.member_keys
    )


def apply_cluster_collapse(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> tuple[str, int]:
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
            _parameter_literals(response.parameters, entry.keys),
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


def helper_rewrite_bindings(
    response: ClusterRefactorResponse,
) -> tuple[HelperRewriteBinding, ...]:
    return helper_rewrite_bindings_for_member_keys(
        response.member_keys,
        helper_name=response.helper_name,
    )


def helper_rewrite_bindings_for_member_keys(
    member_keys: tuple[MemberKeys, ...],
    *,
    helper_name: str,
) -> tuple[HelperRewriteBinding, ...]:
    return tuple(
        HelperRewriteBinding(
            address=entry.address,
            function_name=entry.function_name,
            engine_column=_engine_column_for_member_keys(entry),
            helper_name=helper_name,
        )
        for entry in member_keys
    )


def _engine_column_for_member_keys(entry: MemberKeys) -> EngineColumn:
    engine_column = engine_column_from_member_keys(entry.keys, address=entry.address)
    if engine_column is None:
        raise ValueError(
            f"member_keys for {entry.address} do not resolve to an engine column"
        )
    typed_column = engine_column_for_address(entry.address)
    if typed_column is not None and engine_column != typed_column:
        raise ValueError(
            f"member_keys for {entry.address} resolve to engine column "
            f"{engine_column!r}, expected {typed_column!r}"
        )
    if typed_column is not None:
        return typed_column
    if engine_column not in ENGINE_COLUMNS:
        raise ValueError(
            f"member_keys for {entry.address} resolve to unknown engine column "
            f"{engine_column!r}"
        )
    return cast(EngineColumn, engine_column)


def apply_phase_b_for_response(
    source: str,
    response: ClusterRefactorResponse,
    *,
    plugins: tuple[PhaseBPlugin, ...] = (),
) -> tuple[str, int]:
    return apply_phase_b(source, helper_rewrite_bindings(response), plugins=plugins)


def apply_phase_b(
    source: str,
    bindings: tuple[HelperRewriteBinding, ...],
    *,
    plugins: tuple[PhaseBPlugin, ...] = (),
) -> tuple[str, int]:
    """Replace xl_eval/direct member call sites with parameterized helper calls."""
    wrapper_functions = frozenset(binding.function_name for binding in bindings)
    updated = apply_phase_b_plugins(source, plugins)
    rewrite_count = 0

    updated, count = _rewrite_xl_eval_bindings(updated, bindings, wrapper_functions)
    rewrite_count += count
    updated, count = _rewrite_direct_bindings(updated, bindings, wrapper_functions)
    rewrite_count += count
    updated, count = _rewrite_projection_aliases(updated, bindings)
    rewrite_count += count
    updated = apply_phase_b_plugins(updated, plugins)
    return updated, rewrite_count


def apply_phase_b_final_pass(
    source: str,
    responses: tuple[ClusterRefactorResponse, ...],
    *,
    plugins: tuple[PhaseBPlugin, ...] = (),
) -> tuple[str, int]:
    bindings: list[HelperRewriteBinding] = []
    for response in responses:
        bindings.extend(helper_rewrite_bindings(response))
    return apply_phase_b(source, tuple(bindings), plugins=plugins)


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
    column = _engine_column_for_time_period(int(key_kwargs["time_period"]))
    if column is None:
        return None
    return helper_name, column


def parse_thin_literal_wrapper(
    function_def: ast.FunctionDef,
) -> tuple[str, dict[str, BindingKeyValue]] | None:
    """Return ``(helper_name, key_kwargs)`` for a one-line semantic helper wrapper."""
    return _parse_thin_helper_return(function_def)


def _engine_column_for_time_period(time_period: int) -> str | None:
    return TIME_PERIOD_TO_ENGINE_COLUMN.get(time_period)


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
        time_period = _time_period_for_engine_column(call.args[1].value)
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
    for address in dependency_addresses:
        function_name = address_to_function_name(address)
        node = defined_functions.get(function_name)
        if node is not None:
            wrapper = parse_thin_wrapper(node)
            if wrapper is None or wrapper[0] not in SEMANTIC_HELPER_NAMES:
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


def _infer_collapsed_semantic_dependency(
    source: str,
    address: str,
) -> tuple[str, str] | None:
    _sheet, _column, row = parse_workbook_address(address)
    helper_name = ROW_HELPER_NAMES.get(row)
    if helper_name is None or helper_name not in _function_names(source):
        return None
    helper_def = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == helper_name
    )
    parameter_names = [arg.arg for arg in helper_def.args.args if arg.arg != "ctx"]
    if len(parameter_names) != 1:
        return None
    return helper_name, parameter_names[0]


def function_name_to_workbook_address(function_name: str) -> str | None:
    return _caller_address(function_name)


def address_needs_resolver_dispatch(address: str) -> bool:
    """Engine cells are reached via helpers; only external entry addresses need dispatch."""
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
    updated = _remove_projection_aliases_section(updated)
    existing_dispatch = _parse_address_dispatch(updated) or {}
    existing_dispatch.update(dispatch)
    updated = _replace_resolver_section(
        updated,
        existing_dispatch,
        symbol_dispatch=_parse_symbol_dispatch(source),
    )
    updated, trimmed = _trim_engine_dispatch_entries(updated)
    return updated, len(to_prune) + trimmed


def _time_period_for_engine_column(column: str) -> int | None:
    for time_period, engine_column in TIME_PERIOD_TO_ENGINE_COLUMN.items():
        if engine_column == column:
            return time_period
    return None


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


def _parse_legacy_address_dispatch(
    source: str,
) -> dict[str, tuple[str, str]] | None:
    """Parse column-letter dispatch tuples left from older refactors."""
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_ADDRESS_DISPATCH":
                if not isinstance(node.value, ast.Dict):
                    return None
                dispatch: dict[str, tuple[str, str]] = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value, ast.Tuple)
                        and len(value.elts) == 2
                        and isinstance(value.elts[0], ast.Constant)
                        and isinstance(value.elts[0].value, str)
                        and isinstance(value.elts[1], ast.Constant)
                        and isinstance(value.elts[1].value, str)
                    ):
                        return None
                    dispatch[key.value] = (
                        value.elts[0].value,
                        value.elts[1].value,
                    )
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


def _remove_projection_aliases_section(source: str) -> str:
    if PROJECTION_ALIASES_MARKER not in source:
        return source
    pattern = re.compile(
        rf"\n{re.escape(PROJECTION_ALIASES_MARKER)}\n.*?(?=\n{re.escape(RESOLVER_SECTION_MARKER)})",
        re.DOTALL,
    )
    return pattern.sub("\n", source)


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


def replace_member_with_wrapper(
    source: str,
    *,
    function_name: str,
    helper_name: str,
    engine_column: str,
) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            body_start = _wrapper_body_start_line(node)
            end_line = node.end_lineno
            wrapper_line = f"    {wrapper_source(helper_name, engine_column)}\n"
            return (
                "".join(lines[: body_start - 1])
                + wrapper_line
                + "".join(lines[end_line:])
            )
    raise KeyError(f"Function {function_name!r} not found")


def rewrite_xl_eval_call_sites(
    source: str,
    member_keys: tuple[MemberKeys, ...],
    helper_name: str,
) -> str:
    helper_bindings = helper_rewrite_bindings_for_member_keys(
        member_keys,
        helper_name=helper_name,
    )
    updated, _count = _rewrite_xl_eval_bindings(
        source,
        helper_bindings,
        wrapper_functions=frozenset(),
    )
    return updated


def _helper_call_expr(binding: HelperRewriteBinding, *, use_col_parameter: bool) -> str:
    column = "col" if use_col_parameter else f'"{binding.engine_column}"'
    return f"{binding.helper_name}(ctx, {column})"


def _function_has_col_parameter(function_def: ast.FunctionDef) -> bool:
    return any(arg.arg == "col" for arg in function_def.args.args)


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


def _xl_eval_matches_binding(
    node: ast.Call,
    binding: HelperRewriteBinding,
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
        return True
    return False


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


class _XlEvalRewriteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        binding: HelperRewriteBinding,
        caller_has_col: bool,
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.binding = binding
        self.caller_has_col = caller_has_col
        self.source = source
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        if _xl_eval_matches_binding(node, self.binding):
            segment = ast.get_source_segment(self.source, node)
            if segment is not None:
                self.replacements.append(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        segment,
                        _helper_call_expr(
                            self.binding,
                            use_col_parameter=self.caller_has_col,
                        ),
                    )
                )
        self.generic_visit(node)


class _DirectCallRewriteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        binding: HelperRewriteBinding,
        caller_has_col: bool,
        source: str,
        replacements: list[tuple[int, int, str, str]],
    ) -> None:
        self.binding = binding
        self.caller_has_col = caller_has_col
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
                        _helper_call_expr(
                            self.binding,
                            use_col_parameter=self.caller_has_col,
                        ),
                    )
                )
        self.generic_visit(node)


def _rewrite_xl_eval_bindings(
    source: str,
    bindings: tuple[HelperRewriteBinding, ...],
    wrapper_functions: frozenset[str],
) -> tuple[str, int]:
    module = ast.parse(source)
    segment_replacements: list[tuple[int, int, str, str]] = []

    for binding in bindings:
        for function_def in _iter_function_defs(module.body):
            if function_def.name in wrapper_functions:
                continue
            visitor = _XlEvalRewriteVisitor(
                binding=binding,
                caller_has_col=_function_has_col_parameter(function_def),
                source=source,
                replacements=segment_replacements,
            )
            visitor.visit(function_def)

    if not segment_replacements:
        return source, 0

    return _apply_segment_replacements(source, segment_replacements), len(
        segment_replacements
    )


def _rewrite_direct_bindings(
    source: str,
    bindings: tuple[HelperRewriteBinding, ...],
    wrapper_functions: frozenset[str],
) -> tuple[str, int]:
    module = ast.parse(source)
    segment_replacements: list[tuple[int, int, str, str]] = []

    for binding in bindings:
        for function_def in _iter_function_defs(module.body):
            if function_def.name in wrapper_functions:
                continue
            if function_def.name == binding.function_name:
                continue
            visitor = _DirectCallRewriteVisitor(
                binding=binding,
                caller_has_col=_function_has_col_parameter(function_def),
                source=source,
                replacements=segment_replacements,
            )
            visitor.visit(function_def)

    if not segment_replacements:
        return source, 0

    return _apply_segment_replacements(source, segment_replacements), len(
        segment_replacements
    )


def _rewrite_projection_aliases(
    source: str,
    bindings: tuple[HelperRewriteBinding, ...],
) -> tuple[str, int]:
    binding_by_function = {binding.function_name: binding for binding in bindings}
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []

    for function_def in _iter_function_defs(module.body):
        if len(function_def.body) != 1:
            continue
        statement = function_def.body[0]
        if not isinstance(statement, ast.Return) or statement.value is None:
            continue
        call = statement.value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "xl_eval"
            and len(call.args) >= 3
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
            and isinstance(call.args[2], ast.Name)
        ):
            continue
        binding = binding_by_function.get(call.args[2].id)
        if binding is None:
            continue
        indent = " " * (getattr(statement, "col_offset", 4) or 4)
        replacements.append(
            (
                statement.lineno,
                statement.end_lineno or statement.lineno,
                f'{indent}return {binding.helper_name}(ctx, "{binding.engine_column}")\n',
            )
        )

    if not replacements:
        return source, 0
    return _apply_line_replacements(lines, replacements), len(replacements)


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


def _apply_line_replacements(
    lines: list[str],
    replacements: list[tuple[int, int, str]],
) -> str:
    for start_line, end_line, new_text in sorted(
        replacements,
        key=lambda item: item[0],
        reverse=True,
    ):
        lines[start_line - 1 : end_line] = [new_text]
    return "".join(lines)


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
) -> SingletonRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_singleton(ctx, internals_path=internals_path)
    validate_singleton_refactor_response(ctx, response, existing_names=existing_names)
    updated, rewrite_count = apply_singleton_refactor_plan(source, response, ctx)
    validate_refactored_internals(updated)
    if not dry_run:
        internals_path.write_text(updated, encoding="utf-8")
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
) -> tuple[SingletonRefactorApplyResult, ...]:
    singleton_clusters = {
        cluster.members[0]: cluster for cluster in clusters if len(cluster.members) == 1
    }
    results: list[SingletonRefactorApplyResult] = []
    for address in SINGLETON_REFACTOR_ORDER:
        cluster = singleton_clusters.get(address)
        if cluster is None:
            continue
        ctx = build_singleton_refactor_context(projection, cluster, internals_path)
        if ctx is None:
            continue
        result = refactor_internals_singleton(
            ctx,
            internals_path=internals_path,
            dry_run=dry_run,
        )
        results.append(result)
    return tuple(results)


def refactor_internals_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    response: ClusterRefactorResponse | None = None,
    dry_run: bool = False,
) -> ClusterRefactorApplyResult:
    source = internals_path.read_text(encoding="utf-8")
    existing_names = _function_names(source)
    if response is None:
        response = llm_refactor_cluster(ctx, internals_path=internals_path)
    validate_cluster_refactor_response(ctx, response, existing_names=existing_names)
    updated = apply_refactor_plan(source, response, ctx, phase_b=False)
    validate_refactored_internals(updated)
    if not dry_run:
        internals_path.write_text(updated, encoding="utf-8")
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
    dry_run: bool = False,
    phase_b_plugins: tuple[PhaseBPlugin, ...] = (),
) -> tuple[ClusterRefactorApplyResult, ...]:
    """Refactor singletons, then every multi-member cluster in dependency order."""
    refactor_internals_all_singletons(
        projection,
        clusters,
        internals_path=internals_path,
        dry_run=dry_run,
    )
    ordered_clusters = compute_multi_member_cluster_refactor_order(projection, clusters)
    results: list[ClusterRefactorApplyResult] = []
    responses: list[ClusterRefactorResponse] = []
    for cluster in ordered_clusters:
        ctx = build_cluster_refactor_context(projection, cluster, internals_path)
        if ctx is None:
            continue
        result = refactor_internals_cluster(
            ctx,
            internals_path=internals_path,
            dry_run=dry_run,
        )
        results.append(result)
        responses.append(result.response)

    if not dry_run and responses:
        source = internals_path.read_text(encoding="utf-8")
        updated, phase_c_pruned = apply_phase_c(source)
        validate_refactored_internals(updated)
        internals_path.write_text(updated, encoding="utf-8")
        if results:
            last = results[-1]
            results[-1] = ClusterRefactorApplyResult(
                source=updated,
                helper_name=last.helper_name,
                wrappers_applied=last.wrappers_applied,
                dry_run=last.dry_run,
                response=last.response,
                phase_b_rewrites=0,
                phase_c_pruned=phase_c_pruned,
            )

    return tuple(results)


load_dotenv(repo_root / ".env")


def llm_refactor_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
) -> SingletonRefactorResponse:
    schema = SingletonRefactorResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    cache = load_refactor_cache()
    cache_key = singleton_refactor_cache_key(ctx, internals_bytes, schema)
    if cache_key in cache:
        content = cache[cache_key]
    else:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is required to generate uncached refactor responses"
            )
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        payload = singleton_prompt_payload(ctx)
        response = client.chat.completions.create(
            model=REFACTOR_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You rename and refactor one Excel-generated singleton helper "
                        "into a semantic function. Return only JSON matching the schema. "
                        "Preserve semantics exactly; do not algebraically simplify. "
                        "Write Google-style docstrings with Args and Returns sections."
                    ),
                },
                {
                    "role": "user",
                    "content": _prompt_for_singleton_refactor(payload, schema),
                },
            ],
            stream=False,
            reasoning_effort="high",
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "enabled"}},
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("DeepSeek returned empty singleton refactor content")
        parsed = SingletonRefactorResponse.model_validate_json(content)
        parsed = _prepare_singleton_refactor_response(parsed, ctx)
        validate_singleton_refactor_response(
            ctx,
            parsed,
            existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
        )
        cache[cache_key] = parsed.model_dump_json()
        save_refactor_cache(cache)

    parsed = SingletonRefactorResponse.model_validate_json(content)
    parsed = _prepare_singleton_refactor_response(parsed, ctx)
    validate_singleton_refactor_response(
        ctx,
        parsed,
        existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
    )
    return parsed


def llm_refactor_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
) -> ClusterRefactorResponse:
    schema = ClusterRefactorResponse.model_json_schema()
    internals_bytes = internals_path.read_bytes()
    cache = load_refactor_cache()
    cache_key = refactor_cache_key(ctx, internals_bytes, schema)
    if cache_key in cache:
        content = cache[cache_key]
    else:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is required to generate uncached refactor responses"
            )
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        payload = prompt_payload(ctx)
        response = client.chat.completions.create(
            model=REFACTOR_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You refactor parallel Excel-generated Python helpers into one "
                        "parameterized function. Return only JSON matching the schema. "
                        "Preserve semantics exactly; do not algebraically simplify. "
                        "Write Google-style docstrings with Args and Returns sections."
                    ),
                },
                {
                    "role": "user",
                    "content": _prompt_for_refactor(payload, schema),
                },
            ],
            stream=False,
            reasoning_effort="high",
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "enabled"}},
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("DeepSeek returned empty refactor content")
        parsed = ClusterRefactorResponse.model_validate_json(content)
        parsed = _prepare_cluster_refactor_response(parsed, ctx)
        validate_cluster_refactor_response(
            ctx,
            parsed,
            existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
        )
        cache[cache_key] = parsed.model_dump_json()
        save_refactor_cache(cache)

    parsed = ClusterRefactorResponse.model_validate_json(content)
    parsed = _prepare_cluster_refactor_response(parsed, ctx)
    validate_cluster_refactor_response(
        ctx,
        parsed,
        existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
    )
    return parsed


def _applied_helper_names(existing_names: frozenset[str]) -> frozenset[str]:
    return frozenset(ROW_HELPER_NAMES.values()) & existing_names


def _applied_singleton_names(existing_names: frozenset[str]) -> frozenset[str]:
    return frozenset(SINGLETON_SYMBOL_NAMES.values()) & existing_names


def _prompt_for_singleton_refactor(
    payload: dict[str, object], response_schema: dict[str, object]
) -> str:
    payload_json = json.dumps(payload, indent=2, default=str)
    schema_json = json.dumps(response_schema, indent=2)
    return f"""
Rename and refactor one Excel-generated singleton helper into a semantic function.

Rules:
- Keep signature (ctx) exactly; do not add a col parameter.
- Do not rename dependency functions.
- Rename local temporaries to domain-meaningful snake_case using domain_glossary.
- Do not use excel-shaped locals such as _t1, t2, b21, col10, choose1, func_map, or input17.
- Do not reference cell_* helpers; call semantic helpers (e.g. shock_active, initial_debt_to_gdp).
- Prefer readable if/else over walrus/ternary chains when refactoring for clarity.
- Emit one complete symbol_source function with signature (ctx).
- symbol_source must include a Google-style docstring with Args and Returns sections.
- symbol_docstring must match the docstring embedded in symbol_source exactly.
- Include a Note section listing the workbook address and Excel formula.
- Set symbol_name to constraints.symbol_name exactly.
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
    return f"""
Refactor one parallel formula cluster into a single parameterized helper.

Rules:
- Declare parameters[] using binding key concepts from key_vocabulary; do not use column letters.
- For each cluster member, emit member_keys[] with literal key values from expected_keys.
- Series-constant binding keys (scope: series) are not parameters; bake them into the helper.
- Emit helper_source with signature (ctx, <parameters>) using semantic parameter names.
- Use time_period == 1 branch for first-year {{PRIOR_DEBT}} logic when needed.
- Map time_period to workbook columns internally when reading xl_cell addresses.
- Keep xl_eval only for leaf inputs read with xl_cell; never for refactored cells.
- Do not rename dependency functions.
- Rename local temporaries to domain-meaningful snake_case using domain_glossary.
- Do not use excel-shaped locals such as _t1, t2, b21, col10, choose1, func_map, or input17.
- Do not reference cell_* helpers anywhere in the body.
- For every entry in semantic_dependencies, replace reads with call_form using pass-through
  parameter names, e.g. shock_active(ctx, time_period=time_period).
- Prefer readable if/else over walrus/ternary chains when refactoring for clarity.
- Emit one complete helper_source function.
- helper_source must include a Google-style docstring with Args and Returns sections.
- helper_docstring must match the docstring embedded in helper_source exactly.
- Include a Note section listing covered workbook addresses and the Excel formula.
- Set helper_name to constraints.helper_name exactly.
- Return only JSON matching the response schema.

Example docstring shape:
\"\"\"
Return 1.0 when the shock is active for the given projection column.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    1.0 if the projection year is at or after the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{{col}}5>=Inputs!$B$21,1,0).
\"\"\"

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


def _wrapper_body_start_line(function_def: ast.FunctionDef) -> int:
    if not function_def.body:
        return function_def.lineno + 1
    first = function_def.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return first.end_lineno + 1
    return first.lineno


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
