"""Content-keyed disk cache for refactored ``internals.py`` (issue #239)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from src.mechanical_body import MECHANICAL_BODY_SCHEMA_VERSION
from src.refactor_parity_gate import PARITY_GATE_SCHEMA_VERSION

INTERNALS_CACHE_SCHEMA_VERSION = "1.0.0"


def _default_internals_cache_dir() -> Path:
    # Look up at call time so pytest cache redirects stay in sync.
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    return DEFAULT_INTERNALS_CACHE_DIR


def _internals_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return _default_internals_cache_dir()
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def mechanical_refactor_bodies_value() -> str:
    """Normalize ``MECHANICAL_REFACTOR_BODIES`` for cache-key inclusion."""
    raw = os.environ.get("MECHANICAL_REFACTOR_BODIES", "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return "0"
    return "1"


def consumed_refactors_digest(cache: Mapping[str, str] | None = None) -> str:
    """Hash refactor LLM response entries that feed a warm ``internals.py``.

    The consumed key set cannot be known without running Pass 1/2, so this hashes
    the full ``internals-refactors.json`` map (empty when absent). That is a
    sound over-approximation: unrelated entry churn may false-miss, but never
    false-hit.
    """
    if cache is None:
        from src.internals_refactor import load_refactor_cache

        cache = load_refactor_cache()
    return hashlib.sha256(stable_json(dict(cache)).encode()).hexdigest()


def internals_cache_key(
    *,
    codegen_cache_key: str,
    clusters_cache_key: str,
    consumed_refactors_digest: str,
    mechanical_body_schema_version: str = MECHANICAL_BODY_SCHEMA_VERSION,
    parity_gate_schema_version: str = PARITY_GATE_SCHEMA_VERSION,
    mechanical_refactor_bodies: str | None = None,
    refactor_model: str | None = None,
    excel_grapher_version: str | None = None,
) -> str:
    if mechanical_refactor_bodies is None:
        mechanical_refactor_bodies = mechanical_refactor_bodies_value()
    if refactor_model is None:
        from src.internals_refactor import refactor_model as _refactor_model

        refactor_model = _refactor_model()
    if excel_grapher_version is None:
        excel_grapher_version = version("excel-grapher")
    payload = {
        "cache_schema_version": INTERNALS_CACHE_SCHEMA_VERSION,
        "codegen_cache_key": codegen_cache_key,
        "clusters_cache_key": clusters_cache_key,
        "consumed_refactors_digest": consumed_refactors_digest,
        "mechanical_body_schema_version": mechanical_body_schema_version,
        "parity_gate_schema_version": parity_gate_schema_version,
        "mechanical_refactor_bodies": mechanical_refactor_bodies,
        "refactor_model": refactor_model,
        "excel_grapher_version": excel_grapher_version,
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def internals_key_provenance(
    *,
    mechanical_body_schema_version: str = MECHANICAL_BODY_SCHEMA_VERSION,
    parity_gate_schema_version: str = PARITY_GATE_SCHEMA_VERSION,
    mechanical_refactor_bodies: str | None = None,
    refactor_model: str | None = None,
    excel_grapher_version: str | None = None,
) -> dict[str, str]:
    """Reproducible components of :func:`internals_cache_key`, for adopt checks.

    ``dist/`` adoption (#238) runs before clustering, so it cannot recompute the
    full content key — ``clusters_cache_key`` is not known yet. These fields are
    the subset that describes *how* a refactored module was produced and that a
    fresh clone can reproduce exactly, so they can gate adoption of a committed
    ``dist/``.

    ``consumed_refactors_digest`` is deliberately excluded: it hashes the whole
    ``internals-refactors.json`` map, which a fresh clone does not have, so
    including it would refuse every cold-clone adoption. Drift in the LLM
    response cache is still caught by the strict content key once
    ``.cache/internals/`` is warm.
    """
    if mechanical_refactor_bodies is None:
        mechanical_refactor_bodies = mechanical_refactor_bodies_value()
    if refactor_model is None:
        from src.internals_refactor import refactor_model as _refactor_model

        refactor_model = _refactor_model()
    if excel_grapher_version is None:
        excel_grapher_version = version("excel-grapher")
    return {
        "cache_schema_version": INTERNALS_CACHE_SCHEMA_VERSION,
        "mechanical_body_schema_version": mechanical_body_schema_version,
        "parity_gate_schema_version": parity_gate_schema_version,
        "mechanical_refactor_bodies": mechanical_refactor_bodies,
        "refactor_model": refactor_model,
        "excel_grapher_version": excel_grapher_version,
    }


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.py",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_internals_meta(
    meta_path: Path,
    *,
    cache_key: str,
    codegen_cache_key: str,
    clusters_cache_key: str,
    consumed_refactors_digest: str,
) -> None:
    meta = {
        "cache_schema_version": INTERNALS_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "codegen_cache_key": codegen_cache_key,
        "clusters_cache_key": clusters_cache_key,
        "consumed_refactors_digest": consumed_refactors_digest,
        "mechanical_body_schema_version": MECHANICAL_BODY_SCHEMA_VERSION,
        "parity_gate_schema_version": PARITY_GATE_SCHEMA_VERSION,
        "mechanical_refactor_bodies": mechanical_refactor_bodies_value(),
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def save_refactored_internals_payload(
    source: str,
    *,
    cache_key: str,
    codegen_cache_key: str,
    clusters_cache_key: str,
    consumed_refactors_digest: str,
    cache_dir: Path | None = None,
) -> Path:
    resolved = _internals_cache_dir(cache_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved, cache_key)
    payload_path.write_text(source, encoding="utf-8", newline="\n")
    _write_internals_meta(
        meta_path,
        cache_key=cache_key,
        codegen_cache_key=codegen_cache_key,
        clusters_cache_key=clusters_cache_key,
        consumed_refactors_digest=consumed_refactors_digest,
    )
    _prune_stale_internals_for_other_excel_grapher_versions(resolved)
    return payload_path


def _prune_stale_internals_for_other_excel_grapher_versions(cache_dir: Path) -> None:
    """Delete ``{key}.py`` / ``{key}.meta.json`` for foreign excel-grapher versions."""
    retained = version("excel-grapher")
    for meta_path in sorted(cache_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("excel_grapher_version") == retained:
            continue
        cache_key = meta_path.name.removesuffix(".meta.json")
        py_path = cache_dir / f"{cache_key}.py"
        if py_path.is_file():
            py_path.unlink()
        meta_path.unlink(missing_ok=True)


def clear_internals_cache(*, cache_dir: Path | None = None) -> list[str]:
    """Delete every refactored-internals entry and Pass 1 checkpoint.

    There is no prune-to-current-keys counterpart: an internals key folds in
    ``codegen_cache_key`` and ``clusters_cache_key``, so the current key set is
    unknowable without running codegen and clustering. Tools that invalidate
    upstream inputs therefore clear this cache outright, the same way they clear
    ``.cache/clusters/``.

    Per-package checkpoint directories are removed too. The checkpoint is Pass 1
    crash-recovery state for one specific pristine module, so it is stale as soon
    as the upstream caches are dropped.
    """
    resolved = _internals_cache_dir(cache_dir)
    if not resolved.is_dir():
        return []
    removed: list[str] = []
    for path in sorted(resolved.iterdir()):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(f"{path.name}/")
    return removed


def load_refactored_internals_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> str | None:
    payload_path, _meta_path = _cache_paths(_internals_cache_dir(cache_dir), cache_key)
    if not payload_path.is_file():
        return None
    return payload_path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class InternalsCacheResult:
    source: str
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_load_refactored_internals(
    *,
    codegen_cache_key: str,
    clusters_cache_key: str,
    consumed_refactors_digest: str,
    build_module: Callable[[], str],
    mechanical_body_schema_version: str = MECHANICAL_BODY_SCHEMA_VERSION,
    parity_gate_schema_version: str = PARITY_GATE_SCHEMA_VERSION,
    mechanical_refactor_bodies: str | None = None,
    refactor_model: str | None = None,
    excel_grapher_version: str | None = None,
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    save: bool = True,
) -> InternalsCacheResult:
    """Load a content-keyed refactored module, or build and optionally save it."""
    resolved = _internals_cache_dir(cache_dir)
    cache_key = internals_cache_key(
        codegen_cache_key=codegen_cache_key,
        clusters_cache_key=clusters_cache_key,
        consumed_refactors_digest=consumed_refactors_digest,
        mechanical_body_schema_version=mechanical_body_schema_version,
        parity_gate_schema_version=parity_gate_schema_version,
        mechanical_refactor_bodies=mechanical_refactor_bodies,
        refactor_model=refactor_model,
        excel_grapher_version=excel_grapher_version,
    )
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_refactored_internals_payload(cache_key, cache_dir=resolved)
        if loaded is not None:
            _prune_stale_internals_for_other_excel_grapher_versions(resolved)
            elapsed = time.perf_counter() - started
            print(f"internals: cache hit ({elapsed:.1f}s, key={cache_key[:12]})")
            return InternalsCacheResult(
                source=loaded,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    source = build_module()
    build_elapsed = time.perf_counter() - build_started

    if not no_cache and save:
        save_started = time.perf_counter()
        save_refactored_internals_payload(
            source,
            cache_key=cache_key,
            codegen_cache_key=codegen_cache_key,
            clusters_cache_key=clusters_cache_key,
            consumed_refactors_digest=consumed_refactors_digest,
            cache_dir=resolved,
        )
        save_elapsed = time.perf_counter() - save_started
        print(
            "internals: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, "
            f"key={cache_key[:12]})"
        )
    else:
        print(
            "internals: cache bypassed "
            f"(build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return InternalsCacheResult(
        source=source,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )
