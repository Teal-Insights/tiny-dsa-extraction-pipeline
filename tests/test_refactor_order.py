from __future__ import annotations

from excel_grapher.exporter import BaseProjectionManifest

from src.formula_clustering import cluster_graph_formulas
from src.refactor_order import (
    assert_valid_cluster_refactor_order,
    compute_multi_member_cluster_refactor_order,
    compute_singleton_cluster_refactor_order,
)
from src.subgraph_projection import build_refactor_projection


def test_refactor_projection_uses_optimal_compression(synthetic_graph) -> None:
    projection = build_refactor_projection(synthetic_graph)
    manifest = projection.manifest
    assert isinstance(manifest, BaseProjectionManifest)
    assert manifest.kind == "optimal_compression"
    assert len(projection) <= len(synthetic_graph)


def test_cluster_graph_formulas_finds_parallel_engine_row(synthetic_projection) -> None:
    clusters = cluster_graph_formulas(synthetic_projection)
    parallel = next(
        cluster for cluster in clusters if cluster.members == ("Engine!B2", "Engine!C2")
    )
    assert parallel.row == 2


def test_compute_multi_member_cluster_refactor_order_respects_dependencies(
    synthetic_projection,
) -> None:
    clusters = cluster_graph_formulas(synthetic_projection)
    ordered = compute_multi_member_cluster_refactor_order(
        synthetic_projection,
        clusters,
    )

    assert len(ordered) == 2
    assert ordered[0].members == ("Engine!B2", "Engine!C2")
    assert ordered[1].members == ("Outputs!B1", "Outputs!C1")
    assert_valid_cluster_refactor_order(synthetic_projection, ordered)
    assert len({cluster.cluster_id for cluster in ordered}) == len(ordered)


def test_compute_multi_member_cluster_refactor_order_includes_all_eligible(
    synthetic_projection,
) -> None:
    clusters = cluster_graph_formulas(synthetic_projection)
    eligible = [cluster for cluster in clusters if len(cluster.members) >= 2]
    ordered = compute_multi_member_cluster_refactor_order(
        synthetic_projection,
        clusters,
    )
    assert len(ordered) == len(eligible)


def test_compute_singleton_cluster_refactor_order_is_empty_when_no_singletons(
    synthetic_projection,
) -> None:
    clusters = cluster_graph_formulas(synthetic_projection)
    ordered = compute_singleton_cluster_refactor_order(
        synthetic_projection,
        clusters,
    )
    assert ordered == ()
