"""Require every graph leaf and formula to sit in some series data_range.

``validate_series_bindings`` checks that each authored series is legal.
Codegen later fails on the first formula precedent that is not in any bound
series. This gate lists every uncovered leaf and formula before derivation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from excel_grapher.core.address_keys import parse_address
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.series_bindings.ranges import (
    apply_series_excludes,
    expand_data_range_for_graph,
    series_data_ranges,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

_PREVIEW_LIMIT = 12


class GraphBindingCoverageError(RuntimeError):
    """Raised when graph leaves or formulas fall outside every series data_range."""

    def __init__(self, unbound_cells: Sequence[str]) -> None:
        self.unbound_cells = tuple(unbound_cells)
        super().__init__(format_unbound_graph_cells(self.unbound_cells))


def format_unbound_graph_cells(unbound_cells: Sequence[str]) -> str:
    """Preview the first addresses and the sheets that hold the most holes."""
    ordered = tuple(unbound_cells)
    preview = ", ".join(ordered[:_PREVIEW_LIMIT])
    if len(ordered) > _PREVIEW_LIMIT:
        preview = f"{preview}, ... ({len(ordered) - _PREVIEW_LIMIT} more)"
    sheet_counts: Counter[str] = Counter(
        parse_address(address)[0] for address in ordered
    )
    busiest = ", ".join(
        f"{sheet} ({count})"
        for sheet, count in sorted(
            sheet_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    return (
        f"{len(ordered)} graph cell(s) are outside every series data_range: "
        f"{preview}. Busiest sheets: {busiest}."
    )


def graph_cells_requiring_coverage(graph: DependencyGraph) -> tuple[str, ...]:
    """Return workbook-ordered leaves and formula cells."""
    required = set(graph.leaf_keys()) | set(graph.formula_keys())
    return tuple(str(key) for key in graph if key in required)


def series_data_range_addresses(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    workbook: Path | str | None = None,
) -> set[str]:
    """Expand every series data_range, honoring excludes."""
    covered: set[str] = set()
    for series in bindings.get("series", []):
        if not isinstance(series, dict):
            continue
        try:
            expanded: list[str] = []
            for data_range in series_data_ranges(series):
                expanded.extend(
                    expand_data_range_for_graph(graph, data_range, workbook=workbook)
                )
            expanded = apply_series_excludes(expanded, series)
        except (ValueError, TypeError):
            continue
        covered.update(expanded)
    return covered


def unbound_graph_cells(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    workbook: Path | str | None = None,
) -> tuple[str, ...]:
    """Return leaves and formulas that no series data_range covers."""
    covered = series_data_range_addresses(graph, bindings, workbook=workbook)
    return tuple(
        address
        for address in graph_cells_requiring_coverage(graph)
        if address not in covered
    )


def require_graph_binding_coverage(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    workbook: Path | str | None = None,
) -> None:
    """Raise when any graph leaf or formula is outside every series data_range."""
    unbound = unbound_graph_cells(graph, bindings, workbook=workbook)
    if unbound:
        raise GraphBindingCoverageError(unbound)
