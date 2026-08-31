import asyncio
from collections.abc import Mapping
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from excel_grapher.grapher import DependencyGraph

from src.graph_cache import (
    COMMITTED_GRAPH_CACHE_DIR,
    DependencyGraphCacheResult,
    try_load_cached_dependency_graph,
)
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
    leaf_classification: Mapping[str, str],
) -> None:
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
    if not selected_cases:
        pytest.skip(
            "No eligible formula parents for graph audit "
            "(empty discovery after fan-out filter)"
        )

    results = asyncio.run(
        audit_parent_dependencies_batch_with_llm(
            client=client,
            provider=provider,
            graph=graph,
            cases=selected_cases,
            model=model,
            leaf_classification=leaf_classification,
        )
    )

    failures: list[str] = []
    for case, (verdict, evidence) in zip(selected_cases, results, strict=True):
        if verdict.verdict != "correct":
            failures.append(format_audit_failure(case, verdict, evidence))

    assert not failures, "Graph dependency audits failed:\n" + "\n".join(failures)


def _load_workbook_graph_for_audit(config: PipelineConfig) -> DependencyGraph:
    """Load the warm committed graph read-only; never cold-build under pytest."""
    cached = try_load_cached_dependency_graph(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=config.blank_ranges,
        cache_dir=COMMITTED_GRAPH_CACHE_DIR,
    )
    if cached is None:
        pytest.skip(
            "No warm committed dependency-graph cache for the current workbook/"
            "targets/constraints/blank_ranges. Run `uv run python -m src.extraction_pipeline "
            "--only-stage extract` (or `uv run python -m scripts.regenerate_graph_cache`) "
            "first, then re-run with --run-skipped."
        )
    return cached.graph


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_workbook_graph_is_correct() -> None:
    """Spot-check direct dependency sets for the configured workbook graph."""
    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    graph = _load_workbook_graph_for_audit(config)
    _run_llm_graph_dependency_audit(
        config=config,
        graph=graph,
        leaf_classification={},
        audit_cases=config.graph_audit_cases,
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
        leaf_classification=pipeline.leaf_classification,
        audit_cases=pipeline.config.graph_audit_cases,
    )


def test_workbook_audit_loads_committed_cache_read_only() -> None:
    config = load_pipeline_config()
    graph = DependencyGraph()
    cached = DependencyGraphCacheResult(
        graph=graph,
        cache_key="hit-key",
        cache_hit=True,
        elapsed_seconds=0.0,
    )
    with patch(
        "tests.test_extraction_graph_accuracy.try_load_cached_dependency_graph",
        return_value=cached,
    ) as try_load:
        loaded = _load_workbook_graph_for_audit(config)

    assert loaded is graph
    try_load.assert_called_once()
    kwargs = try_load.call_args.kwargs
    assert kwargs["cache_dir"] is COMMITTED_GRAPH_CACHE_DIR
    assert kwargs["workbook_path"] == config.workbook_path
    assert kwargs["targets"] == config.targets
    assert kwargs["constraints"] == config.constraints
    assert kwargs["load_values"] is True
    assert kwargs["capture_dependency_provenance"] is True


def test_workbook_audit_skips_on_committed_cache_miss() -> None:
    config = load_pipeline_config()
    with (
        patch(
            "tests.test_extraction_graph_accuracy.try_load_cached_dependency_graph",
            return_value=None,
        ) as try_load,
        patch("src.graph_cache.create_dependency_graph") as create,
        patch("src.graph_cache.save_dependency_graph") as save,
        pytest.raises(pytest.skip.Exception, match="warm committed"),
    ):
        _load_workbook_graph_for_audit(config)

    try_load.assert_called_once()
    create.assert_not_called()
    save.assert_not_called()
