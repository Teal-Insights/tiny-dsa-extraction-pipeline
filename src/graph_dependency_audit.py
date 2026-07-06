"""Direct-dependency LLM audits for extracted workbook graphs.

Model selection uses ``LLM_GRAPH_AUDIT_MODEL`` (default ``gpt-5.5`` when unset).
The model name prefix routes through the same OpenAI-compatible providers as
other pipeline stages: ``gpt-*`` (OpenAI), ``glm-*`` (Z.AI), and
``deepseek-*`` (DeepSeek). One model—and therefore one provider—handles every
audit case in a run.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from excel_grapher.core.address_keys import normalize_key
from excel_grapher.grapher.dependency_provenance import DependencyCause
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel, ConfigDict, Field

from src.env_utils import env_int
from src.llm_json import generate_validated_json, generate_validated_json_async
from src.llm_providers import (
    ProviderConfig,
    build_async_client,
    build_client,
    model_from_env,
)

LLM_GRAPH_AUDIT_MODEL_ENV = "LLM_GRAPH_AUDIT_MODEL"
LLM_GRAPH_AUDIT_CASES_ENV = "LLM_GRAPH_AUDIT_CASES"
LLM_GRAPH_AUDIT_SEED_ENV = "LLM_GRAPH_AUDIT_SEED"

DEFAULT_MAX_CHILDREN = 40
DEFAULT_MAX_FORMULA_LENGTH = 240
DEFAULT_CASE_COUNT = 5
DEFAULT_CASE_SEED = 0

VERDICT_JSON_SCHEMA = """{
  "verdict": "correct" | "incorrect" | "inconclusive",
  "missing_dependencies": ["Sheet!A1"],
  "spurious_dependencies": ["Sheet!B2"],
  "reasoning": "string",
  "confidence": "low" | "medium" | "high"
}"""

VERDICT_JSON_EXAMPLE = """{
  "verdict": "correct",
  "missing_dependencies": [],
  "spurious_dependencies": [],
  "reasoning": "All direct references in the parent formula appear in the dependency list.",
  "confidence": "high"
}"""

SYSTEM_PROMPT = f"""You audit Excel dependency graphs extracted from formulas.

Edge direction: parent -> child means the parent formula depends on the child cell.

Review only the parent cell's DIRECT dependencies listed in the evidence. Compare the
parent formula against that dependency set. Identify:
- missing_dependencies: workbook cells or dynamic-ref alternatives the formula clearly
  requires as direct inputs but that are absent from the graph child list
- spurious_dependencies: graph child edges that the formula does not justify

Do not speculate about transitive ancestors beyond direct references in the parent formula.
If the evidence is incomplete, respond with verdict "inconclusive" and explain why.

Respond with JSON only. Your response must be a single JSON object matching this schema:
{VERDICT_JSON_SCHEMA}

Example response:
{VERDICT_JSON_EXAMPLE}"""


@dataclass(frozen=True, slots=True)
class GraphAuditCase:
    parent_key: str
    label: str
    focus: str
    required: bool = False


class GraphDependencyAuditVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["correct", "incorrect", "inconclusive"]
    missing_dependencies: list[str] = Field(default_factory=list)
    spurious_dependencies: list[str] = Field(default_factory=list)
    unverified_missing_dependencies: list[str] = Field(default_factory=list)
    invalid_spurious_dependencies: list[str] = Field(default_factory=list)
    reasoning: str
    confidence: Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class DirectDependencyRecord:
    child_key: NodeKey
    role: str
    formula: str | None
    value: object
    guard: str | None
    provenance: str


@dataclass(frozen=True, slots=True)
class ParentAuditEvidence:
    case: GraphAuditCase
    parent_key: NodeKey
    parent_formula: str | None
    parent_normalized_formula: str | None
    parent_value: object
    direct_dependencies: tuple[DirectDependencyRecord, ...]
    total_dependency_count: int
    truncated_dependency_count: int
    parent_formula_truncated: bool


def resolve_graph_audit_model(model: str | None = None) -> str:
    """Return the caller's model, else ``LLM_GRAPH_AUDIT_MODEL``, else ``gpt-5.5``.

    The resolved name selects the provider via :func:`src.llm_providers.provider_for_model`.
    """
    return model_from_env(LLM_GRAPH_AUDIT_MODEL_ENV, model)


def _sheet_name(address: str) -> str:
    return address.split("!", 1)[0].strip("'")


def _truncate_text(text: str | None, *, max_length: int) -> str | None:
    if text is None:
        return None
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


def _text_was_truncated(text: str | None, *, max_length: int) -> bool:
    return text is not None and len(text) > max_length


def _address_parts(address: str) -> tuple[str, str, int]:
    sheet, col_row = address.split("!", 1)
    sheet = sheet.strip("'")
    column = ""
    row_text = ""
    for index, character in enumerate(col_row):
        if character.isdigit():
            column = col_row[:index]
            row_text = col_row[index:]
            break
    if not column or not row_text:
        raise ValueError(f"invalid workbook address: {address}")
    return sheet, column, int(row_text)


def _child_referenced_in_formula(
    child_key: str,
    formula: str | None,
    *,
    parent_sheet: str,
) -> bool:
    if formula is None:
        return False
    formula_text = formula.replace("$", "").upper()
    child_sheet, column, row = _address_parts(child_key)
    cell_ref = f"{column}{row}".upper()
    if cell_ref in formula_text:
        return True
    if child_sheet == parent_sheet:
        return False
    sheet_patterns = (
        f"{child_sheet}!{column}{row}",
        f"'{child_sheet}'!{column}{row}",
    )
    return any(pattern.upper() in formula_text for pattern in sheet_patterns)


def _select_visible_dependencies(
    dependencies: list[NodeKey],
    *,
    child_limit: int,
    parent_formula: str | None,
    parent_sheet: str,
) -> list[NodeKey]:
    if len(dependencies) <= child_limit:
        return dependencies

    formula_referenced: list[NodeKey] = []
    remaining: list[NodeKey] = []
    for dependency in dependencies:
        if _child_referenced_in_formula(
            dependency,
            parent_formula,
            parent_sheet=parent_sheet,
        ):
            formula_referenced.append(dependency)
        else:
            remaining.append(dependency)

    selected = list(formula_referenced)
    for dependency in remaining:
        if len(selected) >= child_limit:
            break
        selected.append(dependency)
    return selected[:child_limit]


def _format_provenance(causes: frozenset[DependencyCause]) -> str:
    if not causes:
        return "none"
    return ",".join(sorted(cause.value for cause in causes))


def _node_role(
    graph: DependencyGraph,
    key: NodeKey,
) -> str:
    node = graph.get_node(key)
    if node is None:
        return "missing"
    if node.formula:
        return "formula"
    classification = graph.leaf_classification
    if classification is not None:
        kind = classification.get(key)
        if kind is not None:
            return kind
    return "leaf"


def case_difficulty_score(
    graph: DependencyGraph, parent_key: str
) -> tuple[int, int, int]:
    normalized = normalize_key(parent_key)
    dependencies = list(graph.get_dependencies(normalized))
    dynamic_or_guarded = 0
    cross_sheet = set()
    parent_sheet = _sheet_name(normalized)
    for dependency in dependencies:
        edge = graph.get_edge_attrs(normalized, dependency)
        if edge.guard is not None:
            dynamic_or_guarded += 1
        provenance = edge.provenance
        if provenance is not None and provenance.causes:
            if any(
                cause
                in {
                    DependencyCause.dynamic_offset,
                    DependencyCause.dynamic_indirect,
                }
                for cause in provenance.causes
            ):
                dynamic_or_guarded += 1
        if _sheet_name(dependency) != parent_sheet:
            cross_sheet.add(_sheet_name(dependency))
    return (len(dependencies), dynamic_or_guarded, len(cross_sheet))


def validate_audit_cases(
    graph: DependencyGraph, cases: tuple[GraphAuditCase, ...]
) -> None:
    missing: list[str] = []
    non_formula: list[str] = []
    for case in cases:
        parent_key = normalize_key(case.parent_key)
        node = graph.get_node(parent_key)
        if node is None:
            missing.append(case.parent_key)
            continue
        if node.formula is None:
            non_formula.append(case.parent_key)
    if missing:
        raise ValueError(f"audit parent cells missing from graph: {missing}")
    if non_formula:
        raise ValueError(f"audit parent cells are not formula nodes: {non_formula}")


def select_audit_cases(
    graph: DependencyGraph,
    cases: tuple[GraphAuditCase, ...],
    *,
    case_count: int | None = None,
    seed: int | None = None,
) -> list[GraphAuditCase]:
    count = (
        case_count
        if case_count is not None
        else env_int(LLM_GRAPH_AUDIT_CASES_ENV, DEFAULT_CASE_COUNT)
    )
    rng_seed = (
        seed
        if seed is not None
        else env_int(LLM_GRAPH_AUDIT_SEED_ENV, DEFAULT_CASE_SEED)
    )
    if count <= 0:
        raise ValueError("case_count must be positive")

    required = [case for case in cases if case.required]
    optional = [case for case in cases if not case.required]
    optional.sort(
        key=lambda case: case_difficulty_score(graph, case.parent_key),
        reverse=True,
    )

    selected: list[GraphAuditCase] = []
    seen: set[str] = set()

    def add_case(case: GraphAuditCase) -> None:
        normalized = normalize_key(case.parent_key)
        if normalized in seen:
            return
        selected.append(case)
        seen.add(normalized)

    for case in required:
        if len(selected) >= count:
            break
        add_case(case)

    if len(selected) < count and optional:
        remaining = count - len(selected)
        pool = [case for case in optional if normalize_key(case.parent_key) not in seen]
        rng = random.Random(rng_seed)
        for case in rng.sample(pool, k=min(remaining, len(pool))):
            add_case(case)

    return selected[:count]


def collect_parent_audit_evidence(
    graph: DependencyGraph,
    case: GraphAuditCase,
    *,
    max_children: int | None = None,
    max_formula_length: int | None = None,
) -> ParentAuditEvidence:
    child_limit = max_children if max_children is not None else DEFAULT_MAX_CHILDREN
    formula_limit = (
        max_formula_length
        if max_formula_length is not None
        else DEFAULT_MAX_FORMULA_LENGTH
    )

    parent_key = normalize_key(case.parent_key)
    parent = graph.get_node(parent_key)
    if parent is None:
        raise KeyError(f"parent cell not found in graph: {case.parent_key}")
    if parent.formula is None:
        raise ValueError(f"parent cell is not a formula node: {case.parent_key}")

    dependencies = sorted(graph.get_dependencies(parent_key))
    visible_dependencies = _select_visible_dependencies(
        dependencies,
        child_limit=child_limit,
        parent_formula=parent.formula,
        parent_sheet=_sheet_name(parent_key),
    )
    records: list[DirectDependencyRecord] = []
    for dependency in visible_dependencies:
        child = graph.get_node(dependency)
        edge = graph.get_edge_attrs(parent_key, dependency)
        provenance = edge.provenance
        records.append(
            DirectDependencyRecord(
                child_key=dependency,
                role=_node_role(graph, dependency),
                formula=_truncate_text(
                    child.formula if child is not None else None,
                    max_length=formula_limit,
                ),
                value=child.value if child is not None else None,
                guard=str(edge.guard) if edge.guard is not None else None,
                provenance=_format_provenance(
                    provenance.causes if provenance is not None else frozenset()
                ),
            )
        )

    return ParentAuditEvidence(
        case=case,
        parent_key=parent_key,
        parent_formula=_truncate_text(parent.formula, max_length=formula_limit),
        parent_normalized_formula=_truncate_text(
            parent.normalized_formula,
            max_length=formula_limit,
        ),
        parent_value=parent.value,
        direct_dependencies=tuple(records),
        total_dependency_count=len(dependencies),
        truncated_dependency_count=max(
            0, len(dependencies) - len(visible_dependencies)
        ),
        parent_formula_truncated=_text_was_truncated(
            parent.formula,
            max_length=formula_limit,
        ),
    )


def build_parent_audit_prompt(evidence: ParentAuditEvidence) -> str:
    lines = [
        f"Audit case: {evidence.case.label}",
        f"Focus: {evidence.case.focus}",
        f"Parent cell: {evidence.parent_key}",
        f"Parent formula: {evidence.parent_formula}",
        f"Parent normalized formula: {evidence.parent_normalized_formula}",
        f"Parent value: {evidence.parent_value!r}",
        "",
        "Direct graph dependencies:",
    ]
    if not evidence.direct_dependencies:
        lines.append("- (none)")
    for record in evidence.direct_dependencies:
        lines.append(
            "- "
            f"{record.child_key} | role={record.role} | guard={record.guard} | "
            f"provenance={record.provenance} | formula={record.formula!r} | "
            f"value={record.value!r}"
        )
    if evidence.truncated_dependency_count:
        lines.extend(
            [
                "",
                f"Note: {evidence.truncated_dependency_count} additional direct dependencies "
                f"were omitted from this prompt (showing "
                f"{len(evidence.direct_dependencies)} of "
                f"{evidence.total_dependency_count}). Formula-referenced children are "
                "prioritized when the dependency budget is exceeded.",
            ]
        )
    if evidence.parent_formula_truncated:
        lines.extend(
            [
                "",
                "Note: the parent formula text was truncated for this prompt.",
            ]
        )
    lines.extend(
        [
            "",
            "Return JSON only. Use sheet-qualified addresses in dependency lists.",
        ]
    )
    return "\n".join(lines)


def evidence_is_complete_for_audit(evidence: ParentAuditEvidence) -> bool:
    return (
        evidence.truncated_dependency_count == 0
        and not evidence.parent_formula_truncated
    )


def inconclusive_verdict_for_truncated_evidence() -> GraphDependencyAuditVerdict:
    return GraphDependencyAuditVerdict(
        verdict="inconclusive",
        reasoning=(
            "Audit skipped because dependency evidence was incomplete "
            "(evidence_truncated)."
        ),
        confidence="low",
    )


def validate_and_sanitize_audit_verdict(
    graph: DependencyGraph,
    parent_key: NodeKey,
    verdict: GraphDependencyAuditVerdict,
) -> GraphDependencyAuditVerdict:
    direct_children = set(graph.get_dependencies(parent_key))
    verified_missing: list[str] = []
    unverified_missing: list[str] = []
    for address in verdict.missing_dependencies:
        try:
            normalized = normalize_key(address)
        except ValueError:
            unverified_missing.append(address)
            continue
        if graph.get_node(normalized) is not None:
            verified_missing.append(normalized)
        else:
            unverified_missing.append(normalized)

    verified_spurious: list[str] = []
    invalid_spurious: list[str] = []
    for address in verdict.spurious_dependencies:
        try:
            normalized = normalize_key(address)
        except ValueError:
            invalid_spurious.append(address)
            continue
        if normalized in direct_children:
            verified_spurious.append(normalized)
        else:
            invalid_spurious.append(normalized)

    final_verdict = verdict.verdict
    final_confidence = verdict.confidence
    reasoning = verdict.reasoning
    if unverified_missing or invalid_spurious:
        final_verdict = "inconclusive"
        final_confidence = "low"
        details: list[str] = []
        if unverified_missing:
            details.append(
                "unverified missing addresses: " + ", ".join(unverified_missing)
            )
        if invalid_spurious:
            details.append("invalid spurious addresses: " + ", ".join(invalid_spurious))
        reasoning = f"{reasoning} ({'; '.join(details)})"

    return GraphDependencyAuditVerdict(
        verdict=final_verdict,
        missing_dependencies=verified_missing,
        spurious_dependencies=verified_spurious,
        unverified_missing_dependencies=unverified_missing,
        invalid_spurious_dependencies=invalid_spurious,
        reasoning=reasoning,
        confidence=final_confidence,
    )


def _finalize_audit_result(
    graph: DependencyGraph,
    evidence: ParentAuditEvidence,
    verdict: GraphDependencyAuditVerdict,
) -> GraphDependencyAuditVerdict:
    if not evidence_is_complete_for_audit(evidence):
        return inconclusive_verdict_for_truncated_evidence()
    return validate_and_sanitize_audit_verdict(graph, evidence.parent_key, verdict)


def request_audit_verdict_from_llm(
    *,
    client: OpenAI,
    provider: ProviderConfig,
    user_prompt: str,
    model: str,
) -> GraphDependencyAuditVerdict:
    verdict, _content = generate_validated_json(
        client=client,
        model=model,
        provider=provider,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_model=GraphDependencyAuditVerdict,
    )
    return verdict


async def request_audit_verdict_from_llm_async(
    *,
    client: AsyncOpenAI,
    provider: ProviderConfig,
    user_prompt: str,
    model: str,
) -> GraphDependencyAuditVerdict:
    verdict, _content = await generate_validated_json_async(
        client=client,
        model=model,
        provider=provider,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_model=GraphDependencyAuditVerdict,
    )
    return verdict


async def audit_parent_dependencies_with_llm_async(
    *,
    client: AsyncOpenAI,
    provider: ProviderConfig,
    graph: DependencyGraph,
    case: GraphAuditCase,
    model: str,
    max_children: int | None = None,
    max_formula_length: int | None = None,
) -> tuple[GraphDependencyAuditVerdict, ParentAuditEvidence]:
    evidence = collect_parent_audit_evidence(
        graph,
        case,
        max_children=max_children,
        max_formula_length=max_formula_length,
    )
    if not evidence_is_complete_for_audit(evidence):
        return inconclusive_verdict_for_truncated_evidence(), evidence
    verdict = await request_audit_verdict_from_llm_async(
        client=client,
        provider=provider,
        user_prompt=build_parent_audit_prompt(evidence),
        model=model,
    )
    return _finalize_audit_result(graph, evidence, verdict), evidence


async def audit_parent_dependencies_batch_with_llm(
    *,
    client: AsyncOpenAI,
    provider: ProviderConfig,
    graph: DependencyGraph,
    cases: Sequence[GraphAuditCase],
    model: str,
    max_children: int | None = None,
    max_formula_length: int | None = None,
) -> list[tuple[GraphDependencyAuditVerdict, ParentAuditEvidence]]:
    """Run independent parent dependency audits concurrently."""
    return list(
        await asyncio.gather(
            *(
                audit_parent_dependencies_with_llm_async(
                    client=client,
                    provider=provider,
                    graph=graph,
                    case=case,
                    model=model,
                    max_children=max_children,
                    max_formula_length=max_formula_length,
                )
                for case in cases
            )
        )
    )


def audit_parent_dependencies_with_llm(
    *,
    client: OpenAI,
    provider: ProviderConfig,
    graph: DependencyGraph,
    case: GraphAuditCase,
    model: str,
    max_children: int | None = None,
    max_formula_length: int | None = None,
) -> tuple[GraphDependencyAuditVerdict, ParentAuditEvidence]:
    evidence = collect_parent_audit_evidence(
        graph,
        case,
        max_children=max_children,
        max_formula_length=max_formula_length,
    )
    if not evidence_is_complete_for_audit(evidence):
        return inconclusive_verdict_for_truncated_evidence(), evidence
    verdict = request_audit_verdict_from_llm(
        client=client,
        provider=provider,
        user_prompt=build_parent_audit_prompt(evidence),
        model=model,
    )
    return _finalize_audit_result(graph, evidence, verdict), evidence


def build_graph_audit_client(
    model: str | None = None,
    *,
    api_key: str | None = None,
) -> tuple[OpenAI, ProviderConfig, str]:
    """Build a provider client for graph dependency audits."""
    resolved_model = resolve_graph_audit_model(model)
    client, provider = build_client(resolved_model, api_key=api_key)
    return client, provider, resolved_model


def build_graph_audit_async_client(
    model: str | None = None,
    *,
    api_key: str | None = None,
) -> tuple[AsyncOpenAI, ProviderConfig, str]:
    """Build an async provider client for graph dependency audits."""
    resolved_model = resolve_graph_audit_model(model)
    client, provider = build_async_client(resolved_model, api_key=api_key)
    return client, provider, resolved_model


def format_audit_failure(
    case: GraphAuditCase,
    verdict: GraphDependencyAuditVerdict,
    evidence: ParentAuditEvidence,
) -> str:
    return (
        f"{case.label} ({case.parent_key}): verdict={verdict.verdict}, "
        f"confidence={verdict.confidence}, "
        f"missing={verdict.missing_dependencies}, "
        f"spurious={verdict.spurious_dependencies}, "
        f"unverified_missing={verdict.unverified_missing_dependencies}, "
        f"invalid_spurious={verdict.invalid_spurious_dependencies}, "
        f"reasoning={verdict.reasoning}, "
        f"shown_dependencies={len(evidence.direct_dependencies)}/"
        f"{evidence.total_dependency_count}"
    )
