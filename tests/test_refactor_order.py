from __future__ import annotations

from src.formula_clustering import cluster_graph_formulas
from src.internals_refactor import REFACTOR_ROW_ORDER
from src.refactor_order import (
    assert_valid_cluster_refactor_order,
    compute_multi_member_cluster_refactor_order,
    compute_singleton_cluster_refactor_order,
)


def test_compute_multi_member_cluster_refactor_order_for_tiny_dsa(
    tiny_dsa_refactor_projection,
) -> None:
    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    ordered = compute_multi_member_cluster_refactor_order(
        tiny_dsa_refactor_projection,
        clusters,
    )

    assert len(ordered) == len(REFACTOR_ROW_ORDER)
    assert [cluster.row for cluster in ordered][:2] == list(REFACTOR_ROW_ORDER[:2])
    assert ordered[-1].row == REFACTOR_ROW_ORDER[-1]
    assert_valid_cluster_refactor_order(tiny_dsa_refactor_projection, ordered)
    assert len({cluster.cluster_id for cluster in ordered}) == len(ordered)


def test_compute_multi_member_cluster_refactor_order_includes_all_eligible(
    tiny_dsa_refactor_projection,
) -> None:
    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    eligible = [cluster for cluster in clusters if len(cluster.members) >= 2]

    ordered = compute_multi_member_cluster_refactor_order(
        tiny_dsa_refactor_projection,
        clusters,
    )

    assert len(ordered) == len(eligible)


def test_compute_singleton_cluster_refactor_order_for_tiny_dsa(
    tiny_dsa_refactor_projection,
) -> None:
    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    ordered = compute_singleton_cluster_refactor_order(
        tiny_dsa_refactor_projection,
        clusters,
    )

    assert len(ordered) == 2
    assert {cluster.members[0] for cluster in ordered} == {
        "Inputs!B6",
        "Engine!B9",
    }
