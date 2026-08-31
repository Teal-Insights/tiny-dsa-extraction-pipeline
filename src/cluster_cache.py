"""Disk cache for formula clusters and the refactor schedule."""

from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import time
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import cast

from src.formula_clustering import (
    AddressToSeriesId,
    BoundAddressKeys,
    ClusterableGraph,
    FormulaCluster,
    cluster_graph_formulas,
)
from src.graph_cache import (
    bindings_fingerprint,
    prune_cache_entries_for_other_excel_grapher_versions,
)
from src.refactor_order import RefactorUnit, compute_refactor_schedule
from src.refactor_types import ClusteringMode, VariationMode

CLUSTERING_CACHE_SCHEMA_VERSION = "1.1.0"
DEFAULT_CLUSTER_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "clusters"
COMMITTED_CLUSTER_CACHE_DIR = DEFAULT_CLUSTER_CACHE_DIR


def _cluster_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_CLUSTER_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def cluster_cache_key(
    *,
    projection_cache_key: str,
    variation_mode: VariationMode | str,
    clustering_mode: ClusteringMode | str,
    bindings_fingerprint: str,
) -> str:
    payload = {
        "cache_schema_version": CLUSTERING_CACHE_SCHEMA_VERSION,
        "projection_cache_key": projection_cache_key,
        "variation_mode": variation_mode,
        "clustering_mode": clustering_mode,
        "bindings_fingerprint": bindings_fingerprint,
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_cluster_meta(
    meta_path: Path,
    *,
    cache_key: str,
    projection_cache_key: str,
    variation_mode: str,
    clustering_mode: str,
    cluster_count: int,
    schedule_unit_count: int,
) -> None:
    meta = {
        "cache_schema_version": CLUSTERING_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "projection_cache_key": projection_cache_key,
        "variation_mode": variation_mode,
        "clustering_mode": clustering_mode,
        "cluster_count": cluster_count,
        "schedule_unit_count": schedule_unit_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _is_clusters_payload(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    return all(isinstance(item, FormulaCluster) for item in value)


def _is_schedule_payload(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    return all(isinstance(item, RefactorUnit) for item in value)


def save_cluster_payload(
    clusters: tuple[FormulaCluster, ...],
    schedule: tuple[RefactorUnit, ...],
    *,
    cache_key: str,
    projection_cache_key: str,
    variation_mode: VariationMode | str,
    clustering_mode: ClusteringMode | str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _cluster_cache_dir(cache_dir)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(
            (clusters, schedule),
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    _write_cluster_meta(
        meta_path,
        cache_key=cache_key,
        projection_cache_key=projection_cache_key,
        variation_mode=str(variation_mode),
        clustering_mode=str(clustering_mode),
        cluster_count=len(clusters),
        schedule_unit_count=len(schedule),
    )


def load_cluster_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> tuple[tuple[FormulaCluster, ...], tuple[RefactorUnit, ...]] | None:
    payload_path, _meta_path = _cache_paths(_cluster_cache_dir(cache_dir), cache_key)
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, tuple) or len(payload) != 2:
        payload_path.unlink(missing_ok=True)
        return None
    clusters, schedule = payload
    if not (_is_clusters_payload(clusters) and _is_schedule_payload(schedule)):
        payload_path.unlink(missing_ok=True)
        return None
    return (
        cast(tuple[FormulaCluster, ...], clusters),
        cast(tuple[RefactorUnit, ...], schedule),
    )


@dataclass(frozen=True)
class ClusterCacheResult:
    clusters: tuple[FormulaCluster, ...]
    schedule: tuple[RefactorUnit, ...]
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_clusters_and_schedule(
    projection: ClusterableGraph,
    *,
    bound_address_keys: BoundAddressKeys | None,
    address_to_series_id: AddressToSeriesId | Mapping[str, str] | None,
    workbook_path: Path,
    bindings_path: Path,
    projection_cache_key: str,
    variation_mode: VariationMode | str = "independent",
    clustering_mode: ClusteringMode | str = "series_ast",
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> ClusterCacheResult:
    resolved_cache_dir = _cluster_cache_dir(cache_dir)
    fingerprint = bindings_fingerprint(bindings_path)
    cache_key = cluster_cache_key(
        projection_cache_key=projection_cache_key,
        variation_mode=variation_mode,
        clustering_mode=clustering_mode,
        bindings_fingerprint=fingerprint,
    )
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_cluster_payload(cache_key, cache_dir=resolved_cache_dir)
        if loaded is not None:
            clusters, schedule = loaded
            prune_cache_entries_for_other_excel_grapher_versions(
                cache_dir=resolved_cache_dir,
            )
            elapsed = time.perf_counter() - started
            print(f"cluster_schedule: cache hit ({elapsed:.1f}s, key={cache_key[:12]})")
            return ClusterCacheResult(
                clusters=clusters,
                schedule=schedule,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    clusters = cluster_graph_formulas(
        projection,
        bound_address_keys=bound_address_keys,
        variation_mode=cast(VariationMode, variation_mode),
        clustering_mode=cast(ClusteringMode, clustering_mode),
        address_to_series_id=(
            dict(address_to_series_id) if address_to_series_id is not None else None
        ),
        workbook_path=workbook_path,
    )
    schedule = compute_refactor_schedule(projection, clusters)
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_cluster_payload(
            clusters,
            schedule,
            cache_key=cache_key,
            projection_cache_key=projection_cache_key,
            variation_mode=variation_mode,
            clustering_mode=clustering_mode,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        prune_cache_entries_for_other_excel_grapher_versions(
            cache_dir=resolved_cache_dir,
        )
        print(
            "cluster_schedule: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, "
            f"key={cache_key[:12]})"
        )
    else:
        print(
            "cluster_schedule: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return ClusterCacheResult(
        clusters=clusters,
        schedule=schedule,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_cluster_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    cache_dir = _cluster_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()


def prune_stale_cluster_cache_entries(
    current_keys: set[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Delete cache files whose keys are not in ``current_keys``."""
    resolved_cache_dir = _cluster_cache_dir(cache_dir)
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
