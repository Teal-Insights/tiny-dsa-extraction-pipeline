"""Disk cache for ``OptimalCompression`` projection payloads."""

from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import cast

from excel_grapher.exporter import (
    OptimalCompression,
    ProjectionManifest,
    ProjectionResult,
)
from excel_grapher.grapher.graph import DependencyGraph

PROJECTION_CACHE_SCHEMA_VERSION = "1.0.0"
PROJECTION_STRATEGY = "optimal_compression"
DEFAULT_PROJECTION_CACHE_DIR = (
    Path(__file__).resolve().parents[1] / ".cache" / "projection"
)


def _projection_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_PROJECTION_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def projection_cache_key(
    *, graph_cache_key: str, strategy: str = PROJECTION_STRATEGY
) -> str:
    payload = {
        "cache_schema_version": PROJECTION_CACHE_SCHEMA_VERSION,
        "graph_cache_key": graph_cache_key,
        "strategy": strategy,
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_projection_meta(
    meta_path: Path,
    *,
    cache_key: str,
    graph_cache_key: str,
    projected_node_count: int,
) -> None:
    meta = {
        "cache_schema_version": PROJECTION_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "graph_cache_key": graph_cache_key,
        "strategy": PROJECTION_STRATEGY,
        "projected_node_count": projected_node_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def save_projection_payload(
    projected_graph: DependencyGraph,
    manifest: object,
    *,
    cache_key: str,
    graph_cache_key: str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _projection_cache_dir(cache_dir)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(
            (projected_graph, manifest), handle, protocol=pickle.HIGHEST_PROTOCOL
        )
    _write_projection_meta(
        meta_path,
        cache_key=cache_key,
        graph_cache_key=graph_cache_key,
        projected_node_count=len(projected_graph),
    )


def load_projection_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> tuple[DependencyGraph, ProjectionManifest] | None:
    payload_path, _meta_path = _cache_paths(_projection_cache_dir(cache_dir), cache_key)
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
    projected_graph, manifest = payload
    if not isinstance(projected_graph, DependencyGraph):
        payload_path.unlink(missing_ok=True)
        return None
    return projected_graph, cast(ProjectionManifest, manifest)


def rehydrate_projection_result(
    *,
    original_graph: DependencyGraph,
    projected_graph: DependencyGraph,
    manifest: ProjectionManifest,
) -> ProjectionResult:
    return ProjectionResult(
        original_graph=original_graph,
        projected_graph=projected_graph,
        manifest=manifest,
    )


@dataclass(frozen=True)
class ProjectionCacheResult:
    projection: ProjectionResult
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_refactor_projection(
    graph: DependencyGraph,
    *,
    graph_cache_key: str,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> ProjectionCacheResult:
    resolved_cache_dir = _projection_cache_dir(cache_dir)
    cache_key = projection_cache_key(graph_cache_key=graph_cache_key)
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_projection_payload(cache_key, cache_dir=resolved_cache_dir)
        if loaded is not None:
            projected_graph, manifest = loaded
            projection = rehydrate_projection_result(
                original_graph=graph,
                projected_graph=projected_graph,
                manifest=manifest,
            )
            elapsed = time.perf_counter() - started
            print(
                f"optimal_compression: cache hit ({elapsed:.1f}s, key={cache_key[:12]})"
            )
            return ProjectionCacheResult(
                projection=projection,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    projection = OptimalCompression().project(graph)
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_projection_payload(
            projection.projected_graph,
            projection.manifest,
            cache_key=cache_key,
            graph_cache_key=graph_cache_key,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        print(
            "optimal_compression: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, key={cache_key[:12]})"
        )
    else:
        print(
            "optimal_compression: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return ProjectionCacheResult(
        projection=projection,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_projection_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    cache_dir = _projection_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()
