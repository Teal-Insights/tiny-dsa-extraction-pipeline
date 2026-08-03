"""Disk cache for ``validate_series_bindings`` reports."""

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

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.series_bindings import validate_series_bindings
from excel_grapher.series_bindings.types import (
    ValidationReport,
    WorkbookSeriesBindings,
)

from src.graph_cache import prune_cache_entries_for_other_excel_grapher_versions

BINDINGS_VALIDATION_CACHE_SCHEMA_VERSION = "1.0.0"
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS_VALIDATION_CACHE_DIR = _REPO_ROOT / ".cache" / "bindings-validation"
# Pytest redirects DEFAULT_* to an isolated temp dir; session fixtures and the
# regenerate script pin this constant so CI loads the committed repo cache.
COMMITTED_BINDINGS_VALIDATION_CACHE_DIR = DEFAULT_BINDINGS_VALIDATION_CACHE_DIR


def _bindings_validation_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_BINDINGS_VALIDATION_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def bindings_validation_cache_key(*, graph_cache_key: str) -> str:
    payload = {
        "cache_schema_version": BINDINGS_VALIDATION_CACHE_SCHEMA_VERSION,
        "graph_cache_key": graph_cache_key,
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_bindings_validation_meta(
    meta_path: Path,
    *,
    cache_key: str,
    graph_cache_key: str,
    ok: bool,
    issue_count: int,
) -> None:
    meta = {
        "cache_schema_version": BINDINGS_VALIDATION_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "graph_cache_key": graph_cache_key,
        "ok": ok,
        "issue_count": issue_count,
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _is_validation_issue(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("level") in ("error", "warning")
        and isinstance(value.get("code"), str)
        and isinstance(value.get("message"), str)
        and (value.get("series_id") is None or isinstance(value.get("series_id"), str))
        and (value.get("address") is None or isinstance(value.get("address"), str))
    )


def _is_validation_report(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("ok"), bool):
        return False
    issues = value.get("issues")
    if not isinstance(issues, list):
        return False
    return all(_is_validation_issue(issue) for issue in issues)


def save_bindings_validation_report(
    report: ValidationReport,
    *,
    cache_key: str,
    graph_cache_key: str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _bindings_validation_cache_dir(cache_dir)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    # mtime=0 keeps the gzip stream byte-identical across rebuilds so committed
    # cache artifacts do not churn when contents are unchanged.
    with payload_path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as handle:
            pickle.dump(dict(report), handle, protocol=pickle.HIGHEST_PROTOCOL)
    _write_bindings_validation_meta(
        meta_path,
        cache_key=cache_key,
        graph_cache_key=graph_cache_key,
        ok=report["ok"],
        issue_count=len(report["issues"]),
    )


def load_bindings_validation_report(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> ValidationReport | None:
    payload_path, _meta_path = _cache_paths(
        _bindings_validation_cache_dir(cache_dir), cache_key
    )
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not _is_validation_report(payload):
        payload_path.unlink(missing_ok=True)
        return None
    report = cast(ValidationReport, payload)
    report["issues"] = list(report["issues"])
    return report


@dataclass(frozen=True)
class BindingsValidationCacheResult:
    report: ValidationReport
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_bindings_validation(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    workbook_path: Path,
    graph_cache_key: str,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> BindingsValidationCacheResult:
    resolved_cache_dir = _bindings_validation_cache_dir(cache_dir)
    cache_key = bindings_validation_cache_key(graph_cache_key=graph_cache_key)
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_bindings_validation_report(
            cache_key, cache_dir=resolved_cache_dir
        )
        if loaded is not None:
            prune_cache_entries_for_other_excel_grapher_versions(
                cache_dir=resolved_cache_dir,
            )
            elapsed = time.perf_counter() - started
            print(
                "validate_series_bindings: cache hit "
                f"({elapsed:.1f}s, key={cache_key[:12]})"
            )
            return BindingsValidationCacheResult(
                report=loaded,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    report = validate_series_bindings(
        graph,
        bindings,
        workbook=workbook_path,
    )
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_bindings_validation_report(
            report,
            cache_key=cache_key,
            graph_cache_key=graph_cache_key,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        prune_cache_entries_for_other_excel_grapher_versions(
            cache_dir=resolved_cache_dir,
        )
        print(
            "validate_series_bindings: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, "
            f"key={cache_key[:12]})"
        )
    else:
        print(
            "validate_series_bindings: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return BindingsValidationCacheResult(
        report=report,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_bindings_validation_cache(
    *,
    cache_dir: Path | None = None,
) -> None:
    cache_dir = _bindings_validation_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()


def prune_stale_bindings_validation_cache_entries(
    current_keys: set[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Delete cache files whose keys are not in ``current_keys``."""
    resolved_cache_dir = _bindings_validation_cache_dir(cache_dir)
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
