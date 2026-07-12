"""Compare formula clustering across variation_mode settings."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.extraction_pipeline import build_pipeline_graph  # noqa: E402
from src.formula_clustering import (  # noqa: E402
    BoundAddressKeys,
    FormulaCluster,
    VariationMode,
    cluster_graph_formulas,
)
from src.pipeline_config import load_pipeline_config, validate_pipeline_config  # noqa: E402
from src.pipeline_context import activate_pipeline_config  # noqa: E402
from src.refactor_bindings import build_bound_address_keys  # noqa: E402
from src.subgraph_projection import build_refactor_projection  # noqa: E402


def _cluster_by_mode(
    *,
    variation_mode: VariationMode,
    bound_address_keys: BoundAddressKeys,
    projection,
    workbook_path,
    layout,
) -> tuple[FormulaCluster, ...]:
    return cluster_graph_formulas(
        projection,
        bound_address_keys=bound_address_keys,
        variation_mode=variation_mode,
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
        f"  bucket {cluster.cluster_id} "
        f"({len(cluster.members)} member{'s' if len(cluster.members) != 1 else ''})\n"
        f"    members: {members}\n"
        f"    template: {template}"
    )


def _print_mode_report(
    *,
    label: str,
    clusters: tuple[FormulaCluster, ...],
) -> None:
    multi_member = sum(1 for cluster in clusters if len(cluster.members) > 1)
    singletons = len(clusters) - multi_member
    print(f"=== {label} ===")
    print(
        f"buckets: {len(clusters)} total "
        f"({multi_member} multi-member, {singletons} singleton)"
    )
    print()
    for cluster in clusters:
        print(_format_cluster(cluster))
        print()


def _print_differences(
    *,
    left_label: str,
    left_clusters: tuple[FormulaCluster, ...],
    right_label: str,
    right_clusters: tuple[FormulaCluster, ...],
) -> None:
    left_map = _member_to_bucket(left_clusters)
    right_map = _member_to_bucket(right_clusters)
    all_members = sorted(set(left_map) | set(right_map))

    changed: list[str] = []
    for member in all_members:
        if left_map.get(member) != right_map.get(member):
            changed.append(member)

    print("=== differences ===")
    if not changed:
        print("No cell moved between buckets.")
        print()
        return

    print(f"{len(changed)} cell(s) belong to different buckets:")
    print()
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
                f"maps entirely to {right_label} bucket {right_ids[0]}"
            )
        elif len(members) == 1:
            member = members[0]
            print(
                f"  {member} alone in {left_label} bucket {left_id}, "
                f"joins {right_label} bucket {right_ids}"
            )
        else:
            print(
                f"  {left_label} bucket {left_id} members {members} "
                f"split across {right_label} buckets {right_ids}"
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
    args = parser.parse_args()

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
    layout = config.projection_layout

    independent = _cluster_by_mode(
        variation_mode="independent",
        bound_address_keys=bound_address_keys,
        projection=projection,
        workbook_path=config.workbook_path,
        layout=layout,
    )
    dominant_key_only = _cluster_by_mode(
        variation_mode="dominant_key_only",
        bound_address_keys=bound_address_keys,
        projection=projection,
        workbook_path=config.workbook_path,
        layout=layout,
    )

    print(f"workbook: {config.workbook_path}")
    print(
        f"projection: optimal compression ({len(projection.projected_graph)} formula nodes)"
    )
    print()

    _print_mode_report(label='variation_mode="independent"', clusters=independent)
    _print_mode_report(
        label='variation_mode="dominant_key_only"',
        clusters=dominant_key_only,
    )
    _print_differences(
        left_label="independent",
        left_clusters=independent,
        right_label="dominant_key_only",
        right_clusters=dominant_key_only,
    )


if __name__ == "__main__":
    main()
