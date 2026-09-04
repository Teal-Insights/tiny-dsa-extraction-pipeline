"""Disk cache for derived leaf classification, binding indexes, and bound keys."""

from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from excel_grapher.grapher.graph import DependencyGraph

from src.graph_cache import (
    bindings_fingerprint,
    prune_cache_entries_for_other_excel_grapher_versions,
)
from src.internal_binding_coverage import (
    InternalBindingCoverageReport,
    InternalBindingValidationContext,
    InternalBindingValidationMode,
    apply_internal_binding_coverage_report,
    enforce_internal_binding_coverage,
)
from src.internal_bindings import (
    BindingKeyValue,
    InternalBindingIndex,
    build_address_to_series_id,
    build_bound_address_keys,
    build_internal_binding_index,
)

SERIES_DERIVED_SCHEMA_VERSION = "1.1.0"
DEFAULT_SERIES_DERIVED_CACHE_DIR = (
    Path(__file__).resolve().parents[1] / ".cache" / "series-derived"
)
COMMITTED_SERIES_DERIVED_CACHE_DIR = DEFAULT_SERIES_DERIVED_CACHE_DIR

SeriesResolutionList = Sequence[Mapping[str, Any]]
BoundAddressKeys = dict[str, dict[str, BindingKeyValue]]
AddressToSeriesId = dict[str, str]
LeafClassification = dict[str, str]


