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
from excel_grapher.series_bindings.normalize import (
    effective_validation,
    has_constant_direction,
    has_input_direction,
    has_internal_direction,
    has_output_direction,
    input_mode,
)
from excel_grapher.series_bindings.ranges import (
    apply_series_excludes,
    expand_data_range_for_graph,
    series_data_ranges,
)
from excel_grapher.series_bindings.resolve import (
    BindingDirection,
    _select_addresses,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings
from fastpyxl.utils.cell import column_index_from_string, get_column_letter

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


def _series_supports_direction(
    series: Mapping[str, Any],
    direction: BindingDirection,
) -> bool:
    series_dict = dict(series)
    if direction == "input":
        return has_input_direction(series_dict)
    if direction == "internal":
        return has_internal_direction(series_dict)
    if direction == "constant":
        return has_constant_direction(series_dict)
    return has_output_direction(series_dict)


def manifest_binding_addresses(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    direction: BindingDirection,
) -> set[NodeKey]:
    """Expand binding manifests to cell addresses using graph metadata only.

    Mirrors excel-grapher's pre-resolution address selection so coverage can
    fail fast before workbook value reads during series derivation.
    """
    addresses: set[NodeKey] = set()
    for series in bindings.get("series", []):
        if not isinstance(series, dict) or not _series_supports_direction(
            series, direction
        ):
            continue
        try:
            expanded: list[str] = []
            for data_range in series_data_ranges(series):
                expanded.extend(
                    expand_data_range_for_graph(graph, data_range, workbook=None)
                )
            expanded = apply_series_excludes(expanded, series)
        except (ValueError, TypeError):
            continue
        if not expanded:
            continue
        selected, _issues = _select_addresses(
            graph,
            expanded,
            direction=direction,
            validation=effective_validation(series),
            series_id=str(series.get("id", "")),
            input_binding_mode=input_mode(series) if direction == "input" else "leaf",
        )
        addresses.update(selected)
    return addresses


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


def normalize_sheet_name(sheet: str) -> str:
    """Strip Excel-style quoting from a sheet name captured out of an address."""
    if len(sheet) >= 2 and sheet[0] == "'" and sheet[-1] == "'":
        return sheet[1:-1].replace("''", "'")
    return sheet


def group_unbound_cells_by_sheet_row(
    unbound_cells: Sequence[str],
) -> dict[str, dict[int, list[int]]]:
    """Bucket unbound sheet-qualified addresses by sheet and row."""
    grouped: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for address in unbound_cells:
        match = _ADDRESS_RE.match(address)
        if match is None:
            raise ValueError(f"Invalid workbook address: {address!r}")
        grouped[normalize_sheet_name(match["sheet"])][int(match["row"])].append(
            column_index_from_string(match["column"])
        )
    return {sheet: dict(rows) for sheet, rows in grouped.items()}


def collapse_unbound_cells_to_ranges(unbound_cells: Sequence[str]) -> tuple[str, ...]:
    """Collapse addresses into maximal sheet-qualified A1 rectangles.

    Consecutive rows that share the same column-span signature become one
    rectangle per span (``Sheet!B2:C10``). Gaps in rows or columns split.
    """
    grouped = group_unbound_cells_by_sheet_row(unbound_cells)
    ranges: list[str] = []
    for sheet, rows in sorted(grouped.items()):
        signatures: list[tuple[int, tuple[tuple[int, int], ...]]] = []
        for row in sorted(rows):
            spans = tuple(contiguous_column_ranges(rows[row]))
            signatures.append((row, spans))
        index = 0
        while index < len(signatures):
            start_row, spans = signatures[index]
            end_row = start_row
            cursor = index + 1
            while (
                cursor < len(signatures)
                and signatures[cursor][0] == end_row + 1
                and signatures[cursor][1] == spans
            ):
                end_row = signatures[cursor][0]
                cursor += 1
            for column_start, column_end in spans:
                start_letter = get_column_letter(column_start)
                end_letter = get_column_letter(column_end)
                ranges.append(
                    f"{sheet}!{start_letter}{start_row}:{end_letter}{end_row}"
                    if (column_start, start_row) != (column_end, end_row)
                    else f"{sheet}!{start_letter}{start_row}"
                )
            index = cursor
    return tuple(ranges)


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


def find_unbound_internal_formula_cells_from_manifest(
    *,
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    exempt_cells: frozenset[str],
    workbook: Path | None = None,
) -> tuple[NodeKey, ...]:
    _ = workbook  # coverage uses the graph only; workbook kept for call-site compat
    input_cells = manifest_binding_addresses(graph, bindings, direction="input")
    output_cells = manifest_binding_addresses(graph, bindings, direction="output")
    internal_cells = manifest_binding_addresses(graph, bindings, direction="internal")
    unbound: list[NodeKey] = []
    for address in required_internal_formula_cells(
        graph,
        input_cells=input_cells,
        output_cells=output_cells,
    ):
        if address in exempt_cells or address in internal_cells:
            continue
        unbound.append(address)
    return tuple(unbound)


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


def enforce_internal_binding_coverage_from_manifest(
    *,
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    exempt_cells: frozenset[str],
    mode: InternalBindingValidationMode,
    context: InternalBindingValidationContext,
) -> InternalBindingCoverageReport | None:
    if mode == "off":
        return None

    input_cells = manifest_binding_addresses(graph, bindings, direction="input")
    output_cells = manifest_binding_addresses(graph, bindings, direction="output")
    required_cells = required_internal_formula_cells(
        graph,
        input_cells=input_cells,
        output_cells=output_cells,
    )
    unbound_cells = find_unbound_internal_formula_cells_from_manifest(
        graph=graph,
        bindings=bindings,
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
