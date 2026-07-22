"""Internal-binding coverage burn-down worklist.

Loads the cached dependency graph plus the binding sidecars, finds every formula
node not covered by input/output/internal bindings or the exemption list, and
groups the unbound addresses by ``(sheet, row)`` into contiguous column ranges.
Each printed range is one candidate ``layout: row_series`` entry; singletons are
``layout: scalar`` lookups or anchors.

Run: ``uv run python -m scripts.internal_binding_burndown``

The graph build itself does not read the binding YAML. When present, this script
prefers the fingerprint-matching cache entry; otherwise it falls back to the
newest cached graph pickle (with a stale-key warning) even if the bindings
fingerprint has changed since that pickle was written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from excel_grapher.grapher import DependencyGraph, DynamicRefConfig  # noqa: E402
from excel_grapher.series_bindings import load_series_bindings  # noqa: E402

from src.graph_cache import (  # noqa: E402
    DEFAULT_GRAPH_CACHE_DIR,
    dependency_graph_cache_key,
    get_or_build_dependency_graph,
    load_dependency_graph,
    load_newest_cached_dependency_graph,
)
from src.internal_binding_coverage import (  # noqa: E402
    find_unbound_internal_formula_cells_from_manifest,
    format_row_column_spans,
    group_unbound_cells_by_sheet_row,
    suggested_layout_for_row,
)
from src.pipeline_config import (  # noqa: E402
    PipelineConfig,
    load_pipeline_config,
    validate_pipeline_config,
)


def _expected_graph_cache_key(config: PipelineConfig) -> str:
    return dependency_graph_cache_key(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        bindings_path=config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )


def _warn_if_cached_graph_is_stale(config: PipelineConfig, cache_key: str) -> None:
    expected_key = _expected_graph_cache_key(config)
    if cache_key == expected_key:
        return
    print(
        "Warning: newest cached graph key does not match the current workbook, "
        "bindings, or targets fingerprint. Burndown results may be stale; run "
        "uv run python -m scripts.regenerate_graph_cache to refresh."
    )


def load_graph(config: PipelineConfig) -> tuple[DependencyGraph, str | None]:
    expected_key = _expected_graph_cache_key(config)
    matched = load_dependency_graph(expected_key, cache_dir=DEFAULT_GRAPH_CACHE_DIR)
    if matched is not None:
        print(
            f"Loaded cached graph key={expected_key[:12]} "
            f"from {DEFAULT_GRAPH_CACHE_DIR} ({len(matched)} nodes)"
        )
        return matched, expected_key

    cached = load_newest_cached_dependency_graph(cache_dir=DEFAULT_GRAPH_CACHE_DIR)
    if cached is not None:
        graph, cache_key = cached
        print(
            f"Loaded cached graph key={cache_key[:12]} "
            f"from {DEFAULT_GRAPH_CACHE_DIR} ({len(graph)} nodes)"
        )
        _warn_if_cached_graph_is_stale(config, cache_key)
        return graph, cache_key

    dynamic_ref_config = DynamicRefConfig.from_constraints(config.constraints, {})
    result = get_or_build_dependency_graph(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        bindings_path=config.bindings_path,
        dynamic_refs=dynamic_ref_config,
        load_values=True,
        capture_dependency_provenance=True,
    )
    print(f"Built graph key={result.cache_key[:12]} ({len(result.graph)} nodes)")
    return result.graph, result.cache_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-sheet",
        default=None,
        help="Only print row detail for this sheet (summary always prints).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit row detail lines per sheet.",
    )
    args = parser.parse_args()

    config = load_pipeline_config()
    validate_pipeline_config(config)
    graph, _cache_key = load_graph(config)
    bindings = load_series_bindings(config.bindings_path)

    unbound = find_unbound_internal_formula_cells_from_manifest(
        graph=graph,
        bindings=bindings,
        exempt_cells=config.internal_binding_exempt_cells,
        workbook=config.workbook_path,
    )
    formula_count = len(list(graph.formula_keys()))
    print(f"Formula nodes: {formula_count}")
    print(f"Unbound internal formula cells: {len(unbound)}")

    grouped = group_unbound_cells_by_sheet_row(unbound)
    print("\nUnbound cells by sheet:")
    for sheet in sorted(
        grouped,
        key=lambda name: -sum(len(columns) for columns in grouped[name].values()),
    ):
        count = sum(len(columns) for columns in grouped[sheet].values())
        print(f"  {sheet}: {count} cells across {len(grouped[sheet])} rows")

    for sheet, rows in sorted(grouped.items()):
        if args.per_sheet is not None and sheet != args.per_sheet:
            continue
        print(f"\n== {sheet} ==")
        printed = 0
        for row in sorted(rows):
            if args.max_rows is not None and printed >= args.max_rows:
                print("  ... (truncated)")
                break
            columns = rows[row]
            layout = suggested_layout_for_row(columns)
            spans = format_row_column_spans(sheet=sheet, row=row, columns=columns)
            print(f"  row {row}: {spans}  [{layout}]")
            printed += 1


if __name__ == "__main__":
    main()
