"""Internal series binding resolution helpers for formula-cell triangulation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey

from src.refactor_bindings import BindingKeyValue, _coerce_binding_keys

InternalBindingCell = Mapping[str, Any]
InternalBindingIndex = dict[str, InternalBindingCell]


@dataclass(frozen=True)
class InternalBindingEntry:
    address: str
    key: dict[str, BindingKeyValue]
    record: dict[str, BindingKeyValue]


def internal_series_cell_keys(
    internal_series: Sequence[Mapping[str, Any]],
) -> set[NodeKey]:
    keys: set[NodeKey] = set()
    for series in internal_series:
        for cell in series.get("cells", []):
            address = cell.get("address")
            if isinstance(address, str):
                keys.add(address)
    return keys


def build_internal_binding_index(
    internal_series: Sequence[Mapping[str, Any]],
) -> InternalBindingIndex:
    index: InternalBindingIndex = {}
    for series in internal_series:
        for cell in series.get("cells", []):
            address = cell.get("address")
            if not isinstance(address, str):
                continue
            key = cell.get("key")
            record = cell.get("record")
            index[address] = {
                "address": address,
                "key": _coerce_binding_keys(key if isinstance(key, Mapping) else {}),
                "record": _coerce_binding_keys(
                    record if isinstance(record, Mapping) else {}
                ),
            }
    return index


def internal_binding_for_address(
    index: Mapping[str, InternalBindingCell],
    address: str,
) -> InternalBindingEntry | None:
    cell = index.get(address)
    if cell is None:
        return None
    key = cell.get("key")
    record = cell.get("record")
    return InternalBindingEntry(
        address=address,
        key=_coerce_binding_keys(key if isinstance(key, Mapping) else {}),
        record=_coerce_binding_keys(record if isinstance(record, Mapping) else {}),
    )


def _format_binding_map_text(values: Mapping[str, BindingKeyValue]) -> str | None:
    if not values:
        return None
    parts = [f"{concept}={value!r}" for concept, value in sorted(values.items())]
    return ", ".join(parts)


def binding_node_labels(
    graph: DependencyGraph,
    internal_binding_index: Mapping[str, InternalBindingCell],
    *,
    keys: Iterable[NodeKey] | None = None,
    include_formula_on_nodes: bool = True,
    max_formula_length: int | None = 120,
) -> dict[NodeKey, str]:
    """Format internal binding key/record data as concise graph explorer labels."""
    node_labels: dict[NodeKey, str] = {}
    for key in keys or graph.keys(order="workbook"):
        binding = internal_binding_for_address(internal_binding_index, key)
        if binding is None:
            continue
        key_text = _format_binding_map_text(binding.key)
        record_text = _format_binding_map_text(binding.record)
        if key_text is None and record_text is None:
            continue

        node = graph.get_node(key)
        parts = [key]
        if include_formula_on_nodes and node is not None and node.formula:
            formula = node.formula
            if max_formula_length is not None and len(formula) > max_formula_length:
                formula = f"{formula[:max_formula_length]}..."
            parts.append(formula)
        if key_text is not None:
            parts.append(f"keys: {key_text}")
        if record_text is not None:
            parts.append(f"record: {record_text}")
        node_labels[key] = "\n".join(parts)
    return node_labels
