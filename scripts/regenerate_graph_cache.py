"""Regenerate committed dependency-graph, series-resolution, and validation caches.

Warm pytest and CI runs can read these caches to skip cold graph builds,
``derive_*_series`` work, and ``validate_series_bindings``. Cache keys
fingerprint the workbook, bindings YAML, targets/constraints, and the
excel-grapher version. Rerun after changing the workbook,
``bindings/*.bindings.yaml``, ``workbook_config.py`` targets/constraints,
or upgrading excel-grapher:

    uv run python -m scripts.regenerate_graph_cache

Use ``--force`` to rebuild even when current entries already exist. Force also
clears ``.cache/series-resolution`` and ``.cache/bindings-validation`` before
rebuilding and pruning them to keys derived from the current graph cache keys.
Commit the updated ``.cache/dependency-graph`` artifacts when your downstream
pipeline chooses to vendor the cache (override ``.gitignore`` for that
directory).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from excel_grapher.grapher import DynamicRefConfig  # noqa: E402
from excel_grapher.series_bindings import load_series_bindings  # noqa: E402

from src.bindings_validation_cache import (  # noqa: E402
    COMMITTED_BINDINGS_VALIDATION_CACHE_DIR,
    bindings_validation_cache_key,
    clear_bindings_validation_cache,
    get_or_build_bindings_validation,
    prune_stale_bindings_validation_cache_entries,
)
from src.graph_cache import (  # noqa: E402
    COMMITTED_GRAPH_CACHE_DIR,
    get_or_build_dependency_graph,
    prune_stale_graph_cache_entries,
)
from src.pipeline_config import (  # noqa: E402
    load_pipeline_config,
    validate_pipeline_config,
)
from src.series_resolution_cache import (  # noqa: E402
    COMMITTED_SERIES_RESOLUTION_CACHE_DIR,
    clear_series_resolution_cache,
    prune_stale_series_resolution_cache_entries,
    series_resolution_cache_key,
)


def graph_cache_target_bundles(config) -> tuple[tuple[str, tuple[str, ...]], ...]:
    bundles: list[tuple[str, tuple[str, ...]]] = [
        ("default graph", config.targets),
    ]
    bundles.extend(config.graph_cache_target_bundles)
    return tuple(bundles)


def regenerate_graph_cache(
    *,
    force: bool = False,
) -> set[str]:
    config = load_pipeline_config()
    validate_pipeline_config(config)
    dynamic_refs = DynamicRefConfig.from_constraints(config.constraints, {})
    bindings = load_series_bindings(config.bindings_path)

    if force:
        clear_series_resolution_cache(
            cache_dir=COMMITTED_SERIES_RESOLUTION_CACHE_DIR,
        )
        clear_bindings_validation_cache(
            cache_dir=COMMITTED_BINDINGS_VALIDATION_CACHE_DIR,
        )

    current_keys: set[str] = set()
    default_graph_result = None
    for label, targets in graph_cache_target_bundles(config):
        result = get_or_build_dependency_graph(
            workbook_path=config.workbook_path,
            targets=targets,
            constraints=config.constraints,
            bindings_path=config.bindings_path,
            dynamic_refs=dynamic_refs,
            load_values=True,
            capture_dependency_provenance=True,
            cache_dir=COMMITTED_GRAPH_CACHE_DIR,
            force_rebuild=force,
        )
        current_keys.add(result.cache_key)
        if label == "default graph":
            default_graph_result = result
        print(
            f"{label}: key={result.cache_key[:12]} nodes={len(result.graph)} "
            f"cache_hit={result.cache_hit}"
        )

    for filename in prune_stale_graph_cache_entries(
        current_keys,
        cache_dir=COMMITTED_GRAPH_CACHE_DIR,
    ):
        print(f"pruned stale cache entry: {filename}")

    if default_graph_result is None:
        raise RuntimeError("default graph bundle did not run")

    validation_result = get_or_build_bindings_validation(
        default_graph_result.graph,
        bindings,
        workbook_path=config.workbook_path,
        graph_cache_key=default_graph_result.cache_key,
        cache_dir=COMMITTED_BINDINGS_VALIDATION_CACHE_DIR,
        force_rebuild=force,
    )
    print(
        "bindings validation: "
        f"key={validation_result.cache_key[:12]} "
        f"ok={validation_result.report['ok']} "
        f"issues={len(validation_result.report['issues'])} "
        f"cache_hit={validation_result.cache_hit}"
    )
    validation_keys = {
        bindings_validation_cache_key(graph_cache_key=cache_key)
        for cache_key in current_keys
    }
    for filename in prune_stale_bindings_validation_cache_entries(
        validation_keys,
        cache_dir=COMMITTED_BINDINGS_VALIDATION_CACHE_DIR,
    ):
        print(f"pruned stale bindings-validation cache entry: {filename}")

    series_keys = {
        series_resolution_cache_key(graph_cache_key=cache_key)
        for cache_key in current_keys
    }
    for filename in prune_stale_series_resolution_cache_entries(
        series_keys,
        cache_dir=COMMITTED_SERIES_RESOLUTION_CACHE_DIR,
    ):
        print(f"pruned stale series-resolution cache entry: {filename}")
    return current_keys


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate the committed dependency-graph, series-resolution, "
            "and bindings-validation caches."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rebuild graphs/validation even when the cache already has current entries."
        ),
    )
    args = parser.parse_args(argv)
    regenerate_graph_cache(force=args.force)


if __name__ == "__main__":
    main()
