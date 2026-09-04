"""Internal series binding resolution helpers for formula-cell triangulation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey
from excel_grapher.series_bindings.types import Scalar

BindingKeyValue = str | int | float | bool


def _coerce_binding_keys(keys: Mapping[str, Scalar]) -> dict[str, BindingKeyValue]:
    coerced: dict[str, BindingKeyValue] = {}
    for key, value in keys.items():
        if isinstance(value, (bool, str, int, float)):
            coerced[key] = value
    return coerced


def build_bound_address_keys(
    input_series: Sequence[Mapping[str, Any]],
    output_series: Sequence[Mapping[str, Any]],
    internal_series: Sequence[Mapping[str, Any]] = (),
    *,
    constant_series: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, BindingKeyValue]]:
    """Index every bound cell address to its coerced cell-scope binding keys.

    Constant series are folded in first (lowest priority) so a keyed reader-only
    leaf still contributes its per-cell keys. Input/output/internal series
    overwrite the constant baseline for any shared address so formula-bearing
    ownership keeps precedence.
    """
    index: dict[str, dict[str, BindingKeyValue]] = {}
    for series_list in (constant_series, input_series, output_series, internal_series):
        for series in series_list:
            for cell in series["cells"]:
                index[str(cell["address"])] = _coerce_binding_keys(cell["key"])
    return index


def series_cell_owners(
    series_list: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """Map each series cell address to the series ids that claim it."""
    owners: dict[str, list[str]] = {}
    for series in series_list:
        series_id = series.get("id")
        if not isinstance(series_id, str) or not series_id:
            continue
        for cell in series.get("cells", []):
            address = cell.get("address")
            if isinstance(address, str):
                owners.setdefault(address, []).append(series_id)
    return {address: tuple(series_ids) for address, series_ids in owners.items()}


def _unique_series_id_by_address(
    series_list: Sequence[Mapping[str, Any]],
    *,
    ownership_kind: str,
) -> dict[str, str]:
    owners_by_address = series_cell_owners(series_list)
    duplicates = {
        address: series_ids
        for address, series_ids in owners_by_address.items()
        if len(series_ids) > 1
    }
    if duplicates:
        sample_address, sample_series_ids = min(duplicates.items())
        raise ValueError(
            f"{ownership_kind} series cell address must map to exactly one series_id; "
            f"got {sample_address!r} in {list(sample_series_ids)}"
            + (
                f" and {len(duplicates) - 1} more duplicate address(es)"
                if len(duplicates) > 1
                else ""
            )
        )
    return {
        address: series_ids[0]
        for address, series_ids in owners_by_address.items()
        if len(series_ids) == 1
    }


def build_address_to_series_id(
    internal_series: Sequence[Mapping[str, Any]],
    *,
    output_series: Sequence[Mapping[str, Any]] = (),
    input_series: Sequence[Mapping[str, Any]] = (),
    constant_series: Sequence[Mapping[str, Any]] = (),
) -> dict[str, str]:
    """Map cell addresses to owning series ids.

    Internal ownership wins. Addresses without an internal owner fall back to
    constant (reader-only leaf), then public output, then input binding series
    ids.
    """
    address_to_series_id = _unique_series_id_by_address(
        internal_series,
        ownership_kind="internal",
    )
    for ownership_kind, series_list in (
        ("constant", constant_series),
        ("output", output_series),
        ("input", input_series),
    ):
        public_ids = _unique_series_id_by_address(
            series_list,
            ownership_kind=ownership_kind,
        )
        for address, series_id in public_ids.items():
            address_to_series_id.setdefault(address, series_id)
    return address_to_series_id


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
        if include_formula_on_nodes and node is not None:
            formula = node.formula or node.normalized_formula
            if formula is not None:
                if max_formula_length is not None and len(formula) > max_formula_length:
                    formula = f"{formula[:max_formula_length]}..."
                parts.append(formula)
        if key_text is not None:
            parts.append(f"keys: {key_text}")
        if record_text is not None:
            parts.append(f"record: {record_text}")
        node_labels[key] = "\n".join(parts)
    return node_labels
