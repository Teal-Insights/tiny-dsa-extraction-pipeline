from dotenv import load_dotenv
import pytest

import workbook_config
from src.extraction_pipeline import build_pipeline_graph
from src.graph_dependency_audit import (
    audit_parent_dependencies_with_llm,
    format_audit_failure,
    resolve_graph_audit_model,
    select_audit_cases,
    validate_audit_cases,
)
from src.llm_providers import build_client_if_configured
from src.pipeline_config import load_pipeline_config, validate_pipeline_config

load_dotenv()


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_that_graph_is_correct() -> None:
    """Spot-check direct dependency sets for high-value parent formula cells."""
    audit_cases = workbook_config.GRAPH_AUDIT_CASES
    if not audit_cases:
        pytest.skip(
            "workbook_config.GRAPH_AUDIT_CASES is empty; declare audit cases to run "
            "LLM graph dependency audits"
        )

    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    pytest.importorskip(
        "openai",
        reason="requires openai SDK for LLM-based graph accuracy testing",
    )

    try:
        model = resolve_graph_audit_model()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    client, provider = build_client_if_configured(model)
    if client is None:
        pytest.skip(f"{provider.api_key_env} is not set")

    graph, _series_bindings, _input_series, _output_series = build_pipeline_graph(
        config
    )
    validate_audit_cases(graph, audit_cases)
    selected_cases = select_audit_cases(graph, audit_cases)

    failures: list[str] = []
    for case in selected_cases:
        verdict, evidence = audit_parent_dependencies_with_llm(
            client=client,
            provider=provider,
            graph=graph,
            case=case,
            model=model,
        )
        if verdict.verdict != "correct":
            failures.append(format_audit_failure(case, verdict, evidence))

    assert not failures, "Graph dependency audits failed:\n" + "\n".join(failures)
