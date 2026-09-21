"""Compile series-binding domains into dynamic-ref config and leaf annotations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import fastpyxl
from excel_grapher.core.address_keys import parse_address
from excel_grapher.core.cell_types import Between, RealBetween
from excel_grapher.grapher import DynamicRefConfig
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.domains import compile_domain_spec
from excel_grapher.series_bindings.ranges import expand_bound_series_addresses
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.pipeline_config import PipelineConfig

_RuntimeLiteral: Any = cast(Any, Literal)


def load_pipeline_bindings(config: PipelineConfig) -> WorkbookSeriesBindings:
    """Load the merged binding manifest for ``config.bindings_path``."""
    return load_series_bindings(config.bindings_path)


def pipeline_dynamic_ref_config(
    config: PipelineConfig,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> DynamicRefConfig:
    """Build ``DynamicRefConfig`` from sidecar domains, overlaying ``CONSTRAINTS``.

    Production catalogs cover every OFFSET / INDEX / INDIRECT leaf, so the
    overlay is empty. Tests may still pass a Python table on
    ``PipelineConfig.constraints``; those keys win on overlap.
    """
    loaded = (
        bindings if bindings is not None else load_series_bindings(config.bindings_path)
    )
    derived = DynamicRefConfig.from_bindings(
        loaded,
        config.workbook_path,
        bindings_path=config.bindings_path,
    )
    if not config.constraints:
        return derived
    overlay = DynamicRefConfig.from_constraints(config.constraints, {})
    merged, _overrides = derived.overlay(overlay)
    return merged


def domain_annotations_from_bindings(
    bindings: Mapping[str, Any],
    *,
    workbook: Path,
) -> dict[str, object]:
    """Return typing annotations equivalent to compiled series domains.

    ``enum`` / ``between`` / ``real_between`` become ``Literal`` / ``Annotated``
    objects. ``constant`` (implied ``from_workbook``) pins become singleton
    ``Literal`` values read from the workbook cache.
    """
    annotations: dict[str, object] = {}
    from_workbook_addresses: list[str] = []
    series_list = bindings.get("series", ())
    if not isinstance(series_list, list):
        return annotations
    for series in series_list:
        if not isinstance(series, Mapping):
            continue
        spec = compile_domain_spec(series)
        if spec is None:
            continue
        addresses = list(expand_bound_series_addresses(series, workbook=workbook))
        if spec.get("from_workbook") is True:
            from_workbook_addresses.extend(addresses)
            continue
        annotation = _annotation_from_domain_spec(spec)
        if annotation is None:
            continue
        for address in addresses:
            annotations[address] = annotation

    if from_workbook_addresses:
        workbook_values = fastpyxl.load_workbook(
            workbook, data_only=True, read_only=True
        )
        try:
            for address in from_workbook_addresses:
                sheet, coord = parse_address(address)
                annotations[address] = _RuntimeLiteral[
                    tuple([workbook_values[sheet][coord].value])
                ]
        finally:
            workbook_values.close()
    return annotations


def effective_domain_annotations(
    config: PipelineConfig,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Sidecar domain annotations with optional ``CONSTRAINTS`` overlay."""
    loaded = (
        bindings if bindings is not None else load_series_bindings(config.bindings_path)
    )
    annotations = domain_annotations_from_bindings(
        loaded, workbook=config.workbook_path
    )
    annotations.update(config.constraints)
    return annotations


def _annotation_from_domain_spec(spec: Mapping[str, Any]) -> object | None:
    if "enum" in spec:
        return _RuntimeLiteral[tuple(spec["enum"])]
    if "between" in spec:
        bounds = spec["between"]
        if not isinstance(bounds, Mapping):
            raise TypeError(f"domain.between must be a mapping; got {bounds!r}")
        return Annotated[int, Between(bounds.get("min"), bounds.get("max"))]
    if "real_between" in spec:
        bounds = spec["real_between"]
        if not isinstance(bounds, Mapping):
            raise TypeError(f"domain.real_between must be a mapping; got {bounds!r}")
        return Annotated[float, RealBetween(bounds.get("min"), bounds.get("max"))]
    return None
