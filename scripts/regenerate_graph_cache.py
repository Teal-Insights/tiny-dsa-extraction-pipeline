"""Regenerate the committed dependency-graph cache in ``.cache/dependency-graph``.

Warm pytest and CI runs can read this cache to skip cold graph builds. The cache
key fingerprints the workbook, bindings YAML, targets/constraints, and the
excel-grapher version. Rerun after changing the workbook, ``bindings/*.bindings.yaml``,
``workbook_config.py`` targets/constraints, or upgrading excel-grapher:

    uv run python -m scripts.regenerate_graph_cache

Use ``--force`` to rebuild even when current entries already exist. Commit the
updated ``.cache/dependency-graph`` artifacts when your downstream pipeline
chooses to vendor the cache (override ``.gitignore`` for that directory).
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

from src.graph_cache import (  # noqa: E402
    COMMITTED_GRAPH_CACHE_DIR,
    get_or_build_dependency_graph,
    prune_stale_graph_cache_entries,
)
from src.pipeline_config import (  # noqa: E402
    load_pipeline_config,
    validate_pipeline_config,
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

    current_keys: set[str] = set()
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
        print(
            f"{label}: key={result.cache_key[:12]} nodes={len(result.graph)} "
            f"cache_hit={result.cache_hit}"
        )

    for filename in prune_stale_graph_cache_entries(
        current_keys,
        cache_dir=COMMITTED_GRAPH_CACHE_DIR,
    ):
        print(f"pruned stale cache entry: {filename}")
    return current_keys


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate the committed dependency-graph cache."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild graphs even when the cache already has current entries.",
    )
    args = parser.parse_args(argv)
    regenerate_graph_cache(force=args.force)


if __name__ == "__main__":
    main()
