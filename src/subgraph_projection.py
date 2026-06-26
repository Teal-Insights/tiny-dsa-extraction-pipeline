from __future__ import annotations

from excel_grapher.exporter import OptimalCompression, ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph


def build_tiny_dsa_refactor_projection(graph: DependencyGraph) -> ProjectionResult:
    """Build the Tiny DSA projection used for refactor-oriented exports."""
    return OptimalCompression().project(graph)
