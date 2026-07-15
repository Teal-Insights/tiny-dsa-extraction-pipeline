from __future__ import annotations

import logging
import time
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


def _interleaved_family_cycle_schedule(
    member_count: int,
) -> tuple[object, tuple[FormulaCluster, FormulaCluster]]:
    """Two families whose cell deps are acyclic but family deps cycle."""
    cluster_a = FormulaCluster(
        cluster_id=0,
        members=tuple(f"Engine!A{index}" for index in range(1, member_count + 1)),
        canonical_template="=PRIOR",
        row=None,
    )
    cluster_b = FormulaCluster(
        cluster_id=1,
        members=tuple(f"Engine!B{index}" for index in range(1, member_count + 1)),
        canonical_template="=CURRENT",
        row=None,
    )
    deps: dict[str, tuple[str, ...]] = {}
    for index in range(1, member_count + 1):
        a_address = f"Engine!A{index}"
        b_address = f"Engine!B{index}"
        deps[a_address] = (f"Engine!B{index - 1}",) if index > 1 else ()
        deps[b_address] = (a_address,)

    class _InterleavedProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            return deps.get(address, ())

    return _InterleavedProjection(), (cluster_a, cluster_b)


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
        clustering_mode="ast",
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
        clustering_mode="ast",
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
        clustering_mode="ast",
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
        clustering_mode="ast",
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

    clusters = cluster_graph_formulas(
        graph, bound_address_keys=bindings, clustering_mode="ast"
    )
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


def test_cycle_schedule_emits_alternating_singletons_for_interleaved_families() -> None:
    projection, clusters = _interleaved_family_cycle_schedule(3)
    units = compute_refactor_schedule(cast(ProjectionResult, projection), clusters)

    assert [unit.members for unit in units] == [
        ("Engine!A1",),
        ("Engine!B1",),
        ("Engine!A2",),
        ("Engine!B2",),
        ("Engine!A3",),
        ("Engine!B3",),
    ]
    assert_valid_refactor_schedule(cast(ProjectionResult, projection), units)


def test_cycle_schedule_scales_to_thousands_of_interleaved_members(
    caplog,
) -> None:
    member_count = 2_500
    projection, clusters = _interleaved_family_cycle_schedule(member_count)

    with caplog.at_level(logging.INFO, logger="src.refactor_order"):
        started = time.perf_counter()
        units = compute_refactor_schedule(cast(ProjectionResult, projection), clusters)
        elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"cycle schedule took {elapsed:.2f}s"
    assert len(units) == 2 * member_count
    assert all(len(unit.members) == 1 for unit in units)
    assert {member for unit in units for member in unit.members} == {
        *clusters[0].members,
        *clusters[1].members,
    }
    assert_valid_refactor_schedule(cast(ProjectionResult, projection), units)
    assert any(
        "refactor schedule path=cycle_split" in record.message
        and "families=2" in record.message
        and f"units={2 * member_count}" in record.message
        for record in caplog.records
    )


def _ring_family_cycle_schedule(
    family_count: int,
    depth: int,
) -> tuple[object, tuple[FormulaCluster, ...]]:
    """Many families ready in parallel, with a cyclic inter-family dependency ring."""
    clusters: list[FormulaCluster] = []
    deps: dict[str, tuple[str, ...]] = {}
    for family_id in range(family_count):
        members = tuple(f"S{family_id}!A{row}" for row in range(1, depth + 1))
        clusters.append(
            FormulaCluster(
                cluster_id=family_id,
                members=members,
                canonical_template="=PRIOR",
                row=None,
            )
        )
        for row in range(1, depth + 1):
            address = f"S{family_id}!A{row}"
            if row == 1:
                deps[address] = ()
            else:
                predecessor = f"S{(family_id - 1) % family_count}!A{row - 1}"
                deps[address] = (predecessor,)

    class _RingProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            return deps.get(address, ())

    return _RingProjection(), tuple(clusters)


def test_cycle_schedule_scales_with_many_parallel_ready_families() -> None:
    family_count = 80
    depth = 80
    projection, clusters = _ring_family_cycle_schedule(family_count, depth)

    started = time.perf_counter()
    units = compute_refactor_schedule(cast(ProjectionResult, projection), clusters)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"multi-family cycle schedule took {elapsed:.2f}s"
    assert {member for unit in units for member in unit.members} == {
        member for cluster in clusters for member in cluster.members
    }
    assert_valid_refactor_schedule(cast(ProjectionResult, projection), units)


def test_dag_schedule_logs_path(caplog) -> None:
    cluster_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1",),
        canonical_template="=1",
        row=1,
    )
    cluster_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=Engine!A1",
        row=1,
    )

    class _DagProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            if address == "Engine!B1":
                return ("Engine!A1",)
            return ()

    with caplog.at_level(logging.INFO, logger="src.refactor_order"):
        units = compute_refactor_schedule(
            cast(ProjectionResult, _DagProjection()),
            (cluster_a, cluster_b),
        )

    assert [unit.members for unit in units] == [("Engine!A1",), ("Engine!B1",)]
    assert any(
        "refactor schedule path=dag" in record.message
        and "families=2" in record.message
        for record in caplog.records
    )
