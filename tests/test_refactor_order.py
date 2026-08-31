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
    compute_refactor_schedule_with_diagnostics,
    refactor_failure_target,
)
from src.semantic_naming import allocate_schedule_helper_names
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


def test_refactor_projection_uses_optimal_compression(
    synthetic_graph,
    synthetic_series_bindings,
    synthetic_workbook_path,
) -> None:
    projection = build_refactor_projection(
        synthetic_graph,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_workbook_path,
    )
    manifest = projection.manifest
    assert isinstance(manifest, BaseProjectionManifest)
    assert manifest.kind == "optimal_compression"
    assert len(projection) <= len(synthetic_graph)


def test_cluster_graph_formulas_finds_parallel_outputs_row(
    synthetic_projection,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture,
) -> None:
    clusters = cluster_graph_formulas(
        synthetic_projection,
        bound_address_keys=synthetic_bound_address_keys,
        clustering_mode="ast",
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
    )
    parallel = next(
        cluster
        for cluster in clusters
        if cluster.members == ("Outputs!B1", "Outputs!C1")
    )
    assert parallel.row == 1


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
    )
    ordered = compute_cluster_refactor_order(synthetic_projection, clusters)

    assert len(ordered) == 1
    assert ordered[0].members == ("Outputs!B1", "Outputs!C1")
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


def test_scheduler_prefers_whole_ready_family_over_concurrent_peel() -> None:
    """At one decision point, a wholly ready family beats a peelable subset.

    Families A and C form an inter-family cycle (force peel mode). Family B has
    no cross-family hinges and is wholly ready at the same time A has a peelable
    frontier ``{A1}``. The scheduler must emit whole B first, not peel A.
    """
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2"),
        canonical_template="=A",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1", "Engine!B2"),
        canonical_template="=B",
        row=None,
    )
    family_c = FormulaCluster(
        cluster_id=2,
        members=("Engine!C1",),
        canonical_template="=C",
        row=None,
    )

    class _WholeFamilyBeatsPeelProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            # Cell deps stay acyclic (A1 → C1 → A2); families A↔C still cycle.
            deps = {
                "Engine!A1": (),
                "Engine!A2": ("Engine!C1",),
                "Engine!B1": (),
                "Engine!B2": ("Engine!B1",),
                "Engine!C1": ("Engine!A1",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _WholeFamilyBeatsPeelProjection())
    units = compute_refactor_schedule(projection, (family_a, family_b, family_c))

    assert units[0].members == ("Engine!B1", "Engine!B2")
    assert_valid_refactor_schedule(projection, units)


def test_scheduler_prefers_whole_ready_family_over_subset_peel() -> None:
    """Peel only when no whole family is ready; resume whole-family scheduling after.

    Family A and B form an inter-family cycle, so no whole family is ready at
    start and a within-family peel of ``A1`` is required. That peel clears B's
    only cross-family hinge, so the next unit must be the entire remaining
    family B ``(B1, B2)`` — not a cell-level peel that shreds B into singletons.
    """
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2"),
        canonical_template="=A",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1", "Engine!B2"),
        canonical_template="=B",
        row=None,
    )

    class _PeelThenWholeFamilyProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                # Cross-family hinge keeps whole A unready until B runs.
                "Engine!A2": ("Engine!A1", "Engine!B1"),
                "Engine!B1": ("Engine!A1",),
                "Engine!B2": ("Engine!B1",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _PeelThenWholeFamilyProjection())
    units = compute_refactor_schedule(projection, (family_a, family_b))

    assert [unit.members for unit in units] == [
        ("Engine!A1",),
        ("Engine!B1", "Engine!B2"),
        ("Engine!A2",),
    ]
    assert_valid_refactor_schedule(projection, units)


