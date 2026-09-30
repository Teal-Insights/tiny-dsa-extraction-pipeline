"""Materialize ``dist/`` as a disposable projection of cached artifacts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from src.codegen_cache import (
    load_codegen_payload,
    save_codegen_payload,
    write_generated_modules,
)
from src.export_series_graph import seed_series_graph
from src.export_validation_assets import (
    export_reference_reports,
    seed_validation_harness,
)
from src.pipeline_config import PipelineConfig
from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    GRAPH_BASELINE_DEPS,
    VALIDATION_BASELINE_DEV_DEPS,
    render_dist_pyproject_toml,
    write_dist_readme,
)
from src.series_graph_layout import SERIES_GRAPH_LAYOUT_REL, write_series_graph_layout

PACKAGE_CACHE_KEYS_FILENAME = ".pipeline-cache-keys.json"
PACKAGE_OVERLAY_MANIFEST_FILENAME = ".package-overlay-manifest.json"
# Hand-authored, workbook-specific files copied over ``dist/`` at the same
# relative paths after everything else is written.
DIST_OVERLAY_REL = Path("dist-overlay")
DIST_GITIGNORE_CONTENT = """
*.egg-info/
*.pyc
__pycache__/
.venv/
tests/results/local/
.cache/
"""
_BLANK_RANGES_MODULE_NAME = "blank_ranges.py"
# Paths the pipeline writes into ``dist/``. The overlay may not supply them,
# because a later stage (or the next run) would silently overwrite it. Seeded
# series-graph files stay overridable.
_OVERLAY_RESERVED_FILES = frozenset(
    {
        ".github/workflows/deploy-docs.yml",
        ".gitignore",
        PACKAGE_CACHE_KEYS_FILENAME,
        PACKAGE_OVERLAY_MANIFEST_FILENAME,
        "README.md",
        SERIES_GRAPH_LAYOUT_REL.as_posix(),
        "great-docs.yml",
        "pyproject.toml",
        "tests/README.md",
        "tests/__init__.py",
        "uv.lock",
    }
)
_OVERLAY_RESERVED_DIRS = (
    "bindings/",
    "docs-source/",
    "great-docs/",
    "tests/differential/",
    "tests/fixtures/",
    "tests/results/",
    "user_guide/",
)
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


def render_blank_ranges_module(blank_ranges: Iterable[str]) -> str:
    """Python module exposing the pipeline's ``BLANK_RANGES`` to the package."""
    entries = "".join(f"    {spec!r},\n" for spec in blank_ranges)
    return (
        '"""Blank ranges copied from the extraction pipeline workbook config.\n'
        "\n"
        "The series-graph evaluator passes these to ``create_dependency_graph`` so\n"
        'the runtime graph matches the one the pipeline built.\n"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        f"BLANK_RANGES: tuple[str, ...] = (\n{entries})\n"
    )


def dist_overlay_root(config: PipelineConfig) -> Path:
    return config.repo_root / DIST_OVERLAY_REL


def collect_dist_overlay(
    config: PipelineConfig, modules: Mapping[str, str]
) -> tuple[str, ...]:
    """Return ``dist-overlay/`` file paths relative to ``dist/``.

    Raises ``ValueError`` if any would replace a path the pipeline writes.
    """
    overlay_root = dist_overlay_root(config)
    if not overlay_root.is_dir():
        return ()
    package = config.dist_metadata.package_name
    reserved = _OVERLAY_RESERVED_FILES | {
        f"{package}/{name}" for name in (*modules, _BLANK_RANGES_MODULE_NAME)
    }
    # Sort the POSIX strings: Windows ``Path`` ordering ignores case.
    files = tuple(
        sorted(
            path.relative_to(overlay_root).as_posix()
            for path in overlay_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.relative_to(overlay_root).parts
        )
    )
    shadowed = [
        relative
        for relative in files
        if relative in reserved or relative.startswith(_OVERLAY_RESERVED_DIRS)
    ]
    if shadowed:
        raise ValueError(
            f"dist-overlay/ may not replace paths the pipeline writes: {shadowed}"
        )
    return files


def _read_overlay_manifest(dist_root: Path) -> tuple[str, ...]:
    path = dist_root / PACKAGE_OVERLAY_MANIFEST_FILENAME
    if not path.is_file():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(p, str) for p in payload):
        raise TypeError(f"invalid package overlay manifest: {path}")
    return tuple(payload)


def remove_stale_overlay_files(dist_root: Path, files: tuple[str, ...]) -> None:
    """Delete files a previous overlay copied that the current overlay lacks.

    Runs before the tree is rewritten, so a dropped override of a seeded file
    (e.g. ``graph_schema.py``) is restored from the template.
    """
    for stale in sorted(set(_read_overlay_manifest(dist_root)) - set(files)):
        stale_path = dist_root / stale
        stale_path.unlink(missing_ok=True)
        parent = stale_path.parent
        while parent != dist_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def apply_dist_overlay(config: PipelineConfig, files: tuple[str, ...]) -> None:
    """Copy overlay ``files`` onto ``dist/`` and record them in the manifest."""
    manifest_path = config.dist_root / PACKAGE_OVERLAY_MANIFEST_FILENAME
    if not files:
        manifest_path.unlink(missing_ok=True)
        return
    overlay_root = dist_overlay_root(config)
    for relative in files:
        destination = config.dist_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay_root / relative, destination)
    manifest_path.write_text(
        json.dumps(list(files), indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _write_dist_tree(config: PipelineConfig, modules: dict[str, str]) -> None:
    """Write package modules and dist metadata/harness files."""
    overlay_files = collect_dist_overlay(config, modules)
    remove_stale_overlay_files(config.dist_root, overlay_files)
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
            graph_dependencies=list(GRAPH_BASELINE_DEPS),
            metadata=config.dist_metadata,
        ),
        encoding="utf-8",
    )
    write_dist_readme(config.dist_root, metadata=config.dist_metadata)
    seed_validation_harness(config=config)
    seed_series_graph(config=config)
    (config.package_root / _BLANK_RANGES_MODULE_NAME).write_text(
        render_blank_ranges_module(config.blank_ranges),
        encoding="utf-8",
        newline="\n",
    )
    replace_dist_bindings(config)
    apply_dist_overlay(config, overlay_files)
    # After the overlay: it supplies the authored graph_schema.py.
    write_series_graph_layout(config)


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
