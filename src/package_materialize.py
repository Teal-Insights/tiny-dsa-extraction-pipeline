"""Materialize ``dist/`` as a disposable projection of cached artifacts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
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
from src.soft_error_compute_codegen import (
    ensure_xl_error_exception_import,
    rewrite_compute_measure_assignment,
)

PACKAGE_CACHE_KEYS_FILENAME = ".pipeline-cache-keys.json"
DIST_GITIGNORE_CONTENT = """
*.egg-info/
*.pyc
__pycache__/
.venv/
_validate_user_guide_cells.py
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
    internals_key: str | None = None
    internals_inputs: Mapping[str, str] | None = None
    """How ``internals_key`` was produced (see ``internals_key_provenance``).

    Committed alongside ``dist/`` so cold-clone adoption can reject a refactored
    module built under a different refactor model, schema, or ``excel-grapher``
    version without first paying for clustering.
    """


def package_cache_keys_path(dist_root: Path) -> Path:
    return dist_root / PACKAGE_CACHE_KEYS_FILENAME


def write_package_cache_keys(dist_root: Path, keys: PackageCacheKeys) -> None:
    dist_root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "codegen_key": keys.codegen_key,
        "internals_key": keys.internals_key,
    }
    if keys.internals_inputs is not None:
        payload["internals_inputs"] = dict(keys.internals_inputs)
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
    internals_key = payload.get("internals_key")
    if internals_key is not None and not isinstance(internals_key, str):
        raise ValueError(f"package cache keys have non-string internals_key: {path}")
    internals_inputs = payload.get("internals_inputs")
    if internals_inputs is not None and (
        not isinstance(internals_inputs, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in internals_inputs.items()
        )
    ):
        raise ValueError(f"package cache keys have invalid internals_inputs: {path}")
    return PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=internals_key,
        internals_inputs=internals_inputs,
    )


def apply_export_runtime_memoization(modules: dict[str, str]) -> dict[str, str]:
    """Ensure ``runtime.py`` exports the helper-memoization API.

    Mechanical refactor decorates helpers with ``@xl_memoize`` and merges
    ``from .runtime import xl_memoize`` into ``internals.py``, but the codegen
    payload's ``runtime.py`` does not define that API. The refactor stage patches
    the file in place before Pass 1; without this, every later
    :func:`materialize_package` would rewrite ``runtime.py`` from the codegen
    payload and strip the API back out, leaving a package whose ``internals.py``
    imports a symbol its ``runtime.py`` no longer defines.

    Idempotent: a runtime that already exports the API is returned unchanged.
    """
    from src.helper_memoization import ensure_runtime_source_helper_memoization

    rewritten_modules = dict(modules)
    runtime_source = rewritten_modules.get("runtime.py")
    if runtime_source is None:
        return rewritten_modules
    rewritten_modules["runtime.py"] = ensure_runtime_source_helper_memoization(
        runtime_source
    )
    return rewritten_modules


def apply_export_rewrites(modules: dict[str, str]) -> dict[str, str]:
    """Apply every deterministic post-codegen rewrite ``dist/`` depends on.

    ``dist/`` is a projection of the caches plus these rewrites, so every writer
    must apply the same set or the tree it produces is not reproducible.
    """
    return apply_export_runtime_memoization(apply_export_api_rewrite(modules))


def apply_export_api_rewrite(modules: dict[str, str]) -> dict[str, str]:
    """Apply the export-stage ``api.py`` soft-error rewrite to module texts."""
    rewritten_modules = dict(modules)
    api_source = rewritten_modules.get("api.py")
    if api_source is None:
        return rewritten_modules
    rewritten = "\n".join(rewrite_compute_measure_assignment(api_source.splitlines()))
    if api_source.endswith("\n"):
        rewritten += "\n"
    rewritten_modules["api.py"] = ensure_xl_error_exception_import(rewritten)
    return rewritten_modules


def load_pristine_internals_from_codegen(codegen_key: str) -> str:
    """Return pristine ``internals.py`` text from the codegen cache.

    Fails loudly when the payload or module is missing — never falls back to
    whatever is currently on disk under ``dist/``, which may already be
    refactored.
    """
    modules = load_codegen_payload(codegen_key)
    if modules is None:
        raise FileNotFoundError(
            "codegen cache payload missing for "
            f"key={codegen_key[:12]}; cannot resolve pristine internals "
            "for the parity oracle"
        )
    source = modules.get("internals.py")
    if source is None:
        raise KeyError(
            f"codegen payload key={codegen_key[:12]} has no internals.py module"
        )
    return source


def internals_cache_path(internals_key: str, *, cache_dir: Path | None = None) -> Path:
    if cache_dir is None:
        # Look up at call time so pytest cache redirects stay in sync.
        from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

        resolved = DEFAULT_INTERNALS_CACHE_DIR
    else:
        resolved = cache_dir
    return resolved / f"{internals_key}.py"


def load_refactored_internals(
    internals_key: str, *, cache_dir: Path | None = None
) -> str:
    path = internals_cache_path(internals_key, cache_dir=cache_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"internals cache miss for key={internals_key[:12]}: {path}"
        )
    return path.read_text(encoding="utf-8")


def _read_package_modules(package_root: Path) -> dict[str, str] | None:
    modules: dict[str, str] = {}
    for name in _PACKAGE_MODULE_NAMES:
        path = package_root / name
        if not path.is_file():
            return None
        modules[name] = path.read_text(encoding="utf-8")
    return modules


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


