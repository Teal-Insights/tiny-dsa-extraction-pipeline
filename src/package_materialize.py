"""Materialize ``dist/`` as a disposable projection of cached artifacts."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from src.codegen_cache import (
    load_codegen_payload,
    save_codegen_payload,
    write_generated_modules,
)
from src.export_validation_assets import (
    export_reference_reports,
    seed_validation_harness,
)
from src.pipeline_config import PipelineConfig
from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    VALIDATION_BASELINE_DEV_DEPS,
    render_dist_pyproject_toml,
    write_dist_readme,
)

PACKAGE_CACHE_KEYS_FILENAME = ".pipeline-cache-keys.json"
DIST_GITIGNORE_CONTENT = """
*.egg-info/
*.pyc
__pycache__/
.venv/
tests/results/local/
"""
_GENERATED_ROOT_MODULE_NAMES = frozenset(
    {"__init__.py", "api.py", "data.py", "runtime.py", "internals.py"}
)
_PACKAGE_MODULE_NAMES = (
    "__init__.py",
    "api.py",
    "data.py",
    "runtime.py",
    "internals.py",
)


@dataclass(frozen=True)
class PackageCacheKeys:
    """Cache keys recorded beside a materialized ``dist/`` tree."""

    codegen_key: str


def package_cache_keys_path(dist_root: Path) -> Path:
    return dist_root / PACKAGE_CACHE_KEYS_FILENAME


def write_package_cache_keys(dist_root: Path, keys: PackageCacheKeys) -> None:
    dist_root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"codegen_key": keys.codegen_key}
    package_cache_keys_path(dist_root).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_package_cache_keys(dist_root: Path) -> PackageCacheKeys | None:
    path = package_cache_keys_path(dist_root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid package cache keys payload: {path}")
    codegen_key = payload.get("codegen_key")
    if not isinstance(codegen_key, str) or not codegen_key:
        raise ValueError(f"package cache keys missing codegen_key: {path}")
    return PackageCacheKeys(codegen_key=codegen_key)


def _read_package_modules(package_root: Path) -> dict[str, str] | None:
    modules: dict[str, str] = {}
    for name in _PACKAGE_MODULE_NAMES:
        path = package_root / name
        if not path.is_file():
            return None
        modules[name] = path.read_text(encoding="utf-8")
    return modules


def replace_dist_bindings(config: PipelineConfig) -> None:
    """Replace ``dist/bindings`` with the authored bindings directory.

    Anything under the destination that is absent from the source is removed,
    including files left behind by an earlier materialization.
    """
    source = config.bindings_path
    destination = config.dist_root / "bindings"
    if not source.is_dir():
        raise FileNotFoundError(f"bindings directory not found: {source}")

    source_resolved = source.resolve()
    destination_resolved = destination.resolve()
    if (
        source_resolved == destination_resolved
        or destination_resolved.is_relative_to(source_resolved)
        or source_resolved.is_relative_to(destination_resolved)
    ):
        raise ValueError(
            f"refusing to replace {destination} from overlapping bindings path {source}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    shutil.copytree(source, destination)


def _write_dist_tree(config: PipelineConfig, modules: dict[str, str]) -> None:
    """Write package modules and dist metadata/harness files."""
    write_generated_modules(config.package_root, modules)

    for stale_module in _GENERATED_ROOT_MODULE_NAMES:
        stale_path = config.dist_root / stale_module
        if stale_path.is_file():
            stale_path.unlink()

    (config.dist_root / ".gitignore").write_text(
        DIST_GITIGNORE_CONTENT, encoding="utf-8"
    )
    (config.dist_root / "pyproject.toml").write_text(
        render_dist_pyproject_toml(
            dev_dependencies=list(DOCUMENTATION_BASELINE_DEV_DEPS),
            validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
            metadata=config.dist_metadata,
        ),
        encoding="utf-8",
    )
    write_dist_readme(config.dist_root, metadata=config.dist_metadata)
    seed_validation_harness(config=config)
    replace_dist_bindings(config)


def materialize_package(
    config: PipelineConfig,
    *,
    codegen_key: str,
    include_reference_reports: bool = False,
) -> None:
    """Write the complete ``dist/`` tree from cache plus config.

    This is the single writer for the generated package projection. Export and
    mid-pipeline stage entry both call it so ``dist/`` stays disposable.
    """
    modules = load_codegen_payload(codegen_key)
    if modules is None:
        raise FileNotFoundError(
            f"codegen cache payload missing for key={codegen_key[:12]}; "
            "cannot materialize dist/"
        )
    _write_dist_tree(config, dict(modules))
    if include_reference_reports:
        export_reference_reports(config=config)

    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(codegen_key=codegen_key),
    )


def adopt_codegen_cache_from_dist(
    config: PipelineConfig,
    *,
    expected_codegen_key: str,
    projection_cache_key: str,
) -> bool:
    """Adopt package modules into ``.cache/codegen/`` when keys match."""
    keys = read_package_cache_keys(config.dist_root)
    if keys is None or keys.codegen_key != expected_codegen_key:
        return False
    modules = _read_package_modules(config.package_root)
    if modules is None:
        return False
    save_codegen_payload(
        modules,
        cache_key=expected_codegen_key,
        projection_cache_key=projection_cache_key,
    )
    return True
