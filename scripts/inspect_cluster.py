"""Inspect one fingerprint-family cluster from warm graph/projection caches.

Rebuilds clustering without running export/refactor. Useful after a mechanical
refactor failure when you need member addresses, formulas, schedule peels, and
optional cell_* sources without a full pipeline run.

Usage:
    uv run -m scripts.inspect_cluster --cluster-id 365
    uv run -m scripts.inspect_cluster --cluster-id 365 --schedule --sources
    uv run -m scripts.inspect_cluster --cluster-id 365 --internals path/to/internals.py
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.extraction_pipeline import build_pipeline_graph  # noqa: E402
from src.formula_clustering import FormulaCluster, cluster_graph_formulas  # noqa: E402
from src.internals_refactor import (  # noqa: E402
    InternalsSourceIndex,
    address_to_function_name,
)
from src.pipeline_config import (  # noqa: E402
    add_clustering_mode_argument,
    add_variation_mode_argument,
    apply_clustering_mode_cli_override,
    apply_variation_mode_cli_override,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.pipeline_context import activate_pipeline_config  # noqa: E402
from src.refactor_bindings import (  # noqa: E402
    build_address_to_series_id,
    build_bound_address_keys,
)
from src.refactor_order import (  # noqa: E402
    RefactorUnit,
    compute_refactor_schedule,
    refactor_failure_target,
)
from src.subgraph_projection import build_refactor_projection  # noqa: E402


def find_cluster(
    clusters: Sequence[FormulaCluster], cluster_id: int
) -> FormulaCluster | None:
    for cluster in clusters:
        if cluster.cluster_id == cluster_id:
            return cluster
    return None


def schedule_units_for_family(
    units: Sequence[RefactorUnit], parent_cluster_id: int
) -> tuple[RefactorUnit, ...]:
    return tuple(unit for unit in units if unit.parent_cluster_id == parent_cluster_id)


def _member_sources(
    *,
    members: Sequence[str],
    internals_source: str | None,
) -> dict[str, str]:
    if internals_source is None:
        return {}
    index = InternalsSourceIndex.from_source(internals_source)
    sources: dict[str, str] = {}
    for address in members:
        function_name = address_to_function_name(address)
        try:
            sources[address] = index.function_source(function_name)
        except KeyError:
            sources[address] = f"# missing function {function_name!r}\n"
    return sources


def format_cluster_report(
    cluster: FormulaCluster,
    *,
    address_to_series_id: Mapping[str, str],
    formulas: Mapping[str, str],
    schedule_units: Sequence[RefactorUnit] = (),
    sources: Mapping[str, str] | None = None,
) -> str:
    member_sources = sources if sources is not None else {}
    lines: list[str] = [
        f"cluster_id: {cluster.cluster_id}",
        f"members: {len(cluster.members)}",
        f"row: {cluster.row}",
        f"canonical_template: {cluster.canonical_template}",
        "",
        "members:",
    ]
    for index, address in enumerate(cluster.members):
        series_id = address_to_series_id.get(address, "<unbound>")
        formula = formulas.get(address, "<missing formula>")
        function_name = address_to_function_name(address)
        lines.append(f"  [{index}] {address}  series={series_id}  fn={function_name}")
        lines.append(f"      formula: {formula}")
        source = member_sources.get(address)
        if source is not None:
            indented = "\n".join(
                f"      | {line}" if line else "      |"
                for line in source.rstrip("\n").splitlines()
            )
            lines.append("      source:")
            lines.append(indented)

    if schedule_units:
        lines.append("")
        lines.append(f"schedule units: {len(schedule_units)}")
        for unit in schedule_units:
            target = refactor_failure_target(unit)
            lines.append(
                f"  {target}  members={len(unit.members)}  [{', '.join(unit.members)}]"
            )
    return "\n".join(lines) + "\n"


def _formulas_for_members(projection, members: Sequence[str]) -> dict[str, str]:
    formulas: dict[str, str] = {}
    for address in members:
        node = projection.get_node(address)
        if node is None or node.normalized_formula is None:
            continue
        formulas[address] = node.normalized_formula
    return formulas


def _resolve_internals_source(
    *,
    internals_path: Path | None,
    package_root: Path,
) -> str | None:
    path = (
        internals_path if internals_path is not None else package_root / "internals.py"
    )
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect one fingerprint-family cluster using warm graph/projection "
            "caches (no export/refactor)."
        )
    )
    parser.add_argument(
        "--cluster-id",
        type=int,
        required=True,
        help="Fingerprint-family cluster_id (as logged during refactor).",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Also print schedule peels for this family.",
    )
    parser.add_argument(
        "--sources",
        action="store_true",
        help=(
            "Include cell_* python sources from package internals.py (or --internals)."
        ),
    )
    parser.add_argument(
        "--internals",
        type=Path,
        default=None,
        help="Optional internals.py path for --sources (default: package internals).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk graph/projection caches for this run.",
    )
    add_variation_mode_argument(parser)
    add_clustering_mode_argument(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = apply_clustering_mode_cli_override(
        apply_variation_mode_cli_override(load_pipeline_config(), args.variation_mode),
        args.clustering_mode,
    )
    validate_pipeline_config(config)
    activate_pipeline_config(config)

    graph_result = build_pipeline_graph(config, no_cache=args.no_cache)
    projection = build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.graph_cache_key,
        no_cache=args.no_cache,
    )
    bound_address_keys = build_bound_address_keys(
        graph_result.input_series,
        graph_result.output_series,
        graph_result.internal_series,
    )
    address_to_series_id = build_address_to_series_id(
        graph_result.internal_series,
        output_series=graph_result.output_series,
        input_series=graph_result.input_series,
    )
    clusters = cluster_graph_formulas(
        projection,
        bound_address_keys=bound_address_keys,
        variation_mode=config.variation_mode,
        clustering_mode=config.clustering_mode,
        address_to_series_id=address_to_series_id,
        workbook_path=config.workbook_path,
        layout=config.projection_layout,
    )

    cluster = find_cluster(clusters, args.cluster_id)
    if cluster is None:
        available = ", ".join(str(item.cluster_id) for item in clusters[:20])
        suffix = "…" if len(clusters) > 20 else ""
        print(
            f"cluster_id {args.cluster_id} not found "
            f"({len(clusters)} families; sample: {available}{suffix})",
            file=sys.stderr,
        )
        return 1

    schedule_units: tuple[RefactorUnit, ...] = ()
    if args.schedule:
        units = compute_refactor_schedule(projection, clusters)
        schedule_units = schedule_units_for_family(units, args.cluster_id)

    sources: dict[str, str] = {}
    if args.sources:
        internals_source = _resolve_internals_source(
            internals_path=args.internals,
            package_root=config.package_root,
        )
        if internals_source is None:
            path = (
                args.internals
                if args.internals is not None
                else config.package_root / "internals.py"
            )
            print(
                f"warning: internals not found at {path}; omitting sources", flush=True
            )
        else:
            sources = _member_sources(
                members=cluster.members, internals_source=internals_source
            )

    print(f"workbook: {config.workbook_path}")
    print(
        f"clustering_mode={config.clustering_mode!r} "
        f"variation_mode={config.variation_mode!r}"
    )
    print(f"fingerprint families: {len(clusters)}")
    print()
    print(
        format_cluster_report(
            cluster,
            address_to_series_id=address_to_series_id,
            formulas=_formulas_for_members(projection, cluster.members),
            schedule_units=schedule_units,
            sources=sources,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