def materialize_package(
    config: PipelineConfig,
    *,
    codegen_key: str,
    internals_key: str | None = None,
    include_reference_reports: bool = False,
    apply_rewrites: bool = True,
) -> None:
    """Write the complete ``dist/`` tree from cache plus config.

    This is the single writer for the generated package projection. Export and
    mid-pipeline stage entry both call it so ``dist/`` stays disposable.
    Inverted-tree packages skip ctx export rewrites (``apply_rewrites=False``).
    """
    modules = load_codegen_payload(codegen_key)
    if modules is None:
        raise FileNotFoundError(
            f"codegen cache payload missing for key={codegen_key[:12]}; "
            "cannot materialize dist/"
        )
    modules = dict(modules)
    if apply_rewrites:
        modules = apply_export_rewrites(modules)
    if internals_key is not None:
        modules["internals.py"] = load_refactored_internals(internals_key)

    _write_dist_tree(config, modules)
    if include_reference_reports:
        export_reference_reports(config=config)

    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=(
                None if internals_key is None else current_internals_inputs()
            ),
        ),
    )


def current_internals_inputs() -> dict[str, str]:
    """Provenance describing how a refactored ``internals.py`` would be built now."""
    from src.internals_cache import internals_key_provenance

    return internals_key_provenance()


def _internals_inputs_match(
    keys: PackageCacheKeys,
    expected: Mapping[str, str] | None,
) -> bool:
    """True when a sidecar's recorded provenance is present and matches ``expected``.

    A sidecar that carries an ``internals_key`` but no provenance is treated as
    unverifiable and refused: the whole point of the check is that adoption
    happens before clustering, so an unlabeled refactored module could have been
    produced under any model, schema, or ``excel-grapher`` version.
    """
    if expected is None:
        return True
    if keys.internals_inputs is None:
        return False
    return dict(keys.internals_inputs) == dict(expected)


def adopt_codegen_cache_from_dist(
    config: PipelineConfig,
    *,
    expected_codegen_key: str,
    projection_cache_key: str,
) -> bool:
    """Adopt pristine package modules into ``.cache/codegen/`` when keys match.

    Only safe when ``dist/`` still holds pristine ``internals.py`` (no
    ``internals_key`` in the sidecar). A refactored package must not be written
    into the codegen cache — that would poison the parity oracle.
    """
    keys = read_package_cache_keys(config.dist_root)
    if keys is None or keys.codegen_key != expected_codegen_key:
        return False
    if keys.internals_key is not None:
        return False
    modules = _read_package_modules(config.package_root)
    if modules is None:
        return False
    # Dist holds post-rewrite api.py and a memoization-patched runtime.py;
    # re-materialize re-applies both rewrites, which are idempotent for
    # already-rewritten sources.
    save_codegen_payload(
        modules,
        cache_key=expected_codegen_key,
        projection_cache_key=projection_cache_key,
    )
    return True


def adopt_internals_cache_from_dist(
    config: PipelineConfig,
    *,
    expected_codegen_key: str,
    internals_key: str,
) -> bool:
    """Copy committed ``dist`` internals into ``.cache/internals/`` when keys match."""
    keys = read_package_cache_keys(config.dist_root)
    if (
        keys is None
        or keys.codegen_key != expected_codegen_key
        or keys.internals_key != internals_key
    ):
        return False
    source_path = config.package_root / "internals.py"
    if not source_path.is_file():
        return False
    destination = internals_cache_path(internals_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    return True


def try_materialize_refactored_package_from_cache(
    config: PipelineConfig,
    *,
    codegen_key: str,
    expected_internals_key: str | None = None,
    expected_internals_inputs: Mapping[str, str] | None = None,
) -> bool:
    """Materialize dist from caches when a recorded ``internals_key`` is available.

    On a cold ``.cache/internals/`` but matching committed ``dist/`` keys, adopt
    the package module into the internals cache first. When the codegen cache is
    also cold, reconstruct non-oracle module texts from the committed package
    without writing them into ``.cache/codegen/`` (which must stay pristine).

    When ``expected_internals_key`` is provided, the sidecar ``internals_key`` must
    match it exactly — otherwise adoption is refused so a stale content key cannot
    skip Pass 1 / parity / Pass 2. Callers that reach this before clustering do
    not know the content key yet; they pass ``expected_internals_inputs``
    (see :func:`current_internals_inputs`) so adoption is still refused when the
    refactor model, schema versions, ``MECHANICAL_REFACTOR_BODIES``, or the
    ``excel-grapher`` version drifted from whatever produced the committed tree.

    Returns True when materialization succeeded and the refactor stage can skip.
    """
    keys = read_package_cache_keys(config.dist_root)
    if keys is None or keys.codegen_key != codegen_key or keys.internals_key is None:
        return False
    internals_key = keys.internals_key
    if expected_internals_key is not None and internals_key != expected_internals_key:
        return False
    if not _internals_inputs_match(keys, expected_internals_inputs):
        return False
    cache_path = internals_cache_path(internals_key)
    if not cache_path.is_file() and not adopt_internals_cache_from_dist(
        config,
        expected_codegen_key=codegen_key,
        internals_key=internals_key,
    ):
        return False

    modules = load_codegen_payload(codegen_key)
    if modules is None:
        modules = _read_package_modules(config.package_root)
        if modules is None:
            return False
        modules = apply_export_rewrites(modules)
        modules["internals.py"] = load_refactored_internals(internals_key)
        _write_dist_tree(config, modules)
        write_package_cache_keys(
            config.dist_root,
            PackageCacheKeys(
                codegen_key=codegen_key,
                internals_key=internals_key,
                # Preserve, do not restamp: this branch rebuilds the tree from
                # the committed package, so the module still has the provenance
                # it was committed with.
                internals_inputs=keys.internals_inputs,
            ),
        )
        return True

    materialize_package(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
    )
    return True
