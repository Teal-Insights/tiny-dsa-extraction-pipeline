from __future__ import annotations

from typing import cast

from excel_grapher.exporter import BaseProjectionManifest, ProjectionResult

from src.formula_clustering import FormulaCluster, cluster_graph_formulas
from src.refactor_order import (
    assert_valid_cluster_refactor_order,
    assert_valid_refactor_schedule,
    compute_cluster_refactor_order,
    compute_refactor_schedule,
    refactor_failure_target,
)
from src.subgraph_projection import build_refactor_projection
from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph


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


def test_compute_refactor_schedule_dag_matches_cluster_order(
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
    ordered_clusters = compute_cluster_refactor_order(synthetic_projection, clusters)
    units = compute_refactor_schedule(synthetic_projection, clusters)

    assert len(units) == len(ordered_clusters)
    for unit, cluster in zip(units, ordered_clusters, strict=True):
        assert unit.parent_cluster_id == cluster.cluster_id
        assert unit.members == cluster.members
        assert unit.as_formula_cluster() == cluster
    assert_valid_refactor_schedule(synthetic_projection, units)


class _ParallelReadyProjection:
    def get_dependencies(self, address: str) -> tuple[str, ...]:
        if address in {"Engine!B2", "Engine!D2"}:
            return ("Inputs!A1",)
        if address == "Engine!C2":
            return ("Engine!B2",)
        if address == "Engine!E2":
            return ("Engine!D2",)
        return ()


def test_schedule_emits_multi_member_unit_when_parallel_members_ready() -> None:
    parallel_family = FormulaCluster(
        cluster_id=0,
        members=("Engine!B2", "Engine!D2"),
        canonical_template="=Inputs!A1+Inputs!B1",
        row=None,
    )
    dependent_family = FormulaCluster(
        cluster_id=1,
        members=("Engine!C2", "Engine!E2"),
        canonical_template="=Engine!B2*2",
        row=None,
    )
    projection = cast(ProjectionResult, _ParallelReadyProjection())

    units = compute_refactor_schedule(projection, (parallel_family, dependent_family))

    assert len(units) == 2
    assert units[0].members == ("Engine!B2", "Engine!D2")
    assert len(units[0].members) == 2
    assert units[1].members == ("Engine!C2", "Engine!E2")
    assert_valid_refactor_schedule(projection, units)


def test_cluster_detection_can_create_inter_cluster_cycle_on_acyclic_cell_graph() -> (
    None
):
    graph, bindings = inter_cluster_cycle_graph()

    assert graph.evaluation_order() == [
        "Inputs!A2",
        "Inputs!B1",
        "Engine!B2",
        "Engine!C2",
        "Inputs!A3",
        "Engine!B3",
        "Engine!C3",
    ]

    clusters = cluster_graph_formulas(graph, bound_address_keys=bindings)
    members = {cluster.members for cluster in clusters}
    assert members == {
        ("Engine!B2", "Engine!B3"),
        ("Engine!C2", "Engine!C3"),
    }

    units = compute_refactor_schedule(graph, clusters)

    assert [unit.members for unit in units] == [
        ("Engine!B2",),
        ("Engine!C2",),
        ("Engine!B3",),
        ("Engine!C3",),
    ]
    assert {member for unit in units for member in unit.members} == {
        "Engine!B2",
        "Engine!C2",
        "Engine!B3",
        "Engine!C3",
    }
    assert len({unit.refactor_group_id for unit in units}) == 4
    assert refactor_failure_target(units[0]) == (
        f"cluster_{units[0].parent_cluster_id}_g{units[0].refactor_group_id}"
    )
    assert_valid_refactor_schedule(graph, units)


def test_schedule_prefers_family_that_unblocks_least_blocked_waiter() -> None:
    """When two families are ready, prefer the batch waited on by more blockers."""
    cluster_x = FormulaCluster(
        cluster_id=0,
        members=("Sheet!X1", "Sheet!X2", "Sheet!X3"),
        canonical_template="=Inputs!A1",
        row=None,
    )
    cluster_y = FormulaCluster(
        cluster_id=1,
        members=("Sheet!Y1", "Sheet!Y2"),
        canonical_template="=Inputs!A2",
        row=None,
    )

    class _HeuristicProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Sheet!X1": ("Inputs!A1",),
                "Sheet!X2": ("Sheet!Y1",),
                "Sheet!X3": ("Sheet!Y1",),
                "Sheet!Y1": ("Inputs!A2",),
                "Sheet!Y2": ("Sheet!X1",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _HeuristicProjection())
    units = compute_refactor_schedule(projection, (cluster_x, cluster_y))

    assert units[0].members == ("Sheet!Y1",)
    assert units[1].members == ("Sheet!X1", "Sheet!X2", "Sheet!X3")
    assert units[2].members == ("Sheet!Y2",)
    assert_valid_refactor_schedule(projection, units)
