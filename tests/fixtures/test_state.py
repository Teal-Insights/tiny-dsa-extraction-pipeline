"""Reset shared pipeline module state between tests."""

from __future__ import annotations

import sys
from pathlib import Path

_PROBE_MODULE_NAMES = ("tests.differential.differential_test_exported_library",)

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_GRAPH_CACHE_DIR = _REPO_ROOT / ".cache" / "dependency-graph"
REPO_PROJECTION_CACHE_DIR = _REPO_ROOT / ".cache" / "projection"
REPO_SERIES_RESOLUTION_CACHE_DIR = _REPO_ROOT / ".cache" / "series-resolution"
REPO_SERIES_DERIVED_CACHE_DIR = _REPO_ROOT / ".cache" / "series-derived"
REPO_BINDINGS_VALIDATION_CACHE_DIR = _REPO_ROOT / ".cache" / "bindings-validation"
REPO_CODEGEN_CACHE_DIR = _REPO_ROOT / ".cache" / "codegen"

_ORIGINAL_GRAPH_CACHE_DIR: Path | None = None
_ORIGINAL_PROJECTION_CACHE_DIR: Path | None = None
_ORIGINAL_SERIES_RESOLUTION_CACHE_DIR: Path | None = None
_ORIGINAL_SERIES_DERIVED_CACHE_DIR: Path | None = None
_ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR: Path | None = None
_ORIGINAL_CODEGEN_CACHE_DIR: Path | None = None


def redirect_pipeline_disk_cache(root: Path) -> None:
    """Point pipeline disk caches at an isolated directory."""
    global _ORIGINAL_GRAPH_CACHE_DIR
    global _ORIGINAL_PROJECTION_CACHE_DIR
    global _ORIGINAL_SERIES_RESOLUTION_CACHE_DIR
    global _ORIGINAL_SERIES_DERIVED_CACHE_DIR
    global _ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR
    global _ORIGINAL_CODEGEN_CACHE_DIR

    from src import (
        bindings_validation_cache,
        codegen_cache,
        graph_cache,
        projection_cache,
        series_derived_cache,
        series_resolution_cache,
    )

    if _ORIGINAL_GRAPH_CACHE_DIR is None:
        _ORIGINAL_GRAPH_CACHE_DIR = graph_cache.DEFAULT_GRAPH_CACHE_DIR
        _ORIGINAL_PROJECTION_CACHE_DIR = projection_cache.DEFAULT_PROJECTION_CACHE_DIR
        _ORIGINAL_SERIES_RESOLUTION_CACHE_DIR = (
            series_resolution_cache.DEFAULT_SERIES_RESOLUTION_CACHE_DIR
        )
        _ORIGINAL_SERIES_DERIVED_CACHE_DIR = (
            series_derived_cache.DEFAULT_SERIES_DERIVED_CACHE_DIR
        )
        _ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR = (
            bindings_validation_cache.DEFAULT_BINDINGS_VALIDATION_CACHE_DIR
        )
        _ORIGINAL_CODEGEN_CACHE_DIR = codegen_cache.DEFAULT_CODEGEN_CACHE_DIR

    graph_cache.DEFAULT_GRAPH_CACHE_DIR = root / "dependency-graph"
    projection_cache.DEFAULT_PROJECTION_CACHE_DIR = root / "projection"
    series_resolution_cache.DEFAULT_SERIES_RESOLUTION_CACHE_DIR = (
        root / "series-resolution"
    )
    series_derived_cache.DEFAULT_SERIES_DERIVED_CACHE_DIR = root / "series-derived"
    bindings_validation_cache.DEFAULT_BINDINGS_VALIDATION_CACHE_DIR = (
        root / "bindings-validation"
    )
    codegen_cache.DEFAULT_CODEGEN_CACHE_DIR = root / "codegen"


def restore_pipeline_disk_cache() -> None:
    """Restore pipeline disk cache directories after pytest."""
    global _ORIGINAL_GRAPH_CACHE_DIR
    global _ORIGINAL_PROJECTION_CACHE_DIR
    global _ORIGINAL_SERIES_RESOLUTION_CACHE_DIR
    global _ORIGINAL_SERIES_DERIVED_CACHE_DIR
    global _ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR
    global _ORIGINAL_CODEGEN_CACHE_DIR

    original_graph = _ORIGINAL_GRAPH_CACHE_DIR
    original_projection = _ORIGINAL_PROJECTION_CACHE_DIR
    original_series = _ORIGINAL_SERIES_RESOLUTION_CACHE_DIR
    original_derived = _ORIGINAL_SERIES_DERIVED_CACHE_DIR
    original_validation = _ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR
    original_codegen = _ORIGINAL_CODEGEN_CACHE_DIR
    if (
        original_graph is None
        or original_projection is None
        or original_series is None
        or original_derived is None
        or original_validation is None
        or original_codegen is None
    ):
        return

    from src import (
        bindings_validation_cache,
        codegen_cache,
        graph_cache,
        projection_cache,
        series_derived_cache,
        series_resolution_cache,
    )

    graph_cache.DEFAULT_GRAPH_CACHE_DIR = original_graph
    projection_cache.DEFAULT_PROJECTION_CACHE_DIR = original_projection
    series_resolution_cache.DEFAULT_SERIES_RESOLUTION_CACHE_DIR = original_series
    series_derived_cache.DEFAULT_SERIES_DERIVED_CACHE_DIR = original_derived
    bindings_validation_cache.DEFAULT_BINDINGS_VALIDATION_CACHE_DIR = (
        original_validation
    )
    codegen_cache.DEFAULT_CODEGEN_CACHE_DIR = original_codegen
    _ORIGINAL_GRAPH_CACHE_DIR = None
    _ORIGINAL_PROJECTION_CACHE_DIR = None
    _ORIGINAL_SERIES_RESOLUTION_CACHE_DIR = None
    _ORIGINAL_SERIES_DERIVED_CACHE_DIR = None
    _ORIGINAL_BINDINGS_VALIDATION_CACHE_DIR = None
    _ORIGINAL_CODEGEN_CACHE_DIR = None


def reset_pipeline_test_state() -> None:
    for module_name in _PROBE_MODULE_NAMES:
        sys.modules.pop(module_name, None)
