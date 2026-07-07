from __future__ import annotations

from excel_grapher.exporter import OptimalCompression, ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph

from src.projection_cache import get_or_build_refactor_projection


def build_refactor_projection(
    graph: DependencyGraph,
    *,
    graph_cache_key: str | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> ProjectionResult:
    """Build the provenance-aware projection used for refactor-oriented exports."""
    if graph_cache_key is None:
        return OptimalCompression().project(graph)
    return get_or_build_refactor_projection(
        graph,
        graph_cache_key=graph_cache_key,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    ).projection
