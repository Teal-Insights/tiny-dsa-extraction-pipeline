"""Coverage validation for internal formula-cell series bindings."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_internal_series,
    derive_output_series,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings
from fastpyxl.utils.cell import column_index_from_string, get_column_letter

from src.dependency_graph_viz import series_cell_keys
from src.internal_bindings import internal_series_cell_keys

_ADDRESS_RE = re.compile(r"^(?P<sheet>.+)!(?P<column>[A-Z]{1,3})(?P<row>\d+)$")

logger = logging.getLogger(__name__)

InternalBindingValidationMode = Literal["off", "warn", "error"]
InternalBindingValidationContext = Literal["pipeline", "pytest"]


class InternalBindingCoverageError(RuntimeError):
    """Raised when required internal formula cells lack binding coverage."""


@dataclass(frozen=True)
class InternalBindingCoverageReport:
    required_cell_count: int
    unbound_cells: tuple[NodeKey, ...]


def required_internal_formula_cells(
    graph: DependencyGraph,
    *,
    input_cells: Iterable[NodeKey],
    output_cells: Iterable[NodeKey],
) -> tuple[NodeKey, ...]:
    """Return formula nodes that are not already covered by public I/O bindings."""
    excluded_cells = set(input_cells) | set(output_cells)
    return tuple(
        sorted(
            address for address in graph.formula_keys() if address not in excluded_cells
        )
    )


def find_unbound_internal_formula_cells_from_manifest(
    *,
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    exempt_cells: frozenset[str],
    workbook: Path,
) -> tuple[NodeKey, ...]:
    """Resolve binding coverage from the manifest plus the cached graph."""
    input_series = derive_input_series(graph, bindings, workbook=workbook)
    output_series = derive_output_series(graph, bindings, workbook=workbook)
    internal_series = derive_internal_series(graph, bindings, workbook=workbook)
    return find_unbound_internal_formula_cells(
        graph=graph,
        internal_series=internal_series,
        input_cells=series_cell_keys(input_series),
        output_cells=series_cell_keys(output_series),
        exempt_cells=exempt_cells,
    )


def contiguous_column_ranges(columns: Sequence[int]) -> list[tuple[int, int]]:
    """Group sorted 1-based column indices into contiguous spans."""
    if not columns:
        return []
    ordered = sorted(columns)
    ranges: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for column in ordered[1:]:
        if column == prev + 1:
            prev = column
            continue
        ranges.append((start, prev))
        start = prev = column
    ranges.append((start, prev))
    return ranges


def group_unbound_cells_by_sheet_row(
    unbound_cells: Sequence[str],
) -> dict[str, dict[int, list[int]]]:
    """Bucket unbound sheet-qualified addresses by sheet and row."""
    grouped: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for address in unbound_cells:
        match = _ADDRESS_RE.match(address)
        if match is None:
            raise ValueError(f"Invalid workbook address: {address!r}")
        grouped[match["sheet"]][int(match["row"])].append(
            column_index_from_string(match["column"])
        )
    return {sheet: dict(rows) for sheet, rows in grouped.items()}


def format_row_column_spans(
    *,
    sheet: str,
    row: int,
    columns: Sequence[int],
) -> str:
    """Format one row's contiguous column spans as sheet-qualified ranges."""
    spans = ", ".join(
        f"{sheet}!{get_column_letter(start)}{row}"
        if start == end
        else f"{sheet}!{get_column_letter(start)}{row}:{get_column_letter(end)}{row}"
        for start, end in contiguous_column_ranges(columns)
    )
    return spans


def suggested_layout_for_row(columns: Sequence[int]) -> Literal["scalar", "row_series"]:
    """Suggest a binding layout from the number of unbound cells on one row."""
    if len(columns) <= 1:
        return "scalar"
    return "row_series"


def find_unbound_internal_formula_cells(
    *,
    graph: DependencyGraph,
    internal_series: Sequence[Mapping[str, Any]],
    input_cells: Iterable[NodeKey],
    output_cells: Iterable[NodeKey],
    exempt_cells: frozenset[str],
) -> tuple[NodeKey, ...]:
    bound_cells = internal_series_cell_keys(internal_series)
    unbound: list[NodeKey] = []
    for address in required_internal_formula_cells(
        graph,
        input_cells=input_cells,
        output_cells=output_cells,
    ):
        if address in exempt_cells or address in bound_cells:
            continue
        unbound.append(address)
    return tuple(unbound)


def _coverage_error_message(unbound_cells: Sequence[NodeKey]) -> str:
    preview = ", ".join(unbound_cells[:5])
    suffix = "..." if len(unbound_cells) > 5 else ""
    return (
        f"{len(unbound_cells)} internal formula cell(s) lack internal series bindings: "
        f"{preview}{suffix}. Add `internal: {{}}` series entries in "
        "bindings/internals.bindings.yaml or list reviewed addresses in "
        "INTERNAL_BINDING_EXEMPT_CELLS."
    )


def apply_internal_binding_coverage_report(
    report: InternalBindingCoverageReport | None,
    *,
    mode: InternalBindingValidationMode,
    context: InternalBindingValidationContext,
) -> InternalBindingCoverageReport | None:
    """Apply warn/error policy for a previously computed coverage report."""
    if mode == "off":
        return None
    if report is None or not report.unbound_cells:
        return report

    message = _coverage_error_message(report.unbound_cells)
    if mode == "warn" and context == "pipeline":
        logger.warning(message)
        return report
    raise InternalBindingCoverageError(message)


def enforce_internal_binding_coverage(
    *,
    graph: DependencyGraph,
    internal_series: Sequence[Mapping[str, Any]],
    input_cells: Iterable[NodeKey],
    output_cells: Iterable[NodeKey],
    exempt_cells: frozenset[str],
    mode: InternalBindingValidationMode,
    context: InternalBindingValidationContext,
) -> InternalBindingCoverageReport | None:
    if mode == "off":
        return None

    required_cells = required_internal_formula_cells(
        graph,
        input_cells=input_cells,
        output_cells=output_cells,
    )
    unbound_cells = find_unbound_internal_formula_cells(
        graph=graph,
        internal_series=internal_series,
        input_cells=input_cells,
        output_cells=output_cells,
        exempt_cells=exempt_cells,
    )
    report = InternalBindingCoverageReport(
        required_cell_count=len(required_cells),
        unbound_cells=unbound_cells,
    )
    return apply_internal_binding_coverage_report(
        report,
        mode=mode,
        context=context,
    )
