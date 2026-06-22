from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from src.formula_clustering import (
    ENGINE_COLUMNS,
    EngineColumn,
    FormulaCluster,
    engine_column_for_address,
)

repo_root = Path(__file__).resolve().parents[1]

REFACTOR_MODEL = "deepseek-v4-pro"
REFACTOR_PROMPT_VERSION = 1
REFACTOR_CACHE_PATH = repo_root / ".cache/internals-refactors.json"
FORMULA_SECTION_MARKER = "# --- Formula cell functions ---"

REFACTOR_ROW_ORDER: tuple[int, ...] = (10, 16, 6, 20, 14)

ROW_HELPER_NAMES: dict[int, str] = {
    6: "baseline_debt",
    10: "shock_active",
    14: "output_delta",
    16: "primary_balance_shocked",
    20: "debt_to_gdp",
}

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
class ClusterRefactorContext:
    cluster_id: int
    canonical_template: str
    row: int | None
    members: tuple[MemberContext, ...]
    external_dependencies: tuple[str, ...]
    call_sites: tuple[CallSite, ...]
    first_year_column: str
    allowed_runtime_symbols: tuple[str, ...]


class MemberBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(description="Workbook address this binding covers.")
    function_name: str = Field(description="Existing cell_* function being replaced.")
    engine_column: EngineColumn


class ClusterRefactorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    helper_name: str = Field(
        description="New snake_case function in internals.py, e.g. shock_active."
    )
    helper_docstring: str = Field(
        description="One-line docstring listing covered addresses, e.g. 'Engine!C10:G10'."
    )
    uses_first_year_branch: bool = Field(
        description="True when helper branches on engine_column == first_year_column."
    )
    helper_source: str = Field(
        description=(
            "Complete Python function definition including def line and body. "
            "Signature must be (ctx, col). "
            "Use only runtime symbols from allowed_runtime_symbols."
        )
    )
    member_bindings: tuple[MemberBinding, ...] = Field(
        description="One entry per cluster member; must cover all members exactly."
    )


@dataclass(frozen=True)
class ClusterRefactorApplyResult:
    source: str
    helper_name: str
    wrappers_applied: tuple[str, ...]
    dry_run: bool


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


def wrapper_source(helper_name: str, engine_column: str) -> str:
    return f'return {helper_name}(ctx, "{engine_column}")'


