"""Coverage validation for internal formula-cell series bindings."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey

from src.internal_bindings import internal_series_cell_keys

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
    if not unbound_cells:
        return report

    message = _coverage_error_message(unbound_cells)
    if mode == "warn":
        if context == "pipeline":
            logger.warning(message)
            return report
        raise InternalBindingCoverageError(message)
    raise InternalBindingCoverageError(message)