def test_cycle_split_peels_within_family_chain_together_not_as_singletons() -> None:
    """Within-family depends-on chains must peel as one unit, not successive singletons.

    Family A is a recurrence chain (A2 depends on A1, A3 on A2). Family B inserts a
    cross-family hinge after A1 so the inter-family DAG is cyclic and a peel is
    required. After B1 is scheduled, A2 and A3 are only waiting on within-family
    prerequisites — they must leave together.
    """
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2", "Engine!A3"),
        canonical_template="=PRIOR",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=Engine!A1",
        row=None,
    )

    class _ChainWithCrossHingeProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                "Engine!B1": ("Engine!A1",),
                # Cross-family hinge: A2 waits on B1 as well as within-family A1.
                "Engine!A2": ("Engine!A1", "Engine!B1"),
                "Engine!A3": ("Engine!A2",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _ChainWithCrossHingeProjection())
    units = compute_refactor_schedule(projection, (family_a, family_b))

    assert [unit.members for unit in units] == [
        ("Engine!A1",),
        ("Engine!B1",),
        ("Engine!A2", "Engine!A3"),
    ]
    assert_valid_refactor_schedule(projection, units)


def test_peeled_schedule_units_from_one_series_get_unique_helper_names() -> None:
    """Peel slices of one series lock distinct helper names at schedule time."""
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2", "Engine!A3"),
        canonical_template="=PRIOR",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=Engine!A1",
        row=None,
    )

    class _ChainWithCrossHingeProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                "Engine!B1": ("Engine!A1",),
                "Engine!A2": ("Engine!A1", "Engine!B1"),
                "Engine!A3": ("Engine!A2",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _ChainWithCrossHingeProjection())
    units = compute_refactor_schedule(projection, (family_a, family_b))
    address_to_series_id = {
        "Engine!A1": "shocked_path_internal",
        "Engine!A2": "shocked_path_internal",
        "Engine!A3": "shocked_path_internal",
        "Engine!B1": "hinge_helper",
    }

    names = allocate_schedule_helper_names(
        tuple(unit.members for unit in units),
        address_to_series_id,
    )

    assert [unit.members for unit in units] == [
        ("Engine!A1",),
        ("Engine!B1",),
        ("Engine!A2", "Engine!A3"),
    ]
    assert names == (
        "shocked_path_internal",
        "hinge_helper",
        "shocked_path_internal_2",
    )


def test_allocate_schedule_helper_names_avoids_name_already_in_internals() -> None:
    """A peeled series colliding with an existing internals helper is renamed."""
    names = allocate_schedule_helper_names(
        (("Engine!A1",), ("Engine!A2",)),
        {"Engine!A1": "shared_series", "Engine!A2": "shared_series"},
        existing_names=frozenset({"shared_series"}),
    )
    assert names == ("shared_series_2", "shared_series_3")
    assert len(set(names)) == 2


def test_cycle_split_peels_all_leafmost_within_family_frontiers() -> None:
    """Upward within-family walks from every leafmost seed, stopping at external walls.

    Family A is rooted at A1 with two depends-on branches plus a disconnected cell:

    - ``A1 > A2 > A3 > A4`` — fully unblocked within-family chain
    - ``A1 > A5 > A6 > A7`` — external hinge on A5 (depends on family B)
    - ``A8`` — same family, no edges to the rest; also a leafmost seed

    Leafmost seeds are A4, A7, and A8. Each walk includes cells whose outstanding
    deps are only within the peel; A5 is a wall (external to B). A1 is excluded
    because it depends on A5 and is therefore secondhand-blocked by that external
    hinge.

    Ready peel: A2, A3, A4, A6, A7, A8 (not A1, not A5).
    """
    family_a = FormulaCluster(
        cluster_id=0,
        members=tuple(f"Engine!A{index}" for index in range(1, 9)),
        canonical_template="=PRIOR",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=ENGINE",
        row=None,
    )

    class _TwoBranchRootProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                # Unblocked branch: A1 > A2 > A3 > A4
                "Engine!A4": (),
                "Engine!A3": ("Engine!A4",),
                "Engine!A2": ("Engine!A3",),
                # Blocked branch: A1 > A5 > A6 > A7, external wall at A5
                "Engine!A7": (),
                "Engine!A6": ("Engine!A7",),
                "Engine!A5": ("Engine!A6", "Engine!B1"),
                "Engine!A1": ("Engine!A2", "Engine!A5"),
                # Disconnected same-family leafmost seed
                "Engine!A8": (),
                # Inter-family cycle hinge: B waits on the unblocked branch tip.
                "Engine!B1": ("Engine!A2",),
            }
            return deps.get(address, ())

    projection = cast(ProjectionResult, _TwoBranchRootProjection())
    units = compute_refactor_schedule(projection, (family_a, family_b))

    assert units[0].members == (
        "Engine!A2",
        "Engine!A3",
        "Engine!A4",
        "Engine!A6",
        "Engine!A7",
        "Engine!A8",
    )
    assert "Engine!A1" not in units[0].members
    assert "Engine!A5" not in units[0].members
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
        and "fingerprint_families=2" in record.message
        and f"schedule_units={2 * member_count}" in record.message
        and f"singleton_schedule_units={2 * member_count}" in record.message
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
        and "fingerprint_families=2" in record.message
        and "schedule_units=2" in record.message
        for record in caplog.records
    )


