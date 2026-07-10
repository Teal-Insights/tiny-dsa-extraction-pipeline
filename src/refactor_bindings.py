"""Binding key vocabulary and address-key resolution for internals refactor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fastpyxl
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.types import Scalar

from src.workbook_addresses import ProjectionColumnLayout

BindingKeyValue = str | int | float | bool


@dataclass(frozen=True)
class KeyConceptSpec:
    concept: str
    dtype: str
    suggested_param_name: str


def concept_to_param_name(concept: str) -> str:
    return concept.lower()


def load_key_concept_vocabulary(bindings_path: Path) -> tuple[KeyConceptSpec, ...]:
    """Aggregate cell-scoped key dimensions from ``bindings/*.bindings.yaml``."""
    bindings = load_series_bindings(bindings_path)
    concept_scheme = bindings.get("concept_scheme") or {}
    concept_dtypes: dict[str, str] = {}
    for concept in concept_scheme.get("concepts") or []:
        if not isinstance(concept, dict):
            continue
        concept_id = concept.get("id")
        dtype = concept.get("dtype")
        if isinstance(concept_id, str) and dtype is not None:
            concept_dtypes[concept_id] = str(dtype)

    seen: dict[str, KeyConceptSpec] = {}
    for series in bindings.get("series") or []:
        if not isinstance(series, dict):
            continue
        structure = series.get("structure") or {}
        for dimension in structure.get("dimensions") or []:
            if not isinstance(dimension, dict):
                continue
            if dimension.get("role") != "key":
                continue
            if dimension.get("scope") == "series":
                continue
            concept = dimension.get("concept")
            if not isinstance(concept, str) or concept in seen:
                continue
            dtype = concept_dtypes.get(concept) or dimension.get("dtype") or "string"
            seen[concept] = KeyConceptSpec(
                concept=concept,
                dtype=str(dtype),
                suggested_param_name=concept_to_param_name(concept),
            )
    return tuple(sorted(seen.values(), key=lambda item: item.concept))


def build_bound_address_keys(
    input_series: Sequence[Mapping[str, Any]],
    output_series: Sequence[Mapping[str, Any]],
    internal_series: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, BindingKeyValue]]:
    index: dict[str, dict[str, BindingKeyValue]] = {}
    for series_list in (input_series, output_series, internal_series):
        for series in series_list:
            for cell in series["cells"]:
                index[str(cell["address"])] = _coerce_binding_keys(cell["key"])
    return index


def _read_engine_time_period(
    column: str,
    workbook_path: Path,
    *,
    layout: ProjectionColumnLayout,
) -> int | None:
    keep_vba = workbook_path.suffix.lower() == ".xlsm"
    workbook = fastpyxl.load_workbook(
        workbook_path,
        data_only=True,
        read_only=True,
        keep_vba=keep_vba,
    )
    value = workbook[layout.engine_sheet][
        f"{column}{layout.time_period_header_row}"
    ].value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def expected_keys_for_address(
    address: str,
    *,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    workbook_path: Path,
    layout: ProjectionColumnLayout | None = None,
) -> dict[str, BindingKeyValue]:
    bound = bound_address_keys.get(address)
    if bound is not None:
        return _coerce_binding_keys(bound)
    if layout is None:
        return {}
    column = layout.logical_engine_column(address)
    if column is None:
        return {}
    time_period = _read_engine_time_period(column, workbook_path, layout=layout)
    if time_period is None:
        return {}
    return {"TIME_PERIOD": time_period}


def _coerce_binding_keys(keys: Mapping[str, Scalar]) -> dict[str, BindingKeyValue]:
    coerced: dict[str, BindingKeyValue] = {}
    for concept, value in keys.items():
        if isinstance(value, bool):
            coerced[concept] = value
        elif isinstance(value, (str, int, float)):
            coerced[concept] = value
    return coerced


def varying_key_concepts(
    addresses: Sequence[str],
    *,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    workbook_path: Path,
    layout: ProjectionColumnLayout | None = None,
) -> frozenset[str]:
    keys_by_address = [
        expected_keys_for_address(
            address,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        for address in addresses
    ]
    concepts = {concept for keys in keys_by_address for concept in keys}
    return frozenset(
        concept
        for concept in concepts
        if len({keys.get(concept) for keys in keys_by_address}) > 1
    )


def expected_member_keys_for_cluster(
    addresses: Sequence[str],
    *,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    workbook_path: Path,
    layout: ProjectionColumnLayout | None = None,
) -> dict[str, dict[str, BindingKeyValue]]:
    varying = varying_key_concepts(
        addresses,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
    )
    return {
        address: {
            concept: expected_keys_for_address(
                address,
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
            )[concept]
            for concept in varying
        }
        for address in addresses
    }


def engine_column_from_member_keys(
    keys: Mapping[str, BindingKeyValue],
    *,
    address: str,
    layout: ProjectionColumnLayout | None = None,
) -> str | None:
    time_period = keys.get("TIME_PERIOD")
    if isinstance(time_period, int) and layout is not None:
        column = layout.time_period_to_engine_column.get(time_period)
        if column is not None:
            return column
    if layout is None:
        return None
    return layout.logical_engine_column(address)


def format_binding_key_literal(value: BindingKeyValue) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def render_literal_helper_call(
    helper_name: str,
    parameters: Sequence[tuple[str, str]],
    keys: Mapping[str, BindingKeyValue],
) -> str:
    """Render ``helper(ctx, param=literal, ...)`` for one collapsed cell address."""
    kwargs = ", ".join(
        f"{name}={format_binding_key_literal(keys[concept])}"
        for name, concept in parameters
    )
    return f"{helper_name}(ctx, {kwargs})"


def helper_parameters_for_varying_keys(
    varying_concepts: frozenset[str],
    vocabulary: Sequence[KeyConceptSpec],
) -> tuple[KeyConceptSpec, ...]:
    by_concept = {item.concept: item for item in vocabulary}
    missing = sorted(
        concept for concept in varying_concepts if concept not in by_concept
    )
    if missing:
        raise ValueError(f"unknown varying key concepts: {missing}")
    return tuple(by_concept[concept] for concept in sorted(varying_concepts))
