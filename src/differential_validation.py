"""Post-refactor exported-library differential orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from importlib.metadata import version
from pathlib import Path

from src.graph_cache import file_fingerprint, stable_json
from src.pipeline_config import PipelineConfig

logger = logging.getLogger(__name__)

DIFFERENTIAL_CACHE_SCHEMA_VERSION = "1.1.0"
PARITY_CACHE_META_FILENAME = "parity_cache.meta.json"
REFERENCE_REPORT_FILES = ("parity_report.csv", "parity_report.txt")
EXPORTED_LIBRARY_HARNESS_FILES = (
    "differential_test_exported_library.py",
    "differential_types.py",
    "differential_excel.py",
    "comparison_utils.py",
    "workbook_labels.py",
    "differential_scenario_inputs.py",
)


def running_in_ci() -> bool:
    """Return True when running under GitHub Actions / standard CI envs."""
    return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


def directory_fingerprint(root: Path) -> str:
    """Hash all files under ``root`` in stable relative-path order."""
    resolved = root.resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"Directory is not present: {resolved}")
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in resolved.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def harness_fingerprint(harness_dir: Path) -> str:
    """Hash exported-library harness sources that affect differential outcomes."""
    resolved = harness_dir.resolve()
    digest = hashlib.sha256()
    for filename in EXPORTED_LIBRARY_HARNESS_FILES:
        path = resolved / filename
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def differential_cache_key(*, config: PipelineConfig) -> str:
    """Fingerprint inputs that determine exported-library differential outcomes."""
    harness_dir = config.repo_root / "tests" / "differential"
    payload = {
        "cache_schema_version": DIFFERENTIAL_CACHE_SCHEMA_VERSION,
        "package_fingerprint": directory_fingerprint(config.package_root),
        "workbook_fingerprint": file_fingerprint(config.workbook_path),
        "harness_fingerprint": harness_fingerprint(harness_dir),
        "excel_grapher_version": version("excel-grapher"),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def save_differential_cache_meta(
    *,
    report_dir: Path,
    cache_key: str,
    exit_code: int,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "cache_schema_version": DIFFERENTIAL_CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "exit_code": exit_code,
    }
    (report_dir / PARITY_CACHE_META_FILENAME).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def invalidate_differential_cache_meta(*, report_dir: Path) -> None:
    """Remove cache meta so a later run cannot reuse prior successful reports."""
    (report_dir / PARITY_CACHE_META_FILENAME).unlink(missing_ok=True)


def load_differential_cache_meta(report_dir: Path) -> dict[str, object] | None:
    meta_path = report_dir / PARITY_CACHE_META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def has_parity_reports(report_dir: Path) -> bool:
    return all((report_dir / name).is_file() for name in REFERENCE_REPORT_FILES)


def can_reuse_cached_differential_reports(
    *,
    report_dir: Path,
    cache_key: str,
) -> bool:
    """Return True when prior successful reports match ``cache_key``."""
    if not has_parity_reports(report_dir):
        return False
    meta = load_differential_cache_meta(report_dir)
    if meta is None:
        return False
    return meta.get("cache_key") == cache_key and meta.get("exit_code") == 0


def run_post_refactor_differential(
    *,
    config: PipelineConfig,
    no_cache: bool = False,
) -> int | None:
    """Run the exported-library differential against the post-refactor package.

    Writes reports under ``config.differential_report_dir_rel`` in the extraction
    repo. Skips under CI so committed caches can be reused. Locally, skips the
    live harness when the package/workbook/harness fingerprint matches a prior
    successful run (unless ``no_cache``). Non-zero exits and harness exceptions
    are logged as warnings; they do not abort the pipeline. Harness exceptions
    invalidate cache meta so the next run retries instead of reusing a stale hit.
    """
    if running_in_ci():
        logger.warning(
            "Skipping exported-library differential in CI; "
            "reusing cached reports under %s",
            config.differential_report_dir_rel.as_posix(),
        )
        return None

    report_dir = config.repo_root / config.differential_report_dir_rel
    cache_key: str | None
    try:
        cache_key = differential_cache_key(config=config)
    except (OSError, NotADirectoryError, FileNotFoundError) as exc:
        logger.warning(
            "Cannot fingerprint differential inputs (%s); running harness",
            exc,
        )
        cache_key = None

    if (
        cache_key is not None
        and not no_cache
        and can_reuse_cached_differential_reports(
            report_dir=report_dir, cache_key=cache_key
        )
    ):
        logger.info(
            "Reusing cached exported-library differential reports under %s (key=%s)",
            config.differential_report_dir_rel.as_posix(),
            cache_key[:12],
        )
        return 0

    from tests.differential.differential_test_exported_library import (
        resolve_config,
        run_differential_test,
    )

    harness_path = (
        config.repo_root
        / "tests"
        / "differential"
        / "differential_test_exported_library.py"
    )
    try:
        differential_config = resolve_config(
            module_path=harness_path,
            layout="repo",
            report_dir=report_dir,
        )
        exit_code = run_differential_test(differential_config)
    except Exception:
        invalidate_differential_cache_meta(report_dir=report_dir)
        logger.exception(
            "Exported-library differential failed with an unhandled exception; "
            "continuing so any existing reports can still be exported"
        )
        return None

    if cache_key is not None and has_parity_reports(report_dir):
        save_differential_cache_meta(
            report_dir=report_dir,
            cache_key=cache_key,
            exit_code=exit_code,
        )

    if exit_code != 0:
        logger.warning(
            "Exported-library differential finished with exit code %s; "
            "exporting reports under %s for diagnosis",
            exit_code,
            report_dir,
        )
    return exit_code
