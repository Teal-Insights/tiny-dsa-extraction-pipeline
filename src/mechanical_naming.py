"""LLM naming contract for mechanically synthesized cluster bodies (issue #45).

When :mod:`src.mechanical_body` produces a verified draft, the LLM's remaining
job is the semantic layer only: the helper docstring and domain-meaningful
names for the mechanical local temporaries. This module defines that response
contract and applies it mechanically — the LLM never emits Python for these
clusters, so a wrong answer can at worst produce a bad name or docstring,
never a semantic change.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.mechanical_body import MechanicalBodyDraft
from src.semantic_naming import validate_semantic_identifier

_REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTER_NAMING_PROMPT_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "cluster_naming_prompt.md"
)
SINGLETON_NAMING_PROMPT_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "singleton_naming_prompt.md"
)


def load_cluster_naming_prompt_fixed_portion() -> str:
    return CLUSTER_NAMING_PROMPT_FIXTURE.read_text(encoding="utf-8")


def load_singleton_naming_prompt_fixed_portion() -> str:
    return SINGLETON_NAMING_PROMPT_FIXTURE.read_text(encoding="utf-8")


class LocalRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original: str = Field(
        description="Mechanical local name from the draft body, e.g. _t1 or _f2_t3."
    )
    replacement: str = Field(
        description="Domain-meaningful snake_case name, e.g. interest_rate."
    )


class ClusterNamingLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_docstring: str | None = Field(
        description=(
            "Google-style docstring. Include Args and Returns sections. "
            "Null when error is true."
        ),
    )
    renames: tuple[LocalRename, ...] | None = Field(
        description=(
            "Rename for every mechanical local in the draft body (all names "
            "starting with '_'); lookup-table renames are optional. Null when "
            "error is true."
        ),
    )
    error: bool | None = Field(
        description=(
            "Set to true to abort this refactor and stop the pipeline when the "
            "draft cannot be meaningfully documented. Null or false on success."
        ),
    )
    error_reason: str | None = Field(
        description=(
            "Human-readable explanation of why refactoring must abort. "
            "Non-empty when error is true; null otherwise."
        ),
    )

    @model_validator(mode="after")
    def _require_success_fields_or_declared_error(self) -> ClusterNamingLLMResponse:
        from src.internals_refactor import _validate_llm_response_error_or_success

        return _validate_llm_response_error_or_success(
            self,
            success_fields=("symbol_docstring", "renames"),
        )


class SingletonNamingLLMResponse(ClusterNamingLLMResponse):
    """Naming-only response for a mechanically assembled singleton body.

    Same contract as clusters — docstring plus a rename map — but the schema
    title participates in the cache key, so singletons get their own model.
    """


def format_cluster_naming_prompt_context(
    context_dump: str,
    draft: MechanicalBodyDraft,
) -> str:
    """Append the verified draft body and its renameable locals to a context dump."""
    renameable = ", ".join(
        sorted(set(draft.renameable_locals) | set(draft.lookup_table_names))
    )
    return (
        f"{context_dump.strip()}\n\n"
        "Mechanical draft body (verified against every member; "
        "rename locals and write the docstring only):\n\n"
        f"```python\n{draft.body}\n```\n\n"
        f"Renameable locals: {renameable}"
    )


def format_singleton_naming_prompt_context(
    context_dump: str,
    draft: MechanicalBodyDraft,
) -> str:
    """Append the assembled singleton draft body to a context dump."""
    renameable = ", ".join(sorted(draft.renameable_locals))
    return (
        f"{context_dump.strip()}\n\n"
        "Mechanical draft body (assembled from the verified translation; "
        "rename locals and write the docstring only):\n\n"
        f"```python\n{draft.body}\n```\n\n"
        f"Renameable locals: {renameable}"
    )


def apply_cluster_naming_response(
    response: ClusterNamingLLMResponse,
    draft: MechanicalBodyDraft,
    *,
    parameter_names: frozenset[str],
    forbidden_names: frozenset[str],
) -> str:
    """Apply validated renames to the draft body and return the final body.

    Raises ``ValueError`` on any invalid rename so the LLM call is retried with
    the error as a correction turn.
    """
    if response.renames is None:
        raise ValueError("renames must be provided on success")
    renameable = set(draft.renameable_locals) | set(draft.lookup_table_names)
    required = {name for name in draft.renameable_locals if name.startswith("_")}

    renames: dict[str, str] = {}
    for entry in response.renames:
        if entry.original not in renameable:
            raise ValueError(
                f"rename original {entry.original!r} is not a renameable local; "
                f"renameable: {sorted(renameable)}"
            )
        if entry.original in renames:
            raise ValueError(f"duplicate rename for {entry.original!r}")
        renames[entry.original] = entry.replacement

    missing = sorted(required - set(renames))
    if missing:
        raise ValueError(
            "every mechanical local must be renamed; missing: " + ", ".join(missing)
        )

    unrenamed = renameable - set(renames)
    taken = forbidden_names | parameter_names | unrenamed | {"ctx"}
    seen: set[str] = set()
    for original, replacement in renames.items():
        validate_semantic_identifier(replacement, existing_names=frozenset(taken))
        if replacement.startswith("_"):
            raise ValueError(
                f"replacement for {original!r} must not start with '_': {replacement!r}"
            )
        if replacement in seen:
            raise ValueError(f"duplicate replacement name {replacement!r}")
        seen.add(replacement)

    params = ", ".join(sorted(parameter_names))
    prefix = f"def _draft(ctx, {params}):\n" if params else "def _draft(ctx):\n"
    indented = "\n".join(f"    {line}" for line in draft.body.splitlines())
    module = ast.parse(prefix + indented + "\n")
    function = module.body[0]
    assert isinstance(function, ast.FunctionDef)

    renamer = _NameRenamer(renames)
    statements = [renamer.visit(statement) for statement in function.body]
    for statement in statements:
        ast.fix_missing_locations(statement)
    return "\n".join(ast.unparse(statement) for statement in statements)


class _NameRenamer(ast.NodeTransformer):
    def __init__(self, renames: dict[str, str]) -> None:
        self._renames = renames

    def visit_Name(self, node: ast.Name) -> ast.Name:
        replacement = self._renames.get(node.id)
        if replacement is not None:
            return ast.Name(id=replacement, ctx=node.ctx)
        return node


@dataclass(frozen=True)
class NamingUnit:
    """One mechanically synthesized helper awaiting a naming response apply."""

    helper_name: str
    draft: MechanicalBodyDraft
    response: ClusterNamingLLMResponse
    parameter_names: frozenset[str]
    forbidden_names: frozenset[str]


def apply_naming_responses_to_module(
    source: str,
    units: Sequence[NamingUnit],
) -> str:
    """Apply a batch of naming responses to their helpers in a module.

    Each unit names one mechanically synthesized helper: its renames are applied
    to the draft body via :func:`apply_cluster_naming_response`, and the helper's
    body and docstring are replaced in ``source`` while keeping its signature
    (including type hints). Application is commutative — helpers are rewritten in
    source order regardless of the order of ``units`` — so two callers passing
    the same units in different orders produce byte-identical output.
    """
    unit_by_name: dict[str, NamingUnit] = {}
    for unit in units:
        if unit.helper_name in unit_by_name:
            raise ValueError(f"duplicate naming unit for helper {unit.helper_name!r}")
        unit_by_name[unit.helper_name] = unit

    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in unit_by_name:
            continue
        unit = unit_by_name[node.name]
        if unit.response.symbol_docstring is None:
            raise ValueError(
                f"naming response for {node.name!r} must include a docstring"
            )
        named_body = apply_cluster_naming_response(
            unit.response,
            unit.draft,
            parameter_names=unit.parameter_names,
            forbidden_names=unit.forbidden_names,
        )
        rebuilt = _rebuild_named_function(
            node,
            docstring=unit.response.symbol_docstring,
            body=named_body,
        )
        # ``FunctionDef.lineno`` is the ``def`` line; include decorators so a
        # rewrite does not leave an orphaned ``@xl_memoize`` above the unparsed
        # replacement (which re-emits ``decorator_list``).
        start_lineno = (
            min(decorator.lineno for decorator in node.decorator_list)
            if node.decorator_list
            else node.lineno
        )
        start = start_lineno - 1
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        replacements.append((start, end, rebuilt))

    for start, end, rebuilt in sorted(
        replacements, key=lambda item: item[0], reverse=True
    ):
        block = rebuilt if rebuilt.endswith("\n") else rebuilt + "\n"
        lines[start:end] = [block]
    return "".join(lines)


def _rebuild_named_function(
    node: ast.FunctionDef,
    *,
    docstring: str,
    body: str,
) -> str:
    """Return the unparsed helper with a new docstring and renamed body."""
    rebuilt = copy.deepcopy(node)
    docstring_stmt = ast.Expr(value=ast.Constant(value=docstring))
    body_statements = ast.parse(body).body
    rebuilt.body = [docstring_stmt, *body_statements]
    ast.fix_missing_locations(rebuilt)
    return ast.unparse(rebuilt)
