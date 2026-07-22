"""Disk cache for canonical dependency graphs built by ``create_dependency_graph``."""

from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
    create_dependency_graph,
)

GRAPH_CACHE_SCHEMA_VERSION = "1.0.0"
DEFAULT_GRAPH_CACHE_DIR = (
    Path(__file__).resolve().parents[1] / ".cache" / "dependency-graph"
)
COMMITTED_GRAPH_CACHE_DIR = DEFAULT_GRAPH_CACHE_DIR

# Same-process reuse of loaded graphs keyed by (cache_dir, cache_key). Callers
# that mutate the returned DependencyGraph (e.g. leaf_classification assignment
# or projection) share those mutations with later process-cache hits for the
# same key; treat the object as owned by the pipeline run, not as an immutable
# snapshot.
_PROCESS_GRAPH_CACHE: dict[tuple[str, str], DependencyGraph] = {}


def _graph_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_GRAPH_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bindings_fingerprint(bindings_path: Path) -> str:
    """Hash binding YAML files under ``bindings_path`` in stable sorted order."""
    resolved = bindings_path.resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"Bindings path is not a directory: {resolved}")
    binding_files = sorted(resolved.glob("*.bindings.yaml"))
    if not binding_files:
        raise FileNotFoundError(
            f"No *.bindings.yaml files found under bindings path: {resolved}"
        )
    digest = hashlib.sha256()
    for binding_file in binding_files:
        digest.update(binding_file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(binding_file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def dependency_graph_cache_key(
    *,
    workbook_path: Path,
    targets: Sequence[str],
    constraints: Mapping[str, object],
    bindings_path: Path,
    load_values: bool,
    capture_dependency_provenance: bool,
) -> str:
    payload = {
        "cache_schema_version": GRAPH_CACHE_SCHEMA_VERSION,
        "workbook_fingerprint": file_fingerprint(workbook_path),
        "bindings_fingerprint": bindings_fingerprint(bindings_path),
        "targets": sorted(targets),
        "constraints": dict(constraints),
        "load_values": load_values,
        "capture_dependency_provenance": capture_dependency_provenance,
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_graph_meta(
    meta_path: Path,
    *,
    cache_key: str,
    workbook_path: Path,
    targets: Sequence[str],
    node_count: int,
) -> None:
    meta = {
        "cache_schema_version": GRAPH_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "workbook_path": str(workbook_path),
        "target_count": len(targets),
        "node_count": node_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def save_dependency_graph(
    graph: DependencyGraph,
    *,
    cache_key: str,
    workbook_path: Path,
    targets: Sequence[str],
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _graph_cache_dir(cache_dir)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(graph, handle, protocol=pickle.HIGHEST_PROTOCOL)
    _write_graph_meta(
        meta_path,
        cache_key=cache_key,
        workbook_path=workbook_path,
        targets=targets,
        node_count=len(graph),
    )


def load_dependency_graph(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> DependencyGraph | None:
    payload_path, _meta_path = _cache_paths(_graph_cache_dir(cache_dir), cache_key)
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            graph = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not isinstance(graph, DependencyGraph):
        payload_path.unlink(missing_ok=True)
        return None
    return graph


@dataclass(frozen=True)
class DependencyGraphCacheResult:
    graph: DependencyGraph
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def _process_cache_slot(cache_dir: Path, cache_key: str) -> tuple[str, str]:
    return (str(cache_dir.resolve()), cache_key)


def get_or_build_dependency_graph(
    *,
    workbook_path: Path,
    targets: Sequence[str],
    constraints: Mapping[str, object],
    bindings_path: Path,
    dynamic_refs: DynamicRefConfig,
    load_values: bool = True,
    capture_dependency_provenance: bool = True,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    build: Callable[[], DependencyGraph] | None = None,
) -> DependencyGraphCacheResult:
    resolved_cache_dir = _graph_cache_dir(cache_dir)
    cache_key = dependency_graph_cache_key(
        workbook_path=workbook_path,
        targets=targets,
        constraints=constraints,
        bindings_path=bindings_path,
        load_values=load_values,
        capture_dependency_provenance=capture_dependency_provenance,
    )
    process_slot = _process_cache_slot(resolved_cache_dir, cache_key)
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        remembered = _PROCESS_GRAPH_CACHE.get(process_slot)
        if remembered is not None:
            elapsed = time.perf_counter() - started
            print(
                "create_dependency_graph: process cache hit "
                f"({elapsed:.1f}s, key={cache_key[:12]})"
            )
            return DependencyGraphCacheResult(
                graph=remembered,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )
        cached = load_dependency_graph(cache_key, cache_dir=resolved_cache_dir)
        if cached is not None:
            _PROCESS_GRAPH_CACHE[process_slot] = cached
            elapsed = time.perf_counter() - started
            print(
                f"create_dependency_graph: cache hit ({elapsed:.1f}s, key={cache_key[:12]})"
            )
            return DependencyGraphCacheResult(
                graph=cached,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    if build is None:
        graph = create_dependency_graph(
            workbook_path,
            list(targets),
            load_values=load_values,
            dynamic_refs=dynamic_refs,
            capture_dependency_provenance=capture_dependency_provenance,
        )
    else:
        graph = build()
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_dependency_graph(
            graph,
            cache_key=cache_key,
            workbook_path=workbook_path,
            targets=targets,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        _PROCESS_GRAPH_CACHE[process_slot] = graph
        print(
            "create_dependency_graph: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, key={cache_key[:12]})"
        )
    else:
        _PROCESS_GRAPH_CACHE.pop(process_slot, None)
        print(
            "create_dependency_graph: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return DependencyGraphCacheResult(
        graph=graph,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_process_dependency_graph_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    """Drop in-process graph reuse entries.

    When ``cache_dir`` is set, only entries for that directory are removed;
    otherwise the entire process cache is cleared.
    """
    if cache_dir is None:
        _PROCESS_GRAPH_CACHE.clear()
        return
    prefix = str(_graph_cache_dir(cache_dir).resolve())
    for slot in [slot for slot in _PROCESS_GRAPH_CACHE if slot[0] == prefix]:
        del _PROCESS_GRAPH_CACHE[slot]


def clear_dependency_graph_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    clear_process_dependency_graph_cache(cache_dir=cache_dir)
    cache_dir = _graph_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()


def load_newest_cached_dependency_graph(
    *,
    cache_dir: Path | None = None,
) -> tuple[DependencyGraph, str] | None:
    """Return the newest cached graph and its cache key, if any pickle exists."""
    resolved_cache_dir = _graph_cache_dir(cache_dir)
    cached_paths = sorted(
        resolved_cache_dir.glob("*.pkl.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not cached_paths:
        return None
    cache_key = cached_paths[0].name.removesuffix(".pkl.gz")
    graph = load_dependency_graph(cache_key, cache_dir=resolved_cache_dir)
    if graph is None:
        return None
    return graph, cache_key


def prune_stale_graph_cache_entries(
    current_keys: set[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Delete cache files whose keys are not in ``current_keys``."""
    resolved_cache_dir = _graph_cache_dir(cache_dir)
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