def build_cluster_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
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

    external_dependencies = tuple(
        sorted(
            {
                address_to_function_name(dependency)
                for member in members
                for dependency in member.dependency_addresses
                if dependency not in member_addresses
                and address_to_function_name(dependency) in defined_functions
            }
        )
    )

    return ClusterRefactorContext(
        cluster_id=cluster.cluster_id,
        canonical_template=cluster.canonical_template,
        row=cluster.row,
        members=tuple(members),
        external_dependencies=external_dependencies,
        call_sites=scan_call_sites(source, member_addresses, member_functions),
        first_year_column=ENGINE_COLUMNS[0],
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
    return {
        "cluster_id": ctx.cluster_id,
        "row": ctx.row,
        "canonical_template": ctx.canonical_template,
        "first_year_column": ctx.first_year_column,
        "members": [
            {
                "address": member.address,
                "function_name": member.function_name,
                "engine_column": member.engine_column,
                "normalized_formula": member.normalized_formula,
                "python_source": member.python_source,
                "dependency_addresses": member.dependency_addresses,
                "dependency_functions": member.dependency_functions,
            }
            for member in ctx.members
        ],
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
            "helper_name": deterministic_helper_name(ctx),
            "signature": "(ctx, col)",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
        },
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

    binding_addresses = {binding.address for binding in response.member_bindings}
    member_addresses = {member.address for member in ctx.members}
    if binding_addresses != member_addresses:
        raise ValueError(
            "member_bindings must cover cluster members exactly: "
            f"expected {sorted(member_addresses)}, got {sorted(binding_addresses)}"
        )

    for binding in response.member_bindings:
        member = next(item for item in ctx.members if item.address == binding.address)
        if binding.function_name != member.function_name:
            raise ValueError(
                f"binding for {binding.address} has function_name "
                f"{binding.function_name!r}, expected {member.function_name!r}"
            )
        if binding.engine_column != member.engine_column:
            raise ValueError(
                f"binding for {binding.address} has engine_column "
                f"{binding.engine_column!r}, expected {member.engine_column!r}"
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

    arg_names = [arg.arg for arg in helper_def.args.args]
    if arg_names != ["ctx", "col"]:
        raise ValueError(
            f"helper must accept exactly (ctx, col); got ({', '.join(arg_names)})"
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
        | {"ctx", "col"}
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


def apply_refactor_plan(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> str:
    updated = insert_helper_source(source, response.helper_source)
    wrappers_applied: list[str] = []
    for binding in response.member_bindings:
        updated = replace_member_with_wrapper(
            updated,
            function_name=binding.function_name,
            helper_name=response.helper_name,
            engine_column=binding.engine_column,
        )
        wrappers_applied.append(binding.function_name)
    return updated


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
    bindings: tuple[MemberBinding, ...],
    helper_name: str,
) -> str:
    updated = source
    for binding in bindings:
        pattern = (
            rf"xl_eval\(ctx,\s*'{re.escape(binding.address)}',\s*"
            rf"{re.escape(binding.function_name)}\)"
        )
        replacement = f'{helper_name}(ctx, "{binding.engine_column}")'
        updated = re.sub(pattern, replacement, updated)
    return updated


def validate_refactored_internals(source: str) -> None:
    ast.parse(source)
    compile(source, "internals.py", "exec")


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
    updated = apply_refactor_plan(source, response, ctx)
    validate_refactored_internals(updated)
    if not dry_run:
        internals_path.write_text(updated, encoding="utf-8")
    return ClusterRefactorApplyResult(
        source=updated,
        helper_name=response.helper_name,
        wrappers_applied=tuple(
            binding.function_name for binding in response.member_bindings
        ),
        dry_run=dry_run,
    )


def refactor_internals_all_clusters(
    projection: ProjectionResult,
    clusters: tuple[FormulaCluster, ...],
    *,
    internals_path: Path,
    dry_run: bool = False,
) -> tuple[ClusterRefactorApplyResult, ...]:
    """Refactor every multi-member cluster in dependency order, re-extracting context after each apply."""
    clusters_by_row = {
        cluster.row: cluster
        for cluster in clusters
        if cluster.row is not None and len(cluster.members) >= 2
    }
    results: list[ClusterRefactorApplyResult] = []
    for row in REFACTOR_ROW_ORDER:
        cluster = clusters_by_row.get(row)
        if cluster is None:
            continue
        ctx = build_cluster_refactor_context(projection, cluster, internals_path)
        if ctx is None:
            continue
        results.append(
            refactor_internals_cluster(
                ctx,
                internals_path=internals_path,
                dry_run=dry_run,
            )
        )
    return tuple(results)


load_dotenv(repo_root / ".env")


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
                        "Preserve semantics exactly; do not algebraically simplify."
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
        expected_name = deterministic_helper_name(ctx)
        if parsed.helper_name != expected_name:
            parsed = parsed.model_copy(update={"helper_name": expected_name})
        validate_cluster_refactor_response(
            ctx,
            parsed,
            existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
        )
        cache[cache_key] = content
        save_refactor_cache(cache)

    parsed = ClusterRefactorResponse.model_validate_json(content)
    expected_name = deterministic_helper_name(ctx)
    if parsed.helper_name != expected_name:
        parsed = parsed.model_copy(update={"helper_name": expected_name})
    validate_cluster_refactor_response(
        ctx,
        parsed,
        existing_names=_function_names(internals_path.read_text(encoding="utf-8")),
    )
    return parsed


def _applied_helper_names(existing_names: frozenset[str]) -> frozenset[str]:
    return frozenset(ROW_HELPER_NAMES.values()) & existing_names


def _prompt_for_refactor(
    payload: dict[str, object], response_schema: dict[str, object]
) -> str:
    payload_json = json.dumps(payload, indent=2, default=str)
    schema_json = json.dumps(response_schema, indent=2)
    return f"""
Refactor one parallel formula cluster into a single parameterized helper.

Rules:
- Parameterize by col (engine column C..G).
- Use col == first_year_column branch for first-year {{PRIOR_DEBT}} logic when needed.
- Keep xl_eval for dependencies outside this cluster.
- Do not rename dependency functions.
- Emit one complete helper_source function with signature (ctx, col).
- Set helper_name to constraints.helper_name exactly.
- Return only JSON matching the response schema.

Cluster context:
{payload_json}

Response schema:
{schema_json}
""".strip()


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