def test_schedule_diagnostics_marks_dag_path_emits() -> None:
    cluster_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2"),
        canonical_template="=1",
        row=None,
    )
    cluster_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1",),
        canonical_template="=Engine!A1",
        row=None,
    )

    class _DagProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                "Engine!A2": ("Engine!A1",),
                "Engine!B1": ("Engine!A1",),
            }
            return deps.get(address, ())

    units, summary = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, _DagProjection()),
        (cluster_a, cluster_b),
    )
    assert len(units) == 2
    assert summary.path == "dag"
    assert summary.fingerprint_family_count == 2
    assert summary.schedule_unit_count == 2
    assert summary.dag_emits == 2
    assert summary.whole_family_emits == 0
    assert summary.peel_emits == 0
    assert summary.decisions == ()

    units_detail, detail = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, _DagProjection()),
        (cluster_a, cluster_b),
        include_decisions=True,
    )
    assert units_detail == units
    assert [decision.kind for decision in detail.decisions] == ["dag", "dag"]
    assert all(
        decision.member_count == len(units[index].members)
        for index, decision in enumerate(detail.decisions)
    )


def test_schedule_diagnostics_distinguishes_peel_then_whole_family() -> None:
    """Peel A1, then whole B, then leftover A as whole remaining family."""
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!A1", "Engine!A2"),
        canonical_template="=A",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B1", "Engine!B2"),
        canonical_template="=B",
        row=None,
    )

    class _PeelThenWholeFamilyProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!A1": (),
                "Engine!A2": ("Engine!A1", "Engine!B1"),
                "Engine!B1": ("Engine!A1",),
                "Engine!B2": ("Engine!B1",),
            }
            return deps.get(address, ())

    units, summary = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, _PeelThenWholeFamilyProjection()),
        (family_a, family_b),
    )

    assert [unit.members for unit in units] == [
        ("Engine!A1",),
        ("Engine!B1", "Engine!B2"),
        ("Engine!A2",),
    ]
    assert summary.path == "cycle_split"
    assert summary.dag_emits == 0
    assert summary.whole_family_emits == 2
    assert summary.peel_emits == 1
    assert summary.decisions == ()

    _units, detail = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, _PeelThenWholeFamilyProjection()),
        (family_a, family_b),
        include_decisions=True,
    )
    assert [decision.kind for decision in detail.decisions] == [
        "peel",
        "whole_family",
        "whole_family",
    ]
    first_peel = detail.decisions[0]
    assert first_peel.parent_cluster_id == 0
    assert first_peel.member_count == 1
    assert first_peel.blocking_family_sample
    assert any(
        family_id == 1 and cross_blocked > 0
        for family_id, cross_blocked in first_peel.blocking_family_sample
    )
    assert any(
        waiter == "Engine!B1" and dep == "Engine!A1" and dep_family == 0
        for waiter, dep, dep_family in first_peel.blocking_cross_edge_sample
    )

    by_family = {
        stats.parent_cluster_id: stats for stats in summary.families_by_unit_count
    }
    assert by_family[0].member_count == 2
    assert by_family[0].unit_count == 2
    assert by_family[0].peel_emits == 1
    assert by_family[0].whole_family_emits == 1
    assert by_family[0].dag_emits == 0
    assert by_family[0].singleton_units == 2
    assert by_family[1].unit_count == 1
    assert by_family[1].whole_family_emits == 1
    assert by_family[1].peel_emits == 0


