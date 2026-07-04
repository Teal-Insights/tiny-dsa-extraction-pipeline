from __future__ import annotations

from excel_grapher.exporter import OptimalCompression, ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph


def build_refactor_projection(graph: DependencyGraph) -> ProjectionResult:
    """Build the provenance-aware projection used for refactor-oriented exports."""
    return OptimalCompression().project(graph)
