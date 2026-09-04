"""Disk cache for ``CodeGenerator.generate_modules`` payloads."""

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
from typing import cast

from src.llm_providers import model_from_env

DOCSTRING_MODEL_ENV = "DOCSTRING_MODEL"
DOCSTRING_PROMPT_VERSION = 3

CODEGEN_CACHE_SCHEMA_VERSION = "1.0.0"
DEFAULT_CODEGEN_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "codegen"

OPTIONAL_GENERATED_MODULES = frozenset(
    {"_readers.py", "_api_helpers.py", "_output_leaves.py"}
)

CodegenModules = dict[str, str]


def _codegen_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        return DEFAULT_CODEGEN_CACHE_DIR
    return cache_dir


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def guide_fingerprint(guide_path: Path) -> str:
    return hashlib.sha256(guide_path.read_bytes()).hexdigest()


def codegen_cache_key(
    *,
    projection_cache_key: str,
    targets: Sequence[str],
    unpack_return: bool,
    docstring_renderer: str,
    series_docstring_callback: str,
    guide_sha256: str,
    docstring_prompt_version: int = DOCSTRING_PROMPT_VERSION,
    docstring_model: str,
    paradigm: str = "ctx",
) -> str:
    payload = {
        "cache_schema_version": CODEGEN_CACHE_SCHEMA_VERSION,
        "projection_cache_key": projection_cache_key,
        "targets": sorted(targets),
        "unpack_return": unpack_return,
        "docstring_renderer": docstring_renderer,
        "series_docstring_callback": series_docstring_callback,
        "guide_sha256": guide_sha256,
        "docstring_prompt_version": docstring_prompt_version,
        "docstring_model": docstring_model,
        "paradigm": paradigm,
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        cache_dir / f"{cache_key}.pkl.gz",
        cache_dir / f"{cache_key}.meta.json",
    )


def _write_codegen_meta(
    meta_path: Path,
    *,
    cache_key: str,
    projection_cache_key: str,
    module_count: int,
    module_names: Sequence[str],
) -> None:
    meta = {
        "cache_schema_version": CODEGEN_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "projection_cache_key": projection_cache_key,
        "module_count": module_count,
        "module_names": sorted(module_names),
        "excel_grapher_version": version("excel-grapher"),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _is_modules_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(name, str) and isinstance(source, str)
        for name, source in value.items()
    )


def save_codegen_payload(
    modules: Mapping[str, str],
    *,
    cache_key: str,
    projection_cache_key: str,
    cache_dir: Path | None = None,
) -> None:
    resolved_cache_dir = _codegen_cache_dir(cache_dir)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path, meta_path = _cache_paths(resolved_cache_dir, cache_key)
    payload = dict(modules)
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    _write_codegen_meta(
        meta_path,
        cache_key=cache_key,
        projection_cache_key=projection_cache_key,
        module_count=len(payload),
        module_names=tuple(payload),
    )


def load_codegen_payload(
    cache_key: str,
    *,
    cache_dir: Path | None = None,
) -> CodegenModules | None:
    payload_path, _meta_path = _cache_paths(_codegen_cache_dir(cache_dir), cache_key)
    if not payload_path.is_file():
        return None
    try:
        with gzip.open(payload_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        payload_path.unlink(missing_ok=True)
        return None
    if not _is_modules_payload(payload):
        payload_path.unlink(missing_ok=True)
        return None
    return cast(CodegenModules, dict(payload))


def write_generated_modules(package_root: Path, modules: Mapping[str, str]) -> None:
    """Write module texts under ``package_root`` and drop stale optional modules."""
    package_root.mkdir(parents=True, exist_ok=True)
    for filepath, code in modules.items():
        output_path = package_root / filepath
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(code, encoding="utf-8", newline="\n")
    for optional_name in OPTIONAL_GENERATED_MODULES:
        if optional_name in modules:
            continue
        stale_path = package_root / optional_name
        if stale_path.is_file():
            stale_path.unlink()


@dataclass(frozen=True)
class CodegenCacheResult:
    modules: CodegenModules
    cache_key: str
    cache_hit: bool
    elapsed_seconds: float


def get_or_build_codegen_modules(
    *,
    projection_cache_key: str,
    targets: Sequence[str],
    unpack_return: bool,
    docstring_renderer: str,
    series_docstring_callback: str,
    guide_sha256: str,
    docstring_prompt_version: int = DOCSTRING_PROMPT_VERSION,
    docstring_model: str | None = None,
    paradigm: str = "ctx",
    build_modules: Callable[[], Mapping[str, str]],
    cache_dir: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> CodegenCacheResult:
    resolved_cache_dir = _codegen_cache_dir(cache_dir)
    resolved_model = (
        docstring_model
        if docstring_model is not None
        else model_from_env(DOCSTRING_MODEL_ENV)
    )
    cache_key = codegen_cache_key(
        projection_cache_key=projection_cache_key,
        targets=targets,
        unpack_return=unpack_return,
        docstring_renderer=docstring_renderer,
        series_docstring_callback=series_docstring_callback,
        guide_sha256=guide_sha256,
        docstring_prompt_version=docstring_prompt_version,
        docstring_model=resolved_model,
        paradigm=paradigm,
    )
    started = time.perf_counter()
    if not no_cache and not force_rebuild:
        loaded = load_codegen_payload(cache_key, cache_dir=resolved_cache_dir)
        if loaded is not None:
            elapsed = time.perf_counter() - started
            print(f"codegen: cache hit ({elapsed:.1f}s, key={cache_key[:12]})")
            return CodegenCacheResult(
                modules=loaded,
                cache_key=cache_key,
                cache_hit=True,
                elapsed_seconds=elapsed,
            )

    build_started = time.perf_counter()
    modules = dict(build_modules())
    build_elapsed = time.perf_counter() - build_started

    if not no_cache:
        save_started = time.perf_counter()
        save_codegen_payload(
            modules,
            cache_key=cache_key,
            projection_cache_key=projection_cache_key,
            cache_dir=resolved_cache_dir,
        )
        save_elapsed = time.perf_counter() - save_started
        print(
            "codegen: cache miss "
            f"(build {build_elapsed:.1f}s, save {save_elapsed:.1f}s, key={cache_key[:12]})"
        )
    else:
        print(
            f"codegen: cache bypassed (build {build_elapsed:.1f}s, key={cache_key[:12]})"
        )

    return CodegenCacheResult(
        modules=modules,
        cache_key=cache_key,
        cache_hit=False,
        elapsed_seconds=time.perf_counter() - started,
    )


def clear_codegen_cache(*, cache_dir: Path | None = None) -> None:
    cache_dir = _codegen_cache_dir(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        if path.is_file():
            path.unlink()
