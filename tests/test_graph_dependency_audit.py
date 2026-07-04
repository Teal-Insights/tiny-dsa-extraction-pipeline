"""Tests for direct-dependency graph LLM audit helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node

from src.graph_dependency_audit import (
    GraphAuditCase,
    GraphDependencyAuditVerdict,
    SYSTEM_PROMPT,
    audit_parent_dependencies_batch_with_llm,
    audit_parent_dependencies_with_llm,
    build_graph_audit_client,
    build_parent_audit_prompt,
    collect_parent_audit_evidence,
    format_audit_failure,
    select_audit_cases,
    validate_audit_cases,
)
from src.llm_providers import provider_for_model
from tests.fixtures.synthetic_pipeline import GRAPH_AUDIT_CASES


def _formula_node(sheet: str, column: str, row: int, formula: str) -> Node:
    return Node(
        sheet=sheet,
        column=column,
        row=row,
        formula=formula,
        normalized_formula=formula,
        value=None,
        is_leaf=False,
        metadata={},
    )


def _leaf_node(sheet: str, column: str, row: int, value: object = 1) -> Node:
    return Node(
        sheet=sheet,
        column=column,
        row=row,
        formula=None,
        normalized_formula=None,
        value=value,
        is_leaf=True,
        metadata={},
    )


def _sample_graph() -> DependencyGraph:
    graph = DependencyGraph()
    graph.add_node(_formula_node("Output Baseline", "C", 5, "=Baseline!D11"))
    graph.add_node(_formula_node("Baseline", "D", 11, "=Dashboard!C12+Macrofiscal!Z3"))
    graph.add_node(_leaf_node("Dashboard", "C", 12, "Afghanistan"))
    graph.add_node(_leaf_node("Macrofiscal", "Z", 3, 0.5))
    graph.add_edge("Output Baseline!C5", "Baseline!D11")
    graph.add_edge("Baseline!D11", "Dashboard!C12")
    graph.add_edge("Baseline!D11", "Macrofiscal!Z3")
    graph.leaf_classification = {
        "Dashboard!C12": "input",
        "Macrofiscal!Z3": "constant",
    }
    return graph


def test_validate_audit_cases_requires_formula_parents() -> None:
    graph = _sample_graph()
    validate_audit_cases(
        graph,
        (
            GraphAuditCase(
                parent_key="Output Baseline!C5",
                label="baseline",
                focus="baseline path",
            ),
        ),
    )
    with pytest.raises(ValueError, match="not formula nodes"):
        validate_audit_cases(
            graph,
            (
                GraphAuditCase(
                    parent_key="Dashboard!C12",
                    label="dashboard",
                    focus="input",
                ),
            ),
        )


def test_collect_parent_audit_evidence_includes_guard_and_provenance_fields() -> None:
    graph = _sample_graph()
    case = GraphAuditCase(
        parent_key="Baseline!D11",
        label="baseline_engine",
        focus="engine dependencies",
    )
    evidence = collect_parent_audit_evidence(
        graph,
        case,
        max_children=10,
        max_formula_length=100,
    )
    assert evidence.total_dependency_count == 2
    assert len(evidence.direct_dependencies) == 2
    assert {record.child_key for record in evidence.direct_dependencies} == {
        "Dashboard!C12",
        "Macrofiscal!Z3",
    }
    assert evidence.direct_dependencies[0].provenance == "none"


def test_build_parent_audit_prompt_notes_truncation() -> None:
    graph = _sample_graph()
    case = GraphAuditCase(
        parent_key="Baseline!D11",
        label="baseline_engine",
        focus="engine dependencies",
    )
    evidence = collect_parent_audit_evidence(
        graph,
        case,
        max_children=1,
        max_formula_length=100,
    )
    prompt = build_parent_audit_prompt(evidence)
    assert "1 additional direct dependencies were omitted" in prompt
    assert "Parent formula:" in prompt
    assert "Return JSON only" in prompt


def test_select_audit_cases_prefers_required_cases() -> None:
    graph = _sample_graph()
    cases = (
        GraphAuditCase("Output Baseline!C5", "a", "focus", required=True),
        GraphAuditCase("Baseline!D11", "b", "focus"),
        GraphAuditCase("Dashboard!C12", "c", "focus"),
    )
    graph.add_node(_formula_node("Dashboard", "C", 12, "=1"))
    selected = select_audit_cases(graph, cases, case_count=1, seed=0)
    assert len(selected) == 1
    assert selected[0].label == "a"


def test_format_audit_failure_is_parent_specific() -> None:
    case = GraphAuditCase("Baseline!D11", "baseline_engine", "focus")
    evidence = collect_parent_audit_evidence(
        _sample_graph(),
        case,
        max_children=10,
        max_formula_length=100,
    )
    verdict = GraphDependencyAuditVerdict(
        verdict="incorrect",
        missing_dependencies=["Dashboard!C99"],
        spurious_dependencies=[],
        reasoning="Country controller missing.",
        confidence="high",
    )
    message = format_audit_failure(case, verdict, evidence)
    assert "baseline_engine" in message
    assert "Dashboard!C99" in message


def test_system_prompt_includes_json_schema_and_example() -> None:
    assert "Respond with JSON only" in SYSTEM_PROMPT
    assert '"verdict": "correct" | "incorrect"' in SYSTEM_PROMPT
    assert "Example response:" in SYSTEM_PROMPT


def test_build_graph_audit_client_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_GRAPH_AUDIT_MODEL", "gpt-5.5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_graph_audit_client()


def test_audit_parent_dependencies_with_llm_uses_validated_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import cast

    from openai import OpenAI

    graph = _sample_graph()
    case = GraphAuditCase("Baseline!D11", "baseline_engine", "focus")
    verdict = GraphDependencyAuditVerdict(
        verdict="correct",
        missing_dependencies=[],
        spurious_dependencies=[],
        reasoning="ok",
        confidence="high",
    )

    class FakeClient:
        pass

    fake_client = cast(OpenAI, FakeClient())
    provider = provider_for_model("gpt-5.5")

    with patch(
        "src.graph_dependency_audit.generate_validated_json",
        return_value=(verdict, verdict.model_dump_json()),
    ) as generate:
        result_verdict, evidence = audit_parent_dependencies_with_llm(
            client=fake_client,
            provider=provider,
            graph=graph,
            case=case,
            model="gpt-5.5",
        )

    assert result_verdict.verdict == "correct"
    assert evidence.parent_key == "Baseline!D11"
    generate.assert_called_once()
    call = generate.call_args.kwargs
    assert call["client"] is fake_client
    assert call["model"] == "gpt-5.5"
    assert call["provider"] is provider
    assert call["response_model"] is GraphDependencyAuditVerdict
    assert "Baseline!D11" in call["user_prompt"]


def test_audit_parent_dependencies_batch_with_llm_runs_all_cases() -> None:
    import asyncio
    from typing import cast

    from openai import AsyncOpenAI

    graph = _sample_graph()
    cases = (
        GraphAuditCase("Baseline!D11", "baseline_engine", "focus"),
        GraphAuditCase("Output Baseline!C5", "output_baseline", "focus"),
    )
    verdict = GraphDependencyAuditVerdict(
        verdict="correct",
        missing_dependencies=[],
        spurious_dependencies=[],
        reasoning="ok",
        confidence="high",
    )

    class FakeClient:
        pass

    fake_client = cast(AsyncOpenAI, FakeClient())
    provider = provider_for_model("gpt-5.5")

    with patch(
        "src.graph_dependency_audit.generate_validated_json_async",
        return_value=(verdict, verdict.model_dump_json()),
    ) as generate:
        results = asyncio.run(
            audit_parent_dependencies_batch_with_llm(
                client=fake_client,
                provider=provider,
                graph=graph,
                cases=cases,
                model="gpt-5.5",
            )
        )

    assert len(results) == 2
    assert {evidence.parent_key for _, evidence in results} == {
        "'Output Baseline'!C5",
        "Baseline!D11",
    }
    assert generate.await_count == 2


def test_synthetic_catalog_parents_exist_in_extracted_graph(
    synthetic_graph,
) -> None:
    validate_audit_cases(synthetic_graph, GRAPH_AUDIT_CASES)