def test_schedule_diagnostics_samples_least_stuck_families_not_stuckest() -> None:
    """Peel hinge samples prefer near-ready (least stuck) families over mega-stuck ones.

    Family Peel seeds the graph. Family Near has one cross hinge on that seed (almost
    whole-family ready). Family Far has many remaining cross hinges. When diagnostics
    sample why no whole family is ready, Near must appear before Far — we care about
    the unblock frontier, not the globally stickiest SCC participants.
    """
    family_peel = FormulaCluster(
        cluster_id=0,
        members=("Engine!P1", "Engine!P2"),
        canonical_template="=PEEL",
        row=None,
    )
    family_near = FormulaCluster(
        cluster_id=1,
        members=("Engine!N1", "Engine!N2"),
        canonical_template="=NEAR",
        row=None,
    )
    family_far = FormulaCluster(
        cluster_id=2,
        members=tuple(f"Engine!F{index}" for index in range(1, 6)),
        canonical_template="=FAR",
        row=None,
    )

    class _LeastStuckSampleProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!P1": (),
                # Keeps Peel in the inter-family cycle with Near.
                "Engine!P2": ("Engine!N1",),
                "Engine!N1": ("Engine!P1",),
                "Engine!N2": ("Engine!N1",),
                # Far waits on P1 with many still-cross-blocked members.
                "Engine!F1": ("Engine!P1",),
                "Engine!F2": ("Engine!P1",),
                "Engine!F3": ("Engine!P1",),
                "Engine!F4": ("Engine!P1",),
                "Engine!F5": ("Engine!P1",),
            }
            return deps.get(address, ())

    units, detail = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, _LeastStuckSampleProjection()),
        (family_peel, family_near, family_far),
        include_decisions=True,
    )
    assert units[0].members == ("Engine!P1",)
    first_peel = detail.decisions[0]
    assert first_peel.kind == "peel"
    assert first_peel.blocking_family_sample
    sampled_family_ids = [
        family_id for family_id, _ in first_peel.blocking_family_sample
    ]
    assert sampled_family_ids[0] == 1, (
        f"expected least-stuck Near (1) first, got {first_peel.blocking_family_sample}"
    )
    assert 2 in sampled_family_ids
    assert sampled_family_ids.index(1) < sampled_family_ids.index(2)
    near_blocked = dict(first_peel.blocking_family_sample)[1]
    far_blocked = dict(first_peel.blocking_family_sample)[2]
    assert near_blocked < far_blocked
    assert any(
        waiter == "Engine!N1" and dep == "Engine!P1"
        for waiter, dep, _dep_family in first_peel.blocking_cross_edge_sample
    )


def test_schedule_diagnostics_ranks_worst_families_for_interleaved_cycle() -> None:
    projection, clusters = _interleaved_family_cycle_schedule(4)
    units, report = compute_refactor_schedule_with_diagnostics(
        cast(ProjectionResult, projection),
        clusters,
    )

    assert len(units) == 8
    assert report.path == "cycle_split"
    assert report.decisions == ()
    # Tail leftovers with cleared cross hinges become whole-family emits.
    assert report.peel_emits == 6
    assert report.whole_family_emits == 2
    assert report.dag_emits == 0
    # Worst first: both families shredded equally into 4 units of 4 members.
    assert [stats.unit_count for stats in report.families_by_unit_count] == [4, 4]
    assert all(stats.singleton_units == 4 for stats in report.families_by_unit_count)
    assert all(stats.peel_emits == 3 for stats in report.families_by_unit_count)
    assert all(stats.whole_family_emits == 1 for stats in report.families_by_unit_count)
    assert all(
        stats.slices_per_member == 1.0 for stats in report.families_by_unit_count
    )
