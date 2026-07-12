from __future__ import annotations

from typing import cast

from excel_grapher.exporter import BaseProjectionManifest, ProjectionResult

from src.formula_clustering import FormulaCluster, cluster_graph_formulas
from src.refactor_order import (
    assert_valid_cluster_refactor_order,
    compute_cluster_refactor_order,
)
from src.subgraph_projection import build_refactor_projection


def test_refactor_projection_uses_optimal_compression(synthetic_graph) -> None:
    projection = build_refactor_projection(synthetic_graph)
    manifest = projection.manifest
    assert isinstance(manifest, BaseProjectionManifest)
    assert manifest.kind == "optimal_compression"
    assert len(projection) <= len(synthetic_graph)


def test_cluster_graph_formulas_finds_parallel_engine_row(
    synthetic_projection,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture,
) -> None:
    clusters = cluster_graph_formulas(
        synthetic_projection,
        bound_address_keys=synthetic_bound_address_keys,
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
        layout=synthetic_pipeline_config_fixture.projection_layout,
    )
    parallel = next(
        cluster for cluster in clusters if cluster.members == ("Engine!B2", "Engine!C2")
    )
    assert parallel.row == 2


def test_compute_cluster_refactor_order_respects_dependencies(
    synthetic_projection,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture,
) -> None:
    clusters = cluster_graph_formulas(
        synthetic_projection,
        bound_address_keys=synthetic_bound_address_keys,
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
        layout=synthetic_pipeline_config_fixture.projection_layout,
    )
    ordered = compute_cluster_refactor_order(synthetic_projection, clusters)

    assert len(ordered) == 2
    assert ordered[0].members == ("Engine!B2", "Engine!C2")
    assert ordered[1].members == ("Outputs!B1", "Outputs!C1")
    assert_valid_cluster_refactor_order(synthetic_projection, ordered)
    assert len({cluster.cluster_id for cluster in ordered}) == len(ordered)


class _StubProjection:
    def get_dependencies(self, address: str) -> tuple[str, ...]:
        if address == "Engine!B3":
            return ("Engine!B2",)
        if address in {"Outputs!B1", "Outputs!C1"}:
            return ("Engine!B2",) if address == "Outputs!B1" else ("Engine!C2",)
        return ()


def test_compute_cluster_refactor_order_interleaves_singleton_and_multi_member() -> (
    None
):
    multi_member = FormulaCluster(
        cluster_id=0,
        members=("Engine!B2", "Engine!C2"),
        canonical_template="=Inputs!A1+Inputs!B1+1",
        row=2,
    )
    singleton = FormulaCluster(
        cluster_id=1,
        members=("Engine!B3",),
        canonical_template="=Engine!B2*2",
        row=3,
    )
    clusters = (multi_member, singleton)
    projection = cast(ProjectionResult, _StubProjection())

    ordered = compute_cluster_refactor_order(projection, clusters)

    assert ordered == (multi_member, singleton)
    assert_valid_cluster_refactor_order(projection, ordered)


def test_compute_cluster_refactor_order_includes_all_eligible_clusters(
    synthetic_projection,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture,
) -> None:
    clusters = cluster_graph_formulas(
        synthetic_projection,
        bound_address_keys=synthetic_bound_address_keys,
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
        layout=synthetic_pipeline_config_fixture.projection_layout,
    )
    eligible = [cluster for cluster in clusters if cluster.members]
    ordered = compute_cluster_refactor_order(synthetic_projection, clusters)
    assert len(ordered) == len(eligible)
    assert_valid_cluster_refactor_order(synthetic_projection, ordered)
