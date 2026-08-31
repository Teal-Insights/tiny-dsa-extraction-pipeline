"""Internal-binding coverage burn-down worklist.

Loads the cached dependency graph plus the binding sidecars, finds every formula
node not covered by input/output/internal bindings or the exemption list, and
groups the unbound addresses by ``(sheet, row)`` into contiguous column ranges.
Each printed range is one candidate ``layout: row_series`` entry; singletons are
``layout: scalar`` lookups or anchors.

Run: ``uv run python -m scripts.internal_binding_burndown``

The graph build itself does not read the binding YAML. When present, this script
prefers the fingerprint-matching cache entry; otherwise it falls back to the
newest cached graph pickle (with a stale-key warning) even if workbook or
targets have changed since that pickle was written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from excel_grapher.series_bindings import load_series_bindings

from src.graph_cache import load_pipeline_dependency_graph
from src.internal_binding_coverage import (
    find_unbound_internal_formula_cells_from_manifest,
    format_row_column_spans,
    group_unbound_cells_by_sheet_row,
    suggested_layout_for_row,
)
from src.pipeline_config import (
    load_pipeline_config,
    validate_pipeline_config,
)


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
    graph, _cache_key = load_pipeline_dependency_graph(config)
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
        for printed, row in enumerate(sorted(rows)):
            if args.max_rows is not None and printed >= args.max_rows:
                print("  ... (truncated)")
                break
            columns = rows[row]
            layout = suggested_layout_for_row(columns)
            spans = format_row_column_spans(sheet=sheet, row=row, columns=columns)
            print(f"  row {row}: {spans}  [{layout}]")


if __name__ == "__main__":
    main()
