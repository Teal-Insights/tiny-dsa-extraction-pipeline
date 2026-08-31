"""Disk cache for ``derive_*_series`` resolution payloads."""

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
from excel_grapher.series_bindings import (
    derive_constant_series,
    derive_input_series,
    derive_internal_series,
    derive_output_series,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.graph_cache import (
    bindings_fingerprint,
    prune_cache_entries_for_other_excel_grapher_versions,
)

# 1.2.0: key includes bindings_fingerprint independently of graph_cache_key.
# 1.1.0: payload is (input, output, internal, constant); 1.0.0 was a 3-tuple.
SERIES_RESOLUTION_CACHE_SCHEMA_VERSION = "1.2.0"
DEFAULT_SERIES_RESOLUTION_CACHE_DIR = (
    Path(__file__).resolve().parents[1] / ".cache" / "series-resolution"
)
COMMITTED_SERIES_RESOLUTION_CACHE_DIR = DEFAULT_SERIES_RESOLUTION_CACHE_DIR

SeriesResolutionList = Sequence[Mapping[str, Any]]


def _series_resolution_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_SERIES_RESOLUTION_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def series_resolution_cache_key(*, graph_cache_key: str, bindings_path: Path) -> str:
    payload = {
        "cache_schema_version": SERIES_RESOLUTION_CACHE_SCHEMA_VERSION,
        "graph_cache_key": graph_cache_key,
        "bindings_fingerprint": bindings_fingerprint(bindings_path),
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_series_resolution_meta(
    meta_path: Path,
    *,
    cache_key: str,
    graph_cache_key: str,
    input_series_count: int,
    output_series_count: int,
    internal_series_count: int,
    constant_series_count: int,
) -> None:
    meta = {
        "cache_schema_version": SERIES_RESOLUTION_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "graph_cache_key": graph_cache_key,
        "input_series_count": input_series_count,
        "output_series_count": output_series_count,
        "internal_series_count": internal_series_count,
        "constant_series_count": constant_series_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _is_series_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(isinstance(item, Mapping) for item in value)


def save_series_resolution_payload(
    input_series: SeriesResolutionList,
    output_series: SeriesResolutionList,
    internal_series: SeriesResolutionList,
    constant_series: SeriesResolutionList,
    *,
    cache_key: str,
    graph_cache_key: str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _series_resolution_cache_dir(cache_dir)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(
            (
                list(input_series),
                list(output_series),
                list(internal_series),
                list(constant_series),
            ),
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    _write_series_resolution_meta(
        meta_path,
        cache_key=cache_key,
        graph_cache_key=graph_cache_key,
        input_series_count=len(input_series),
        output_series_count=len(output_series),
        internal_series_count=len(internal_series),
        constant_series_count=len(constant_series),
    )


def load_series_resolution_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> (
    tuple[
        list[Mapping[str, Any]],
        list[Mapping[str, Any]],
        list[Mapping[str, Any]],
        list[Mapping[str, Any]],
    ]
    | None
):
    payload_path, _meta_path = _cache_paths(
        _series_resolution_cache_dir(cache_dir), cache_key
    )
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, tuple) or len(payload) != 4:
        payload_path.unlink(missing_ok=True)
        return None
    input_series, output_series, internal_series, constant_series = payload
    if not (
        _is_series_list(input_series)
        and _is_series_list(output_series)
        and _is_series_list(internal_series)
        and _is_series_list(constant_series)
    ):
        payload_path.unlink(missing_ok=True)
        return None
    return (
        cast(list[Mapping[str, Any]], input_series),
        cast(list[Mapping[str, Any]], output_series),
        cast(list[Mapping[str, Any]], internal_series),
        cast(list[Mapping[str, Any]], constant_series),
    )


@dataclass(frozen=True)
class SeriesResolutionCacheResult:
    input_series: SeriesResolutionList
    output_series: SeriesResolutionList
    internal_series: SeriesResolutionList
    constant_series: SeriesResolutionList
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_series_resolution(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    workbook_path: Path,
    graph_cache_key: str,
    bindings_path: Path,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> SeriesResolutionCacheResult:
    resolved_cache_dir = _series_resolution_cache_dir(cache_dir)
    cache_key = series_resolution_cache_key(
        graph_cache_key=graph_cache_key,
        bindings_path=bindings_path,
    )
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_series_resolution_payload(cache_key, cache_dir=resolved_cache_dir)
        if loaded is not None:
            input_series, output_series, internal_series, constant_series = loaded
            prune_cache_entries_for_other_excel_grapher_versions(
                cache_dir=resolved_cache_dir,
            )
            elapsed = time.perf_counter() - started
            print(f"derive_series: cache hit ({elapsed:.1f}s, key={cache_key[:12]})")
            return SeriesResolutionCacheResult(
                input_series=input_series,
                output_series=output_series,
                internal_series=internal_series,
                constant_series=constant_series,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    input_series = cast(
        SeriesResolutionList,
        derive_input_series(graph, bindings, workbook=workbook_path),
    )
    output_series = cast(
        SeriesResolutionList,
        derive_output_series(graph, bindings, workbook=workbook_path),
    )
    internal_series = cast(
        SeriesResolutionList,
        derive_internal_series(graph, bindings, workbook=workbook_path),
    )
    constant_series = cast(
        SeriesResolutionList,
        derive_constant_series(graph, bindings, workbook=workbook_path),
    )
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_series_resolution_payload(
            input_series,
            output_series,
            internal_series,
            constant_series,
            cache_key=cache_key,
            graph_cache_key=graph_cache_key,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        prune_cache_entries_for_other_excel_grapher_versions(
            cache_dir=resolved_cache_dir,
        )
        print(
            "derive_series: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, key={cache_key[:12]})"
        )
    else:
        print(
            "derive_series: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return SeriesResolutionCacheResult(
        input_series=input_series,
        output_series=output_series,
        internal_series=internal_series,
        constant_series=constant_series,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_series_resolution_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    cache_dir = _series_resolution_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()


def prune_stale_series_resolution_cache_entries(
    current_keys: set[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Delete cache files whose keys are not in ``current_keys``."""
    resolved_cache_dir = _series_resolution_cache_dir(cache_dir)
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
