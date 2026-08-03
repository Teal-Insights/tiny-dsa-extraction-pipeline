"""Standalone Pass-2 semantic naming from a mechanical ``internals.py``.

Discovers helpers still carrying the mechanical placeholder docstring, rebuilds
a minimal naming draft from each helper body, and applies docstring + local
renames without graph, clustering, or parity-gate context.

v1 prompts are intentionally thinner than in-pipeline Pass 2: they include the
fixed naming-contract fixtures plus the helper's existing Note / body /
renameable locals, but not full fingerprint dumps from
``build_cluster_refactor_prompt_context``. LLM naming quality may differ
slightly from a live Pass 2 run.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from src.internals_refactor import (
    _strip_note_section,
    load_refactor_cache,
    save_refactor_cache,
    semantic_naming_cache_key,
    validate_refactored_internals,
)
from src.mechanical_body import MechanicalBodyDraft
from src.mechanical_naming import (
    ClusterNamingLLMResponse,
    NamingUnit,
    SingletonNamingLLMResponse,
    apply_cluster_naming_response,
    apply_naming_responses_to_module,
    format_cluster_naming_prompt_context,
    format_singleton_naming_prompt_context,
    load_cluster_naming_prompt_fixed_portion,
    load_singleton_naming_prompt_fixed_portion,
)
from src.runtime_symbols import (
    discover_allowed_formula_symbols,
    discover_allowed_reader_symbols,
    discover_allowed_runtime_symbols,
)

logger = logging.getLogger(__name__)

MECHANICAL_PENDING_DOCSTRING_SUMMARY = (
    "Mechanically synthesized helper pending semantic naming."
)

# Mechanical temps are ``_tN`` or multi-group ``_fG_tN``. Lambda params like
# ``_ln`` / ``_rn`` must never be treated as required renames.
_RENAMEABLE_TEMP_PATTERN = re.compile(r"^(_f\d+)?_t\d+$")
_NOTE_EXCEL_PATTERN = re.compile(
    r"Excel:\s*(?P<formula>.+?)\s*\Z",
    re.DOTALL,
)
_NOTE_COVERS_PATTERN = re.compile(
    r"Covers\s+(?P<covers>.+?)(?:\.\s*Excel:|\s*\Z)",
    re.DOTALL,
)


@dataclass(frozen=True)
class DiscoveredNamingHelper:
    """One mechanical helper discovered from ``internals.py`` source text."""

    kind: Literal["cluster", "singleton"]
    helper_name: str
    draft: MechanicalBodyDraft
    parameter_names: frozenset[str]
    note_section: str
    covered_addresses: str
    excel_formula: str


def discover_pending_naming_helpers(
    source: str,
) -> tuple[DiscoveredNamingHelper, ...]:
    """Return helpers whose docstring marks them pending semantic naming.

    Raises ``ValueError`` when a *semantic* helper (not a ``cell_*`` formula
    function) still has mechanical ``_tN`` / ``_fG_tN`` store targets but lacks
    the exact pending-marker summary. Unreformed ``cell_*`` bodies keep codegen
    temporaries and must not trip this check.
    """
    module = ast.parse(source)
    discovered: list[DiscoveredNamingHelper] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        docstring = ast.get_docstring(node)
        renameable, lookup_tables, body = _draft_parts_from_function(node)
        has_mechanical_temps = bool(renameable)
        suspected = _is_suspected_mechanical_helper(node)
        if docstring is None:
            if suspected and has_mechanical_temps:
                raise ValueError(
                    f"helper {node.name!r} has mechanical locals "
                    f"{sorted(renameable)} but no docstring; expected summary "
                    f"{MECHANICAL_PENDING_DOCSTRING_SUMMARY!r}"
                )
            continue
        summary = docstring.strip().splitlines()[0].strip()
        if summary != MECHANICAL_PENDING_DOCSTRING_SUMMARY:
            if suspected and has_mechanical_temps:
                raise ValueError(
                    f"helper {node.name!r} has mechanical locals "
                    f"{sorted(renameable)} but docstring summary "
                    f"{summary!r} is not the pending semantic naming marker "
                    f"{MECHANICAL_PENDING_DOCSTRING_SUMMARY!r}"
                )
            continue
        parameter_names = frozenset(
            arg.arg for arg in node.args.args if arg.arg != "ctx"
        )
        kind: Literal["cluster", "singleton"] = (
            "singleton" if not parameter_names else "cluster"
        )
        note_section = _extract_note_section(docstring)
        discovered.append(
            DiscoveredNamingHelper(
                kind=kind,
                helper_name=node.name,
                draft=MechanicalBodyDraft(
                    body=body,
                    renameable_locals=tuple(sorted(renameable)),
                    lookup_table_names=tuple(sorted(lookup_tables)),
                    group_count=_infer_group_count(renameable),
                ),
                parameter_names=parameter_names,
                note_section=note_section,
                covered_addresses=_covers_from_note(note_section),
                excel_formula=_excel_formula_from_note(note_section),
            )
        )
    return tuple(discovered)


def _is_suspected_mechanical_helper(node: ast.FunctionDef) -> bool:
    """Return whether ``node`` looks like a Pass-1 semantic helper.

    Formula ``cell_*`` functions and private ``_`` helpers are excluded: codegen
    cell bodies routinely use ``_tN`` temporaries and are not naming candidates.
    """
    if node.name.startswith("cell_") or node.name.startswith("_"):
        return False
    return bool(node.args.args and node.args.args[0].arg == "ctx")


def build_standalone_naming_prompt(helper: DiscoveredNamingHelper) -> str:
    """Build a thin naming prompt from the placeholder Note + draft body."""
    context_dump = (
        f"Helper name: {helper.helper_name}\n"
        f"Kind: {helper.kind}\n"
        f"Parameters: {', '.join(sorted(helper.parameter_names)) or '(ctx only)'}\n"
        f"Covered addresses: {helper.covered_addresses or '(unknown)'}\n"
        f"Excel formula / template: {helper.excel_formula or '(unknown)'}\n\n"
        "Existing Note (provenance; reattached after naming — document Args/"
        "Returns only):\n"
        f"{helper.note_section or '(none)'}"
    )
    if helper.kind == "cluster":
        return (
            load_cluster_naming_prompt_fixed_portion().strip()
            + "\n\n"
            + format_cluster_naming_prompt_context(context_dump, helper.draft)
        )
    return (
        load_singleton_naming_prompt_fixed_portion().strip()
        + "\n\n"
        + format_singleton_naming_prompt_context(context_dump, helper.draft)
    )


def forbidden_names_for_standalone(
    source: str,
    *,
    runtime_path: Path | None = None,
    readers_path: Path | None = None,
    allowed_runtime_symbols: Sequence[str] | None = None,
) -> frozenset[str]:
    """Sibling defs + runtime/reader allowlist + builtins."""
    module = ast.parse(source)
    names = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    if allowed_runtime_symbols is not None:
        names.update(allowed_runtime_symbols)
    elif runtime_path is not None:
        resolved_readers = readers_path
        if resolved_readers is None:
            sibling_readers = runtime_path.with_name("_readers.py")
            if sibling_readers.is_file():
                resolved_readers = sibling_readers
        if resolved_readers is not None:
            names.update(
                discover_allowed_formula_symbols(runtime_path, resolved_readers)
            )
        else:
            names.update(discover_allowed_runtime_symbols(runtime_path))
    elif readers_path is not None:
        names.update(discover_allowed_reader_symbols(readers_path))
    names.update(name for name in builtins.__dict__ if isinstance(name, str))
    return frozenset(names)


class GatherNamingResponses(Protocol):
    """LLM gather hook; ``on_success`` enables incremental cache writes."""

    def __call__(
        self,
        misses: Sequence[DiscoveredNamingHelper],
        prompts: Mapping[str, str],
        /,
        *,
        on_success: Callable[[str, ClusterNamingLLMResponse], None] | None = None,
    ) -> Mapping[str, ClusterNamingLLMResponse]: ...


def _invoke_gather_responses(
    gather_responses: Callable[..., Mapping[str, ClusterNamingLLMResponse]],
    misses: Sequence[DiscoveredNamingHelper],
    prompts: Mapping[str, str],
    *,
    on_success: Callable[[str, ClusterNamingLLMResponse], None],
) -> Mapping[str, ClusterNamingLLMResponse]:
    """Call gather with ``on_success`` when the callable accepts it."""
    try:
        parameters = inspect.signature(gather_responses).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "on_success" in parameters:
        return gather_responses(misses, prompts, on_success=on_success)
    results = gather_responses(misses, prompts)
    for helper_name, response in results.items():
        on_success(helper_name, response)
    return results


def run_standalone_semantic_naming(
    source: str,
    *,
    naming_responses: Mapping[str, ClusterNamingLLMResponse] | None = None,
    gather_responses: (
        GatherNamingResponses
        | Callable[
            [Sequence[DiscoveredNamingHelper], Mapping[str, str]],
            Mapping[str, ClusterNamingLLMResponse],
        ]
        | None
    ) = None,
    allowed_runtime_symbols: Sequence[str] | None = None,
    runtime_path: Path | None = None,
    readers_path: Path | None = None,
    internals_path: Path | None = None,
    dry_run: bool = True,
    use_cache: bool = True,
) -> str:
    """Name all pending mechanical helpers in ``source`` and return the result.

    Provide either ``naming_responses`` (tests / offline) or ``gather_responses``
    (LLM). When ``use_cache`` is true, hits from ``.cache/internals-refactors.json``
    are preferred and each successful miss is written back as it arrives so a
    mid-run failure can resume without re-paying completed LLM calls.
    """
    helpers = discover_pending_naming_helpers(source)
    if not helpers:
        logger.info("standalone semantic naming: no pending mechanical helpers")
        return source

    forbidden = forbidden_names_for_standalone(
        source,
        runtime_path=runtime_path,
        readers_path=readers_path,
        allowed_runtime_symbols=allowed_runtime_symbols,
    )
    cache = load_refactor_cache() if use_cache else {}
    prompts = {
        helper.helper_name: build_standalone_naming_prompt(helper) for helper in helpers
    }
    response_models: dict[str, type[ClusterNamingLLMResponse]] = {
        helper.helper_name: (
            ClusterNamingLLMResponse
            if helper.kind == "cluster"
            else SingletonNamingLLMResponse
        )
        for helper in helpers
    }
    cache_keys = {
        helper.helper_name: semantic_naming_cache_key(
            kind=helper.kind,
            unit_id=helper.helper_name,
            canonical_template=helper.excel_formula,
            mechanical_body=helper.draft.body,
            response_schema=response_models[helper.helper_name].model_json_schema(),
            member_fingerprints=(),
            contract="member_sweep" if helper.kind == "cluster" else None,
        )
        for helper in helpers
    }

    resolved: dict[str, ClusterNamingLLMResponse] = {}
    if naming_responses is not None:
        missing = sorted(set(cache_keys) - set(naming_responses))
        if missing:
            raise ValueError("naming_responses missing helpers: " + ", ".join(missing))
        for helper in helpers:
            response = naming_responses[helper.helper_name]
            _validate_naming_response(helper, response, forbidden=forbidden)
            resolved[helper.helper_name] = response
            if use_cache:
                cache[cache_keys[helper.helper_name]] = response.model_dump_json()
    else:
        misses: list[DiscoveredNamingHelper] = []
        for helper in helpers:
            cached = cache.get(cache_keys[helper.helper_name]) if use_cache else None
            if cached is None:
                misses.append(helper)
                continue
            try:
                response = response_models[helper.helper_name].model_validate_json(
                    cached
                )
                _validate_naming_response(helper, response, forbidden=forbidden)
            except (ValueError, ValidationError):
                cache.pop(cache_keys[helper.helper_name], None)
                misses.append(helper)
                continue
            resolved[helper.helper_name] = response
        if misses:
            if gather_responses is None:
                raise ValueError(
                    "standalone semantic naming needs gather_responses for cache "
                    f"misses: {[helper.helper_name for helper in misses]}"
                )
            helpers_by_name = {helper.helper_name: helper for helper in misses}

            def _on_success(
                helper_name: str, response: ClusterNamingLLMResponse
            ) -> None:
                helper = helpers_by_name[helper_name]
                _validate_naming_response(helper, response, forbidden=forbidden)
                resolved[helper_name] = response
                if use_cache and not dry_run:
                    cache[cache_keys[helper_name]] = response.model_dump_json()
                    save_refactor_cache(cache)

            fresh = _invoke_gather_responses(
                gather_responses,
                misses,
                prompts,
                on_success=_on_success,
            )
            missing_fresh = sorted(set(helpers_by_name) - set(fresh))
            if missing_fresh:
                raise ValueError(
                    "gather_responses missing helpers: " + ", ".join(missing_fresh)
                )

    units = [
        NamingUnit(
            helper_name=helper.helper_name,
            draft=helper.draft,
            response=resolved[helper.helper_name].model_copy(
                update={
                    "symbol_docstring": _docstring_with_preserved_note(
                        resolved[helper.helper_name].symbol_docstring,
                        helper.note_section,
                    )
                }
            ),
            parameter_names=helper.parameter_names,
            forbidden_names=forbidden,
        )
        for helper in helpers
    ]
    named_source = apply_naming_responses_to_module(source, units)
    validate_refactored_internals(named_source)
    if not dry_run:
        if internals_path is None:
            raise ValueError("internals_path is required when dry_run is false")
        internals_path.write_text(named_source, encoding="utf-8", newline="\n")
        if use_cache:
            save_refactor_cache(cache)
    return named_source


def _validate_naming_response(
    helper: DiscoveredNamingHelper,
    response: ClusterNamingLLMResponse,
    *,
    forbidden: frozenset[str],
) -> None:
    if response.error:
        raise ValueError(
            f"naming response for {helper.helper_name!r} declared error: "
            f"{response.error_reason}"
        )
    apply_cluster_naming_response(
        response,
        helper.draft,
        parameter_names=helper.parameter_names,
        forbidden_names=forbidden,
    )


def _docstring_with_preserved_note(
    symbol_docstring: str | None, note_section: str
) -> str:
    if symbol_docstring is None:
        raise ValueError("symbol_docstring must be provided on success")
    stripped = _strip_note_section(symbol_docstring)
    if not note_section:
        return stripped
    return f"{stripped.rstrip()}\n\nNote:\n    {note_section}"


def _draft_parts_from_function(
    node: ast.FunctionDef,
) -> tuple[set[str], set[str], str]:
    statements = list(node.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    parameter_names = {arg.arg for arg in node.args.args}
    renameable: set[str] = set()
    lookup_tables: set[str] = set()
    for statement in statements:
        for child in ast.walk(statement):
            if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Store):
                continue
            if _RENAMEABLE_TEMP_PATTERN.fullmatch(child.id):
                renameable.add(child.id)
            elif (
                not child.id.startswith("_")
                and child.id not in parameter_names
                and child.id != "ctx"
            ):
                lookup_tables.add(child.id)
    body = "\n".join(ast.unparse(statement) for statement in statements)
    return renameable, lookup_tables, body


def _infer_group_count(renameable: set[str]) -> int:
    groups = {
        match.group(1)
        for name in renameable
        if (match := re.match(r"^_f(\d+)_t\d+$", name)) is not None
    }
    return max(len(groups), 1)


def _extract_note_section(docstring: str) -> str:
    note_match = re.search(r"\nNote:\n(?P<body>.*)\Z", docstring, re.DOTALL)
    if note_match is None:
        return ""
    return _dedent_note(note_match.group("body"))


def _dedent_note(body: str) -> str:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    indent = min(indents) if indents else 0
    return "\n".join(line[indent:] if len(line) >= indent else line for line in lines)


def _covers_from_note(note_section: str) -> str:
    match = _NOTE_COVERS_PATTERN.search(note_section)
    if match is None:
        return ""
    return match.group("covers").strip().rstrip(".")


def _excel_formula_from_note(note_section: str) -> str:
    match = _NOTE_EXCEL_PATTERN.search(note_section)
    if match is None:
        return ""
    return match.group("formula").strip()
