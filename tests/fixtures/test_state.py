"""Reset shared pipeline module state between tests."""

from __future__ import annotations

import sys
from pathlib import Path

_PROBE_MODULE_NAMES = (
    "_runtime_symbols_probe",
    "_exported_runtime_for_parity",
    "_exported_data_for_parity",
    "tests.differential.differential_test_exported_library",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_GRAPH_CACHE_DIR = _REPO_ROOT / ".cache" / "dependency-graph"
REPO_PROJECTION_CACHE_DIR = _REPO_ROOT / ".cache" / "projection"

_ORIGINAL_GRAPH_CACHE_DIR: Path | None = None
_ORIGINAL_PROJECTION_CACHE_DIR: Path | None = None


def redirect_pipeline_disk_cache(root: Path) -> None:
    """Point graph/projection disk caches at an isolated directory."""
    global _ORIGINAL_GRAPH_CACHE_DIR, _ORIGINAL_PROJECTION_CACHE_DIR

    import src.graph_cache as graph_cache
    import src.projection_cache as projection_cache

    if _ORIGINAL_GRAPH_CACHE_DIR is None:
        _ORIGINAL_GRAPH_CACHE_DIR = graph_cache.DEFAULT_GRAPH_CACHE_DIR
        _ORIGINAL_PROJECTION_CACHE_DIR = projection_cache.DEFAULT_PROJECTION_CACHE_DIR

    graph_cache.DEFAULT_GRAPH_CACHE_DIR = root / "dependency-graph"
    projection_cache.DEFAULT_PROJECTION_CACHE_DIR = root / "projection"


def restore_pipeline_disk_cache() -> None:
    """Restore graph/projection disk cache directories after pytest."""
    global _ORIGINAL_GRAPH_CACHE_DIR, _ORIGINAL_PROJECTION_CACHE_DIR

    original_graph = _ORIGINAL_GRAPH_CACHE_DIR
    original_projection = _ORIGINAL_PROJECTION_CACHE_DIR
    if original_graph is None or original_projection is None:
        return

    import src.graph_cache as graph_cache
    import src.projection_cache as projection_cache

    graph_cache.DEFAULT_GRAPH_CACHE_DIR = original_graph
    projection_cache.DEFAULT_PROJECTION_CACHE_DIR = original_projection
    _ORIGINAL_GRAPH_CACHE_DIR = None
    _ORIGINAL_PROJECTION_CACHE_DIR = None


def clear_runtime_caches() -> None:
    from src.runtime_symbols import allowed_runtime_symbols

    allowed_runtime_symbols.cache_clear()
    try:
        from src.refactor_parity_gate import _dist_data, _runtime

        _runtime.cache_clear()
        _dist_data.cache_clear()
    except ImportError:
        pass


def reset_pipeline_test_state() -> None:
    from src.pipeline_context import reset_pipeline_config

    reset_pipeline_config()
    clear_runtime_caches()
    for module_name in _PROBE_MODULE_NAMES:
        sys.modules.pop(module_name, None)
