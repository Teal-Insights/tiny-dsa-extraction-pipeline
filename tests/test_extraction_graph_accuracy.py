from dotenv import load_dotenv
import asyncio
import pytest

from excel_grapher.grapher import DependencyGraph
from src.extraction_pipeline import build_pipeline_graph
from src.graph_dependency_audit import (
    GraphAuditCase,
    audit_parent_dependencies_batch_with_llm,
    format_audit_failure,
    resolve_graph_audit_model,
    select_audit_cases,
    validate_audit_cases,
)
from src.llm_providers import build_async_client_if_configured
from src.pipeline_config import (
    PipelineConfig,
    load_pipeline_config,
    validate_pipeline_config,
)
from tests.conftest import SyntheticConfiguredPipeline

load_dotenv()


def _run_llm_graph_dependency_audit(
    *,
    config: PipelineConfig,
    graph: DependencyGraph,
    audit_cases: tuple[GraphAuditCase, ...],
    empty_cases_message: str,
) -> None:
    if not audit_cases:
        pytest.skip(empty_cases_message)

    pytest.importorskip(
        "openai",
        reason="requires openai SDK for LLM-based graph accuracy testing",
    )

    try:
        model = resolve_graph_audit_model()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    client, provider = build_async_client_if_configured(model)
    if client is None:
        pytest.skip(f"{provider.api_key_env} is not set")

    validate_audit_cases(graph, audit_cases)
    selected_cases = select_audit_cases(graph, audit_cases)

    results = asyncio.run(
        audit_parent_dependencies_batch_with_llm(
            client=client,
            provider=provider,
            graph=graph,
            cases=selected_cases,
            model=model,
        )
    )

    failures: list[str] = []
    for case, (verdict, evidence) in zip(selected_cases, results, strict=True):
        if verdict.verdict != "correct":
            failures.append(format_audit_failure(case, verdict, evidence))

    assert not failures, "Graph dependency audits failed:\n" + "\n".join(failures)


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_workbook_graph_is_correct() -> None:
    """Spot-check direct dependency sets for the configured workbook graph."""
    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    graph_result = build_pipeline_graph(config)
    graph = graph_result.graph
    _run_llm_graph_dependency_audit(
        config=config,
        graph=graph,
        audit_cases=config.graph_audit_cases,
        empty_cases_message=(
            "workbook_config.GRAPH_AUDIT_CASES is empty; declare audit cases in "
            "workbook_config.py to run LLM graph dependency audits against the "
            "configured workbook"
        ),
    )


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_synthetic_graph_is_correct(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    """Spot-check direct dependency sets for the synthetic smoke-test graph."""
    pipeline = synthetic_configured_pipeline
    _run_llm_graph_dependency_audit(
        config=pipeline.config,
        graph=pipeline.graph,
        audit_cases=pipeline.config.graph_audit_cases,
        empty_cases_message=(
            "Synthetic pipeline config has no graph audit cases; this should not "
            "happen because the synthetic fixture declares GRAPH_AUDIT_CASES"
        ),
    )
