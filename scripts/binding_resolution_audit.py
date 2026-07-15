"""CLI for series-binding resolution audit (codegen-fatal issues).

Loads the cached dependency graph plus binding sidecars, resolves input/output/
internal bindings the same way codegen does, and prints Tier-1 failures:
bind-resolution errors, empty public series, partial bind failures, sparse
``column_header`` / ``row_label`` spans without ``fill: true``, and internal
formula cells claimed by more than one internal series.

Run: ``uv run python -m scripts.binding_resolution_audit``

Uses ``load_graph`` from ``internal_binding_burndown``: prefers the
fingerprint-matching cache entry when present; otherwise falls back to the
newest cached graph pickle (with a stale-key warning).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from excel_grapher.series_bindings import load_series_bindings  # noqa: E402
from excel_grapher.series_bindings.resolve import BindingDirection  # noqa: E402

from scripts.internal_binding_burndown import load_graph  # noqa: E402
from src.binding_resolution_audit import (  # noqa: E402
    DIRECTIONS,
    audit_binding_resolutions,
    format_audit_findings,
)
from src.pipeline_config import (  # noqa: E402
    load_pipeline_config,
    validate_pipeline_config,
)


def _parse_directions(raw: list[str] | None) -> tuple[BindingDirection, ...]:
    if not raw:
        return DIRECTIONS
    allowed: set[str] = set(DIRECTIONS)
    parsed: list[BindingDirection] = []
    for item in raw:
        if item not in allowed:
            raise SystemExit(
                f"Unknown direction {item!r}; expected one of {sorted(allowed)}"
            )
        direction = cast(BindingDirection, item)
        if direction not in parsed:
            parsed.append(direction)
    return tuple(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--direction",
        action="append",
        choices=list(DIRECTIONS),
        help="Limit to one direction (repeatable). Default: all.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as fatal (exit 1 when any warning is present).",
    )
    args = parser.parse_args(argv)

    config = load_pipeline_config()
    validate_pipeline_config(config)
    graph, _cache_key = load_graph(config)
    bindings = load_series_bindings(config.bindings_path)
    directions = _parse_directions(args.direction)

    report = audit_binding_resolutions(
        graph,
        bindings,
        workbook=config.workbook_path,
        directions=directions,
    )

    print(
        f"Binding resolution audit: {report.error_count} error(s), "
        f"{report.warning_count} warning(s) "
        f"(directions={','.join(directions)})"
    )
    lines = format_audit_findings(report.findings)
    if lines:
        print()
        print("\n".join(lines))
    else:
        print("No resolution issues found.")

    if not report.ok:
        return 1
    if args.strict and report.warning_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
