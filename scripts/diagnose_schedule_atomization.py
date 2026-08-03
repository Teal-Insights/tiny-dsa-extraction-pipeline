"""Diagnose fingerprint-family → schedule-slice fan-out on the configured workbook.

Run: uv run -m scripts.diagnose_schedule_atomization
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.extraction_pipeline import build_pipeline_graph  # noqa: E402
from src.formula_clustering import FormulaCluster, cluster_graph_formulas  # noqa: E402
from src.pipeline_config import load_pipeline_config, validate_pipeline_config  # noqa: E402
from src.refactor_order import (  # noqa: E402
    ScheduleDiagnostics,
    compute_refactor_schedule_with_diagnostics,
)
from src.series_remodel_diagnostics import (  # noqa: E402
    format_remodel_recommendations,
    recommend_series_remodels,
    shredded_series_from_schedule,
)
from src.subgraph_projection import build_refactor_projection  # noqa: E402


def _percentile(sorted_values: list[int], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = (len(sorted_values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _print_report(
    *,
    diagnostics: ScheduleDiagnostics,
    address_to_series_id: dict[str, str],
    clusters_by_id: dict[int, FormulaCluster],
    top_n: int,
    peel_sample_limit: int,
) -> None:
    print(f"path: {diagnostics.path}")
    print(
        f"fingerprint families: {diagnostics.fingerprint_family_count}; "
        f"schedule units: {diagnostics.schedule_unit_count}"
    )
    print(
        f"emits: dag={diagnostics.dag_emits} "
        f"whole_family={diagnostics.whole_family_emits} "
        f"peel={diagnostics.peel_emits} "
        f"(fan-out {diagnostics.schedule_unit_count / max(diagnostics.fingerprint_family_count, 1):.1f}x families)"
    )

    intact = [
        stats for stats in diagnostics.families_by_unit_count if stats.unit_count == 1
    ]
    shredded = [
        stats for stats in diagnostics.families_by_unit_count if stats.unit_count > 1
    ]
    member_total = sum(
        stats.member_count for stats in diagnostics.families_by_unit_count
    )
    shredded_members = sum(stats.member_count for stats in shredded)
    shredded_units = sum(stats.unit_count for stats in shredded)
    print(
        f"intact families (1 unit): {len(intact)}; "
        f"shredded families (>1 unit): {len(shredded)}"
    )
    if member_total:
        print(
            f"shredded coverage: {shredded_members}/{member_total} members, "
            f"{shredded_units}/{diagnostics.schedule_unit_count} units"
        )

    whole_sizes = [
        decision.member_count
        for decision in diagnostics.decisions
        if decision.kind in {"dag", "whole_family"}
    ]
    if whole_sizes:
        whole_hist = Counter(whole_sizes)
        print(
            "whole-family emits: "
            f"multi-member={sum(1 for size in whole_sizes if size > 1)} "
            f"singleton={sum(1 for size in whole_sizes if size == 1)}; "
            f"size hist top={list(whole_hist.most_common(6))}"
        )

    unit_counts = [stats.unit_count for stats in diagnostics.families_by_unit_count]
    singleton_counts = [
        stats.singleton_units for stats in diagnostics.families_by_unit_count
    ]
    if unit_counts:
        print(
            "units per family: "
            f"max={max(unit_counts)} "
            f"p50={_percentile(sorted(unit_counts), 0.5):.1f} "
            f"p90={_percentile(sorted(unit_counts), 0.9):.1f} "
            f"mean={statistics.fmean(unit_counts):.1f}"
        )
        print(
            "singleton units per family: "
            f"max={max(singleton_counts)} "
            f"p50={_percentile(sorted(singleton_counts), 0.5):.1f} "
            f"mean={statistics.fmean(singleton_counts):.1f}"
        )

    peel_sizes = [
        decision.member_count
        for decision in diagnostics.decisions
        if decision.kind == "peel"
    ]
    if peel_sizes:
        size_hist = Counter(peel_sizes)
        top_sizes = ", ".join(
            f"size={size}:{count}"
            for size, count in sorted(
                size_hist.items(), key=lambda item: (-item[1], item[0])
            )[:8]
        )
        print(
            f"peel sizes: n={len(peel_sizes)} "
            f"max={max(peel_sizes)} "
            f"p50={_percentile(sorted(peel_sizes), 0.5):.1f} "
            f"mean={statistics.fmean(peel_sizes):.2f}"
        )
        print(f"peel size histogram (top): {top_sizes}")

    print()
    print(f"=== worst families by unit count (top {top_n}) ===")
    for stats in diagnostics.families_by_unit_count[:top_n]:
        cluster = clusters_by_id[stats.parent_cluster_id]
        members = cluster.members
        series_ids = sorted(
            {
                address_to_series_id[member]
                for member in members
                if member in address_to_series_id
            }
        )
        series_label = ", ".join(series_ids[:3]) if series_ids else "(unowned)"
        if len(series_ids) > 3:
            series_label += f", +{len(series_ids) - 3} more"
        sample_members = ", ".join(members[:3])
        if len(members) > 3:
            sample_members += f", +{len(members) - 3} more"
        print(
            f"family {stats.parent_cluster_id}: "
            f"members={stats.member_count} units={stats.unit_count} "
            f"(dag={stats.dag_emits} whole_family={stats.whole_family_emits} "
            f"peel={stats.peel_emits} singleton={stats.singleton_units}) "
            f"slices/member={stats.slices_per_member:.2f}"
        )
        print(f"  series: {series_label}")
        print(f"  members sample: {sample_members}")
        template = cluster.canonical_template
        if len(template) > 96:
            template = f"{template[:93]}..."
        print(f"  template: {template}")

    peel_decisions = [
        decision for decision in diagnostics.decisions if decision.kind == "peel"
    ]
    if peel_sample_limit <= 0 or not peel_decisions:
        return

    print()
    print(f"=== sample peel decisions (first {peel_sample_limit}) ===")
    for decision in peel_decisions[:peel_sample_limit]:
        print(
            f"peel family={decision.parent_cluster_id} "
            f"batch_size={decision.member_count}"
        )
        if decision.blocking_family_sample:
            stuck = ", ".join(
                f"{family_id}:{blocked}"
                for family_id, blocked in decision.blocking_family_sample[:5]
            )
            print(f"  least-stuck families (id:cross_blocked): {stuck}")
        if decision.blocking_cross_edge_sample:
            for waiter, dep, dep_family in decision.blocking_cross_edge_sample[:5]:
                print(f"  cross hinge: {waiter} <- {dep} (dep family {dep_family})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Explain fingerprint-family → schedule-slice fan-out with peel vs "
            "whole-family emit evidence from compute_refactor_schedule."
        )
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk graph, projection, and codegen caches for this run.",
    )
    parser.add_argument(
        "--top-families",
        type=int,
        default=10,
        help="How many worst families (by unit count) to print (default: 10).",
    )
    parser.add_argument(
        "--peel-samples",
        type=int,
        default=12,
        help="How many early peel decisions to print with hinge samples (default: 12).",
    )
    parser.add_argument(
        "--top-shredded-series",
        type=int,
        default=15,
        help="How many shredded series to list (default: 15).",
    )
    parser.add_argument(
        "--top-recommendations",
        type=int,
        default=20,
        help="How many cyclical remodel recommendations to print (default: 20).",
    )
    args = parser.parse_args()

    config = load_pipeline_config()
    validate_pipeline_config(config)

    graph_result = build_pipeline_graph(config, no_cache=args.no_cache)
    projection = build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.graph_cache_key,
        no_cache=args.no_cache,
    )
    bound_address_keys = graph_result.bound_address_keys
    address_to_series_id = graph_result.address_to_series_id

    clusters = cluster_graph_formulas(
        projection,
        bound_address_keys=bound_address_keys,
        variation_mode=config.variation_mode,
        clustering_mode=config.clustering_mode,
        address_to_series_id=address_to_series_id,
        workbook_path=config.workbook_path,
        layout=config.projection_layout,
    )
    clusters_by_id = {cluster.cluster_id: cluster for cluster in clusters}

    print(f"workbook: {config.workbook_path}")
    print(
        f"clustering_mode={config.clustering_mode!r} "
        f"variation_mode={config.variation_mode!r}"
    )
    print(
        f"projected formulas: {len(projection.formula_keys())}; "
        f"fingerprint families: {len(clusters)}"
    )
    print()

    units, diagnostics = compute_refactor_schedule_with_diagnostics(
        projection,
        clusters,
        include_decisions=args.peel_samples > 0,
    )
    assert len(units) == diagnostics.schedule_unit_count

    _print_report(
        diagnostics=diagnostics,
        address_to_series_id=address_to_series_id,
        clusters_by_id=clusters_by_id,
        top_n=args.top_families,
        peel_sample_limit=args.peel_samples,
    )

    shredded = shredded_series_from_schedule(
        clusters=clusters,
        units=units,
        address_to_series_id=address_to_series_id,
    )
    recommendations = recommend_series_remodels(
        projection=projection,
        clusters=clusters,
        units=units,
        address_to_series_id=address_to_series_id,
    )
    print(
        format_remodel_recommendations(
            recommendations,
            shredded=shredded,
            top_shredded=args.top_shredded_series,
            top_recommendations=args.top_recommendations,
        )
    )


if __name__ == "__main__":
    main()
