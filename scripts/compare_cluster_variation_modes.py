"""Compare formula clustering across variation_mode settings.

Run: uv run -m scripts.compare_cluster_variation_modes
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.extraction_pipeline import build_pipeline_graph  # noqa: E402
from src.formula_clustering import (  # noqa: E402
    BoundAddressKeys,
    ClusteringMode,
    FormulaCluster,
    VariationMode,
    cluster_graph_formulas,
    format_structural_skeleton,
    structural_fingerprint,
)
from src.workbook_addresses import ProjectionColumnLayout  # noqa: E402
from src.pipeline_config import load_pipeline_config, validate_pipeline_config  # noqa: E402
from src.pipeline_context import activate_pipeline_config  # noqa: E402
from src.refactor_bindings import (  # noqa: E402
    build_address_to_series_id,
    build_bound_address_keys,
)
from src.refactor_order import compute_refactor_schedule_with_diagnostics  # noqa: E402
from src.subgraph_projection import build_refactor_projection  # noqa: E402

IncludeSection = Literal["changes", "members", "fingerprints"]
INCLUDE_SECTIONS: tuple[IncludeSection, ...] = ("changes", "members", "fingerprints")


def _cluster_by_mode(
    *,
    variation_mode: VariationMode,
    bound_address_keys: BoundAddressKeys,
    address_to_series_id: dict[str, str],
    clustering_mode: ClusteringMode,
    projection,
    workbook_path,
    layout,
) -> tuple[FormulaCluster, ...]:
    return cluster_graph_formulas(
        projection,
        bound_address_keys=bound_address_keys,
        variation_mode=variation_mode,
        clustering_mode=clustering_mode,
        address_to_series_id=address_to_series_id,
        workbook_path=workbook_path,
        layout=layout,
    )


def _member_to_bucket(
    clusters: tuple[FormulaCluster, ...],
) -> dict[str, int]:
    return {
        member: cluster.cluster_id for cluster in clusters for member in cluster.members
    }


def _format_cluster(cluster: FormulaCluster) -> str:
    members = ", ".join(cluster.members)
    template = cluster.canonical_template
    if len(template) > 72:
        template = f"{template[:69]}..."
    return (
        f"  cluster {cluster.cluster_id} "
        f"({len(cluster.members)} member{'s' if len(cluster.members) != 1 else ''})\n"
        f"    members: {members}\n"
        f"    template: {template}"
    )


def _series_count_for_clusters(
    clusters: tuple[FormulaCluster, ...],
    address_to_series_id: dict[str, str],
) -> int:
    series_ids = {
        address_to_series_id[member]
        for cluster in clusters
        for member in cluster.members
        if member in address_to_series_id
    }
    return len(series_ids)


def _print_mode_report(
    *,
    label: str,
    clusters: tuple[FormulaCluster, ...],
    projection,
    address_to_series_id: dict[str, str],
    include: Collection[str],
) -> None:
    multi_member = sum(1 for cluster in clusters if len(cluster.members) > 1)
    singletons = len(clusters) - multi_member
    member_total = sum(len(cluster.members) for cluster in clusters)
    series_count = _series_count_for_clusters(clusters, address_to_series_id)
    family_count = len(clusters)
    schedule_units, report = compute_refactor_schedule_with_diagnostics(
        projection,
        clusters,
    )
    slice_count = len(schedule_units)
    singleton_slices = sum(1 for unit in schedule_units if len(unit.members) == 1)

    print(f"=== {label} ===")
    print(
        f"series: {series_count}; "
        f"series-fingerprint families: {family_count} "
        f"({multi_member} multi-member, {singletons} singleton); "
        f"scheduled slices: {slice_count} "
        f"({slice_count - singleton_slices} multi-member, {singleton_slices} singleton)"
    )
    print(
        f"schedule path={report.path}; "
        f"emits: dag={report.dag_emits} "
        f"whole_family={report.whole_family_emits} peel={report.peel_emits}"
    )
    print(f"member cells: {member_total}")
    sizes = sorted((len(cluster.members) for cluster in clusters), reverse=True)
    if sizes:
        top = ", ".join(str(size) for size in sizes[:5])
        print(f"largest families by member count: {top}")
    worst = report.families_by_unit_count[:5]
    if worst:
        worst_bits = ", ".join(
            f"family {stats.parent_cluster_id}→{stats.unit_count}u"
            f"/{stats.member_count}m"
            for stats in worst
        )
        print(f"worst family slice fan-out: {worst_bits}")
    print()
    if "members" not in include:
        return
    for cluster in clusters:
        print(_format_cluster(cluster))
        print()


def _print_differences(
    *,
    left_label: str,
    left_clusters: tuple[FormulaCluster, ...],
    right_label: str,
    right_clusters: tuple[FormulaCluster, ...],
    include: Collection[str],
) -> None:
    left_map = _member_to_bucket(left_clusters)
    right_map = _member_to_bucket(right_clusters)
    all_members = sorted(set(left_map) | set(right_map))
    include_changes = "changes" in include

    changed: list[str] = []
    for member in all_members:
        if left_map.get(member) != right_map.get(member):
            changed.append(member)

    print("=== differences ===")
    if not changed:
        print("No cell moved between buckets.")
        print()
        return

    print(f"{len(changed)} cell(s) belong to different buckets.")
    print()

    if include_changes:
        for member in changed:
            left_bucket = left_map.get(member)
            right_bucket = right_map.get(member)
            print(
                f"  {member}: {left_label} bucket {left_bucket} "
                f"-> {right_label} bucket {right_bucket}"
            )
        print()

    left_groups: dict[int, list[str]] = defaultdict(list)
    for member in changed:
        left_groups[left_map[member]].append(member)

    print("Merged/split summary:")
    for left_id, members in sorted(left_groups.items()):
        right_ids = sorted({right_map[member] for member in members})
        if len(right_ids) == 1 and len(members) > 1:
            print(
                f"  {left_label} bucket {left_id} "
                f"({len(members)} members) maps entirely to "
                f"{right_label} bucket {right_ids[0]}"
            )
        elif len(members) == 1:
            member = members[0]
            if include_changes:
                print(
                    f"  {member} alone in {left_label} bucket {left_id}, "
                    f"joins {right_label} bucket {right_ids}"
                )
            else:
                print(
                    f"  {left_label} bucket {left_id} (1 member) joins "
                    f"{right_label} bucket {right_ids}"
                )
        elif include_changes:
            print(
                f"  {left_label} bucket {left_id} members {members} "
                f"split across {right_label} buckets {right_ids}"
            )
        else:
            print(
                f"  {left_label} bucket {left_id} "
                f"({len(members)} members) split across "
                f"{right_label} buckets {right_ids}"
            )
    print()


def _print_fingerprints(
    *,
    label: str,
    clusters: tuple[FormulaCluster, ...],
    bound_address_keys: BoundAddressKeys,
    workbook_path: Path,
    layout: ProjectionColumnLayout | None,
    include: Collection[str],
) -> None:
    if "fingerprints" not in include:
        return

    print(f"=== fingerprints ({label}) ===")
    unique_skeletons: set[tuple] = set()
    incomplete = 0
    for cluster in clusters:
        fingerprint = structural_fingerprint(
            cluster.canonical_template,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        member_count = len(cluster.members)
        if fingerprint is None:
            incomplete += 1
            print(
                f"  cluster {cluster.cluster_id} "
                f"({member_count} member{'s' if member_count != 1 else ''}): "
                "unparseable"
            )
            print()
            continue

        skeleton, _refs = fingerprint
        unique_skeletons.add(skeleton)
        print(
            f"  cluster {cluster.cluster_id} "
            f"({member_count} member{'s' if member_count != 1 else ''})"
        )
        print(f"    {format_structural_skeleton(skeleton)}")
        print()

    print(
        f"unique skeletons: {len(unique_skeletons)}; unparseable clusters: {incomplete}"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare formula buckets for independent vs dominant_key_only "
            "variation_mode on the configured workbook."
        )
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk graph and projection caches for this run.",
    )
    parser.add_argument(
        "--include",
        nargs="+",
        choices=INCLUDE_SECTIONS,
        default=[],
        metavar="SECTION",
        help=(
            "Optional detail sections to print (default: quiet summary only). "
            "Available: changes (per-cell membership diffs), "
            "members (per-bucket member lists), "
            "fingerprints (per-bucket formula-like skeleton with literal scalars and ref_N[dims]). "
            "Example: --include changes fingerprints"
        ),
    )
    args = parser.parse_args()
    include: Sequence[str] = args.include

    config = load_pipeline_config()
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
    layout = config.projection_layout

    independent = _cluster_by_mode(
        variation_mode="independent",
        bound_address_keys=bound_address_keys,
        address_to_series_id=address_to_series_id,
        clustering_mode=config.clustering_mode,
        projection=projection,
        workbook_path=config.workbook_path,
        layout=layout,
    )
    dominant_key_only = _cluster_by_mode(
        variation_mode="dominant_key_only",
        bound_address_keys=bound_address_keys,
        address_to_series_id=address_to_series_id,
        clustering_mode=config.clustering_mode,
        projection=projection,
        workbook_path=config.workbook_path,
        layout=layout,
    )

    source_formula_count = len(graph_result.graph.formula_keys())
    projected_formula_count = len(projection.formula_keys())
    print(f"workbook: {config.workbook_path}")
    print(
        "graph formulas: "
        f"{source_formula_count} before OptimalCompression, "
        f"{projected_formula_count} after "
        f"({source_formula_count - projected_formula_count} collapsed); "
        f"{len(projection.projected_graph)} total projected nodes "
        f"({len(projection.leaf_keys())} leaves)"
    )
    print(
        f"clustering_mode={config.clustering_mode!r} "
        "(compares variation_mode only; scheduled slices use compute_refactor_schedule)"
    )
    print()

    _print_mode_report(
        label='variation_mode="independent"',
        clusters=independent,
        projection=projection,
        address_to_series_id=address_to_series_id,
        include=include,
    )
    _print_mode_report(
        label='variation_mode="dominant_key_only"',
        clusters=dominant_key_only,
        projection=projection,
        address_to_series_id=address_to_series_id,
        include=include,
    )
    _print_differences(
        left_label="independent",
        left_clusters=independent,
        right_label="dominant_key_only",
        right_clusters=dominant_key_only,
        include=include,
    )
    _print_fingerprints(
        label='variation_mode="independent"',
        clusters=independent,
        bound_address_keys=bound_address_keys,
        workbook_path=config.workbook_path,
        layout=layout,
        include=include,
    )
    _print_fingerprints(
        label='variation_mode="dominant_key_only"',
        clusters=dominant_key_only,
        bound_address_keys=bound_address_keys,
        workbook_path=config.workbook_path,
        layout=layout,
        include=include,
    )


if __name__ == "__main__":
    main()