def _series_derived_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_SERIES_DERIVED_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def series_derived_cache_key(
    *,
    graph_cache_key: str,
    bindings_path: Path,
    validation_mode: InternalBindingValidationMode,
    exempt_cells: frozenset[str] | set[str] | Sequence[str],
) -> str:
    payload = {
        "cache_schema_version": SERIES_DERIVED_SCHEMA_VERSION,
        "graph_cache_key": graph_cache_key,
        "bindings_fingerprint": bindings_fingerprint(bindings_path),
        "excel_grapher_version": version("excel-grapher"),
        "internal_binding_validation_mode": validation_mode,
        "exempt_cells": sorted(exempt_cells),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_series_derived_meta(
    meta_path: Path,
    *,
    cache_key: str,
    graph_cache_key: str,
    leaf_count: int,
    bound_address_count: int,
    unbound_cell_count: int,
) -> None:
    meta = {
        "cache_schema_version": SERIES_DERIVED_SCHEMA_VERSION,
        "cache_key": cache_key,
        "graph_cache_key": graph_cache_key,
        "leaf_count": leaf_count,
        "bound_address_count": bound_address_count,
        "unbound_cell_count": unbound_cell_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _is_leaf_classification(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(key, str) and isinstance(kind, str) for key, kind in value.items()
    )


def _is_bound_address_keys(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for address, keys in value.items():
        if not isinstance(address, str) or not isinstance(keys, dict):
            return False
        if not all(isinstance(concept, str) for concept in keys):
            return False
    return True


def _is_address_to_series_id(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(address, str) and isinstance(series_id, str)
        for address, series_id in value.items()
    )


def _is_internal_binding_index(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(address, str) and isinstance(cell, Mapping)
        for address, cell in value.items()
    )


def _is_coverage_report(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, InternalBindingCoverageReport)


def save_series_derived_payload(
    *,
    leaf_classification: LeafClassification,
    internal_binding_index: InternalBindingIndex,
    bound_address_keys: BoundAddressKeys,
    address_to_series_id: AddressToSeriesId,
    coverage_report: InternalBindingCoverageReport | None,
    cache_key: str,
    graph_cache_key: str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _series_derived_cache_dir(cache_dir)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(
            (
                dict(leaf_classification),
                dict(internal_binding_index),
                dict(bound_address_keys),
                dict(address_to_series_id),
                coverage_report,
            ),
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    unbound_count = 0 if coverage_report is None else len(coverage_report.unbound_cells)
    _write_series_derived_meta(
        meta_path,
        cache_key=cache_key,
        graph_cache_key=graph_cache_key,
        leaf_count=len(leaf_classification),
        bound_address_count=len(bound_address_keys),
        unbound_cell_count=unbound_count,
    )


def load_series_derived_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> (
    tuple[
        LeafClassification,
        InternalBindingIndex,
        BoundAddressKeys,
        AddressToSeriesId,
        InternalBindingCoverageReport | None,
    ]
    | None
):
    payload_path, _meta_path = _cache_paths(
        _series_derived_cache_dir(cache_dir), cache_key
    )
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, tuple) or len(payload) != 5:
        payload_path.unlink(missing_ok=True)
        return None
    (
        leaf_classification,
        internal_binding_index,
        bound_address_keys,
        address_to_series_id,
        coverage_report,
    ) = payload
    if not (
        _is_leaf_classification(leaf_classification)
        and _is_internal_binding_index(internal_binding_index)
        and _is_bound_address_keys(bound_address_keys)
        and _is_address_to_series_id(address_to_series_id)
        and _is_coverage_report(coverage_report)
    ):
        payload_path.unlink(missing_ok=True)
        return None
    return (
        cast(LeafClassification, leaf_classification),
        cast(InternalBindingIndex, internal_binding_index),
        cast(BoundAddressKeys, bound_address_keys),
        cast(AddressToSeriesId, address_to_series_id),
        cast(InternalBindingCoverageReport | None, coverage_report),
    )


@dataclass(frozen=True)
class SeriesDerivedCacheResult:
    leaf_classification: LeafClassification
    internal_binding_index: InternalBindingIndex
    bound_address_keys: BoundAddressKeys
    address_to_series_id: AddressToSeriesId
    coverage_report: InternalBindingCoverageReport | None
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_series_derived(
    graph: DependencyGraph,
    *,
    constraints: Mapping[str, object],
    input_series: SeriesResolutionList,
    output_series: SeriesResolutionList,
    internal_series: SeriesResolutionList,
    constant_series: SeriesResolutionList,
    input_cells: Sequence[str] | set[str],
    output_cells: Sequence[str] | set[str],
    exempt_cells: frozenset[str],
    validation_mode: InternalBindingValidationMode,
    context: InternalBindingValidationContext,
    graph_cache_key: str,
    bindings_path: Path,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> SeriesDerivedCacheResult:
    from src.extraction_pipeline import classify_leaves_from_constraints

    resolved_cache_dir = _series_derived_cache_dir(cache_dir)
    cache_key = series_derived_cache_key(
        graph_cache_key=graph_cache_key,
        bindings_path=bindings_path,
        validation_mode=validation_mode,
        exempt_cells=exempt_cells,
    )
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_series_derived_payload(cache_key, cache_dir=resolved_cache_dir)
        if loaded is not None:
            (
                leaf_classification,
                internal_binding_index,
                bound_address_keys,
                address_to_series_id,
                coverage_report,
            ) = loaded
            apply_internal_binding_coverage_report(
                coverage_report,
                mode=validation_mode,
                context=context,
            )
            prune_cache_entries_for_other_excel_grapher_versions(
                cache_dir=resolved_cache_dir,
            )
            elapsed = time.perf_counter() - started
            print(f"series_derived: cache hit ({elapsed:.1f}s, key={cache_key[:12]})")
            return SeriesDerivedCacheResult(
                leaf_classification=leaf_classification,
                internal_binding_index=internal_binding_index,
                bound_address_keys=bound_address_keys,
                address_to_series_id=address_to_series_id,
                coverage_report=coverage_report,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    leaf_classification = classify_leaves_from_constraints(
        constraints, graph.leaf_keys()
    )
    coverage_report = enforce_internal_binding_coverage(
        graph=graph,
        internal_series=internal_series,
        input_cells=input_cells,
        output_cells=output_cells,
        exempt_cells=exempt_cells,
        mode=validation_mode,
        context=context,
    )
    internal_binding_index = build_internal_binding_index(internal_series)
    bound_address_keys = build_bound_address_keys(
        input_series,
        output_series,
        internal_series,
        constant_series=constant_series,
    )
    address_to_series_id = build_address_to_series_id(
        internal_series,
        output_series=output_series,
        input_series=input_series,
        constant_series=constant_series,
    )
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_series_derived_payload(
            leaf_classification=leaf_classification,
            internal_binding_index=internal_binding_index,
            bound_address_keys=bound_address_keys,
            address_to_series_id=address_to_series_id,
            coverage_report=coverage_report,
            cache_key=cache_key,
            graph_cache_key=graph_cache_key,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        prune_cache_entries_for_other_excel_grapher_versions(
            cache_dir=resolved_cache_dir,
        )
        print(
            "series_derived: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, key={cache_key[:12]})"
        )
    else:
        print(
            "series_derived: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return SeriesDerivedCacheResult(
        leaf_classification=leaf_classification,
        internal_binding_index=internal_binding_index,
        bound_address_keys=bound_address_keys,
        address_to_series_id=address_to_series_id,
        coverage_report=coverage_report,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def require_bound_address_keys_from_series_derived_cache(
    *,
    graph_cache_key: str,
    bindings_path: Path,
    validation_mode: InternalBindingValidationMode,
    exempt_cells: frozenset[str],
    cache_dir: Path | None = None,
) -> BoundAddressKeys:
    """Load bound address keys from disk or fail loudly when the entry is absent."""
    resolved_cache_dir = _series_derived_cache_dir(cache_dir)
    cache_key = series_derived_cache_key(
        graph_cache_key=graph_cache_key,
        bindings_path=bindings_path,
        validation_mode=validation_mode,
        exempt_cells=exempt_cells,
    )
    loaded = load_series_derived_payload(cache_key, cache_dir=resolved_cache_dir)
    if loaded is None:
        payload_path = resolved_cache_dir / f"{cache_key}.pkl.gz"
        raise RuntimeError(
            "series-derived cache miss for bound_address_keys at "
            f"{payload_path}; run extract/export first or pass "
            "bound_address_keys explicitly"
        )
    return loaded[2]


def clear_series_derived_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    cache_dir = _series_derived_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()


def prune_stale_series_derived_cache_entries(
    current_keys: set[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Delete cache files whose keys are not in ``current_keys``."""
    resolved_cache_dir = _series_derived_cache_dir(cache_dir)
    if not resolved_cache_dir.is_dir():
        return []
    pruned: list[str] = []
    for path in sorted(resolved_cache_dir.iterdir()):
        if not path.is_file():
            continue
        cache_key = path.name.split(".", 1)[0]
        if cache_key in current_keys:
            continue
        path.unlink()
        pruned.append(path.name)
    return pruned
