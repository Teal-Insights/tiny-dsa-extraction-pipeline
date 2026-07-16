from __future__ import annotations

import heapq
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

from src.formula_clustering import ClusterableGraph, FormulaCluster
from src.workbook_addresses import parse_workbook_address

logger = logging.getLogger(__name__)

EmitKind = Literal["dag", "whole_family", "peel"]
SchedulePath = Literal["dag", "cycle_split"]

_BLOCKING_SAMPLE_LIMIT = 8


@dataclass(frozen=True)
class RefactorUnit:
    """One schedulable refactor step: a subset of a fingerprint family's members."""

    parent_cluster_id: int
    refactor_group_id: int
    members: tuple[str, ...]
    canonical_template: str
    row: int | None

    def as_formula_cluster(self) -> FormulaCluster:
        return FormulaCluster(
            cluster_id=self.parent_cluster_id,
            members=self.members,
            canonical_template=self.canonical_template,
            row=self.row,
        )


@dataclass(frozen=True)
class ScheduleEmitDecision:
    """One scheduler emit label for detailed diagnostics (optional).

    ``kind`` is not a control lever — it records which scheduler branch produced
    the unit: ``dag`` (acyclic Kahn path), ``whole_family`` (cycle_split intact
    family), or ``peel`` (cycle_split frontier slice).
    """

    kind: EmitKind
    parent_cluster_id: int
    member_count: int
    # When peeling: least-stuck remaining families (unblock frontier), excluding
    # the peeled family, as (family_id, remaining_cross_blocked).
    blocking_family_sample: tuple[tuple[int, int], ...] = ()
    # When peeling: sample cross hinges from that least-stuck frontier
    # as (waiter, dependency, dependency_family_id).
    blocking_cross_edge_sample: tuple[tuple[str, str, int], ...] = ()


@dataclass(frozen=True)
class FamilyAtomizationStats:
    """How one fingerprint family was sliced into schedule units."""

    parent_cluster_id: int
    member_count: int
    unit_count: int
    dag_emits: int
    whole_family_emits: int
    peel_emits: int
    singleton_units: int

    @property
    def slices_per_member(self) -> float:
        if self.member_count == 0:
            return 0.0
        return self.unit_count / self.member_count


@dataclass(frozen=True)
class ScheduleDiagnostics:
    """Evidence for family→slice fan-out after scheduling.

    Summary fields are always populated. ``decisions`` is empty unless
    ``include_decisions=True`` on ``compute_refactor_schedule_with_diagnostics``.
    """

    path: SchedulePath
    fingerprint_family_count: int
    schedule_unit_count: int
    dag_emits: int
    whole_family_emits: int
    peel_emits: int
    families_by_unit_count: tuple[FamilyAtomizationStats, ...]
    decisions: tuple[ScheduleEmitDecision, ...] = ()


def refactor_failure_target(unit: RefactorUnit) -> str:
    """Stable diagnostic slug for refactor failure dumps."""
    return f"cluster_{unit.parent_cluster_id}_g{unit.refactor_group_id}"


def assert_valid_cluster_refactor_order(
    projection: ClusterableGraph,
    ordered: tuple[FormulaCluster, ...],
) -> None:
    """Raise ``AssertionError`` when a dependency cluster appears after its dependents."""
    owners = _address_owner_clusters(ordered)
    position = {cluster.cluster_id: index for index, cluster in enumerate(ordered)}
    for cluster in ordered:
        for dependency in _external_dependency_addresses(projection, cluster):
            owner = owners.get(dependency)
            if owner is None or owner.cluster_id == cluster.cluster_id:
                continue
            assert position[owner.cluster_id] < position[cluster.cluster_id]


def assert_valid_refactor_schedule(
    projection: ClusterableGraph,
    units: tuple[RefactorUnit, ...],
) -> None:
    """Raise ``AssertionError`` when a unit appears before a unit that owns its dependency."""
    owners = _address_owner_units(units)
    position = {unit.refactor_group_id: index for index, unit in enumerate(units)}
    for unit in units:
        member_set = frozenset(unit.members)
        for address in unit.members:
            for dependency in projection.get_dependencies(address):
                if dependency in member_set:
                    continue
                owner_group = owners.get(dependency)
                if owner_group is None:
                    continue
                assert position[owner_group] < position[unit.refactor_group_id]


def _cluster_member_set(cluster: FormulaCluster) -> frozenset[str]:
    return frozenset(cluster.members)


def _external_dependency_addresses(
    projection: ClusterableGraph,
    cluster: FormulaCluster,
) -> frozenset[str]:
    members = _cluster_member_set(cluster)
    external: set[str] = set()
    for address in cluster.members:
        for dependency in projection.get_dependencies(address):
            if dependency not in members:
                external.add(dependency)
    return frozenset(external)


def _address_owner_clusters(
    clusters: tuple[FormulaCluster, ...],
) -> dict[str, FormulaCluster]:
    owners: dict[str, FormulaCluster] = {}
    for cluster in clusters:
        for address in cluster.members:
            owners[address] = cluster
    return owners


def _address_owner_units(units: tuple[RefactorUnit, ...]) -> dict[str, int]:
    owners: dict[str, int] = {}
    for unit in units:
        for address in unit.members:
            owners[address] = unit.refactor_group_id
    return owners


def _row_for_members(members: tuple[str, ...]) -> int | None:
    rows = {parse_workbook_address(address)[2] for address in members}
    return next(iter(rows)) if len(rows) == 1 else None


def _inter_family_depends_on(
    projection: ClusterableGraph,
    eligible: tuple[FormulaCluster, ...],
) -> dict[int, set[int]]:
    owners = _address_owner_clusters(eligible)
    depends_on: dict[int, set[int]] = {
        cluster.cluster_id: set() for cluster in eligible
    }
    for cluster in eligible:
        for dependency in _external_dependency_addresses(projection, cluster):
            owner = owners.get(dependency)
            if owner is None or owner.cluster_id == cluster.cluster_id:
                continue
            depends_on[cluster.cluster_id].add(owner.cluster_id)
    return depends_on


def _kahn_cluster_order(
    eligible: tuple[FormulaCluster, ...],
    depends_on: dict[int, set[int]],
) -> tuple[FormulaCluster, ...] | None:
    eligible_by_id = {cluster.cluster_id: cluster for cluster in eligible}
    dependents: dict[int, list[int]] = defaultdict(list)
    remaining_inbound = {
        cluster.cluster_id: len(depends_on[cluster.cluster_id]) for cluster in eligible
    }
    for cluster_id, prerequisites in depends_on.items():
        for prerequisite_id in prerequisites:
            dependents[prerequisite_id].append(cluster_id)

    ready: list[int] = [
        cluster_id for cluster_id, count in remaining_inbound.items() if count == 0
    ]
    heapq.heapify(ready)
    ordered_ids: list[int] = []

    while ready:
        cluster_id = heapq.heappop(ready)
        ordered_ids.append(cluster_id)
        for dependent_id in dependents[cluster_id]:
            remaining_inbound[dependent_id] -= 1
            if remaining_inbound[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)

    if len(ordered_ids) != len(eligible):
        return None

    return tuple(eligible_by_id[cluster_id] for cluster_id in ordered_ids)


def _try_inter_family_dag_order(
    projection: ClusterableGraph,
    eligible: tuple[FormulaCluster, ...],
) -> tuple[FormulaCluster, ...] | None:
    depends_on = _inter_family_depends_on(projection, eligible)
    return _kahn_cluster_order(eligible, depends_on)


def _member_adjacency(
    projection: ClusterableGraph,
    members: set[str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    deps: dict[str, tuple[str, ...]] = {}
    dependents_lists: dict[str, list[str]] = defaultdict(list)
    for address in members:
        address_deps = tuple(projection.get_dependencies(address))
        deps[address] = address_deps
        for dependency in address_deps:
            if dependency in members:
                dependents_lists[dependency].append(address)
    dependents = {
        address: tuple(dependent_addresses)
        for address, dependent_addresses in dependents_lists.items()
    }
    return deps, dependents


def _select_ready_parent_cluster(
    ready_by_family: dict[int, list[str]],
    *,
    blocker_buckets: dict[int, set[str]],
    deps: dict[str, tuple[str, ...]],
) -> int:
    if len(ready_by_family) == 1:
        return next(iter(ready_by_family))

    if not blocker_buckets:
        return min(
            ready_by_family,
            key=lambda parent_id: (-len(ready_by_family[parent_id]), parent_id),
        )

    min_blockers = min(blocker_buckets)
    priority_waiters = blocker_buckets[min_blockers]

    ready_address_to_parent = {
        address: parent_id
        for parent_id, addresses in ready_by_family.items()
        for address in addresses
    }
    unblocks: dict[int, set[str]] = {parent_id: set() for parent_id in ready_by_family}
    for waiter in priority_waiters:
        for dependency in deps[waiter]:
            parent_id = ready_address_to_parent.get(dependency)
            if parent_id is not None:
                unblocks[parent_id].add(waiter)

    return min(
        ready_by_family,
        key=lambda parent_id: (
            -len(unblocks[parent_id]),
            -len(ready_by_family[parent_id]),
            parent_id,
        ),
    )


def _decrease_blocker(
    address: str,
    *,
    blocker_count: dict[str, int],
    blocker_buckets: dict[int, set[str]],
    ready: set[str],
) -> None:
    old_count = blocker_count[address]
    new_count = old_count - 1
    if new_count < 0:
        raise ValueError(f"blocker count underflow for {address!r}")
    blocker_count[address] = new_count
    old_bucket = blocker_buckets[old_count]
    old_bucket.remove(address)
    if not old_bucket:
        del blocker_buckets[old_count]
    if new_count == 0:
        ready.add(address)
    else:
        blocker_buckets.setdefault(new_count, set()).add(address)


def _within_family_peel(
    family_id: int,
    *,
    family_remaining: set[str],
    ready: set[str],
    remaining: set[str],
    deps: dict[str, tuple[str, ...]],
    dependents: dict[str, tuple[str, ...]],
    address_to_parent: dict[str, int],
) -> tuple[str, ...]:
    """Leafmost seeds, then upward closure within family until external walls."""
    if not family_remaining:
        return ()

    peel: set[str] = {address for address in family_remaining if address in ready}
    queue: deque[str] = deque(peel)
    while queue:
        address = queue.popleft()
        for dependent in dependents.get(address, ()):
            if dependent not in family_remaining or dependent in peel:
                continue
            outstanding = [dep for dep in deps[dependent] if dep in remaining]
            if any(address_to_parent.get(dep) != family_id for dep in outstanding):
                continue
            if all(dep in peel for dep in outstanding):
                peel.add(dependent)
                queue.append(dependent)
    return tuple(sorted(peel))


def _sample_blocking_cross_evidence(
    *,
    remaining_by_family: dict[int, set[str]],
    family_cross_blocked: dict[int, int],
    cross_blocker_count: dict[str, int],
    deps: dict[str, tuple[str, ...]],
    remaining: set[str],
    address_to_parent: dict[str, int],
    exclude_family_id: int | None = None,
    limit: int = _BLOCKING_SAMPLE_LIMIT,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[str, str, int], ...]]:
    """Sample near-ready stuck families (unblock frontier) and their cross hinges.

    Prefers least-cross-blocked remaining families over the globally stickiest
    participants, and skips ``exclude_family_id`` (typically the family being
    peeled) so the sample describes what the peel is positioned to unblock.
    """
    stuck_families = sorted(
        (
            (family_id, family_cross_blocked[family_id])
            for family_id, members in remaining_by_family.items()
            if members
            and family_cross_blocked[family_id] > 0
            and family_id != exclude_family_id
        ),
        key=lambda item: (item[1], item[0]),
    )[:limit]

    cross_edges: list[tuple[str, str, int]] = []
    for family_id, _blocked in stuck_families:
        for waiter in sorted(remaining_by_family[family_id]):
            if cross_blocker_count[waiter] <= 0:
                continue
            for dependency in deps[waiter]:
                if dependency not in remaining:
                    continue
                dep_family = address_to_parent.get(dependency)
                if dep_family is None or dep_family == family_id:
                    continue
                cross_edges.append((waiter, dependency, dep_family))
                if len(cross_edges) >= limit:
                    return tuple(stuck_families), tuple(cross_edges)
    return tuple(stuck_families), tuple(cross_edges)


def _family_atomization_stats(
    eligible: tuple[FormulaCluster, ...],
    units: tuple[RefactorUnit, ...],
    kind_counts_by_parent: dict[int, dict[EmitKind, int]],
) -> tuple[FamilyAtomizationStats, ...]:
    member_count_by_id = {
        cluster.cluster_id: len(cluster.members) for cluster in eligible
    }
    unit_count: dict[int, int] = defaultdict(int)
    singleton_units: dict[int, int] = defaultdict(int)

    for unit in units:
        unit_count[unit.parent_cluster_id] += 1
        if len(unit.members) == 1:
            singleton_units[unit.parent_cluster_id] += 1

    stats = [
        FamilyAtomizationStats(
            parent_cluster_id=family_id,
            member_count=member_count,
            unit_count=unit_count.get(family_id, 0),
            dag_emits=kind_counts_by_parent.get(family_id, {}).get("dag", 0),
            whole_family_emits=kind_counts_by_parent.get(family_id, {}).get(
                "whole_family", 0
            ),
            peel_emits=kind_counts_by_parent.get(family_id, {}).get("peel", 0),
            singleton_units=singleton_units.get(family_id, 0),
        )
        for family_id, member_count in member_count_by_id.items()
    ]
    stats.sort(
        key=lambda item: (
            -item.unit_count,
            -item.singleton_units,
            -item.member_count,
            item.parent_cluster_id,
        )
    )
    return tuple(stats)


def _build_schedule_diagnostics(
    *,
    path: SchedulePath,
    eligible: tuple[FormulaCluster, ...],
    units: tuple[RefactorUnit, ...],
    kind_counts_by_parent: dict[int, dict[EmitKind, int]],
    decisions: tuple[ScheduleEmitDecision, ...],
) -> ScheduleDiagnostics:
    dag_emits = 0
    whole_family_emits = 0
    peel_emits = 0
    for counts in kind_counts_by_parent.values():
        dag_emits += counts.get("dag", 0)
        whole_family_emits += counts.get("whole_family", 0)
        peel_emits += counts.get("peel", 0)
    return ScheduleDiagnostics(
        path=path,
        fingerprint_family_count=len(eligible),
        schedule_unit_count=len(units),
        dag_emits=dag_emits,
        whole_family_emits=whole_family_emits,
        peel_emits=peel_emits,
        families_by_unit_count=_family_atomization_stats(
            eligible, units, kind_counts_by_parent
        ),
        decisions=decisions,
    )


def _empty_emit_kind_counts() -> dict[EmitKind, int]:
    return {"dag": 0, "whole_family": 0, "peel": 0}


def _schedule_refactor_units_on_cycle(
    projection: ClusterableGraph,
    eligible: tuple[FormulaCluster, ...],
    *,
    record_decisions: bool,
) -> tuple[
    tuple[RefactorUnit, ...],
    tuple[ScheduleEmitDecision, ...],
    dict[int, dict[EmitKind, int]],
]:
    """Schedule through a stuck inter-family graph by preferring whole families.

    Loop:
    1. If any remaining family has no outstanding cross-family hinges, schedule
       that whole remaining family as one unit (``cluster_id`` ascending).
    2. Otherwise peel one family's within-family ready frontier (leafmost seeds,
       upward closure stopping at external walls), choosing among peelable
       families with the unblock heuristic.
    3. Repeat — peels are only a fallback when no whole family is ready.
    """
    clusters_by_id = {cluster.cluster_id: cluster for cluster in eligible}
    address_to_parent: dict[str, int] = {}
    remaining_by_family: dict[int, set[str]] = {}
    for cluster in eligible:
        remaining_by_family[cluster.cluster_id] = set(cluster.members)
        for address in cluster.members:
            address_to_parent[address] = cluster.cluster_id

    remaining = {address for cluster in eligible for address in cluster.members}
    deps, dependents = _member_adjacency(projection, remaining)
    blocker_count = {
        address: sum(1 for dependency in deps[address] if dependency in remaining)
        for address in remaining
    }
    cross_blocker_count = {
        address: sum(
            1
            for dependency in deps[address]
            if dependency in remaining
            and address_to_parent.get(dependency) != address_to_parent[address]
        )
        for address in remaining
    }
    # Members in each family that still have a cross-family hinge.
    family_cross_blocked: dict[int, int] = {
        family_id: sum(1 for address in members if cross_blocker_count[address] > 0)
        for family_id, members in remaining_by_family.items()
    }
    ready: set[str] = set()
    blocker_buckets: dict[int, set[str]] = {}
    for address, count in blocker_count.items():
        if count == 0:
            ready.add(address)
        else:
            blocker_buckets.setdefault(count, set()).add(address)

    units: list[RefactorUnit] = []
    decisions: list[ScheduleEmitDecision] = []
    kind_counts_by_parent: dict[int, dict[EmitKind, int]] = defaultdict(
        _empty_emit_kind_counts
    )
    refactor_group_id = 0

    def _emit(
        parent_id: int,
        batch: tuple[str, ...],
        *,
        kind: EmitKind,
        blocking_family_sample: tuple[tuple[int, int], ...] = (),
        blocking_cross_edge_sample: tuple[tuple[str, str, int], ...] = (),
    ) -> None:
        nonlocal refactor_group_id
        parent = clusters_by_id[parent_id]
        units.append(
            RefactorUnit(
                parent_cluster_id=parent_id,
                refactor_group_id=refactor_group_id,
                members=batch,
                canonical_template=parent.canonical_template,
                row=_row_for_members(batch),
            )
        )
        kind_counts_by_parent[parent_id][kind] += 1
        if record_decisions:
            decisions.append(
                ScheduleEmitDecision(
                    kind=kind,
                    parent_cluster_id=parent_id,
                    member_count=len(batch),
                    blocking_family_sample=blocking_family_sample,
                    blocking_cross_edge_sample=blocking_cross_edge_sample,
                )
            )
        refactor_group_id += 1
        family_remaining = remaining_by_family[parent_id]
        for address in batch:
            remaining.remove(address)
            family_remaining.discard(address)
            ready.discard(address)
            if cross_blocker_count[address] > 0:
                family_cross_blocked[parent_id] -= 1
            for dependent in dependents.get(address, ()):
                if dependent not in remaining:
                    continue
                _decrease_blocker(
                    dependent,
                    blocker_count=blocker_count,
                    blocker_buckets=blocker_buckets,
                    ready=ready,
                )
                if address_to_parent[dependent] != address_to_parent[address]:
                    old_cross = cross_blocker_count[dependent]
                    cross_blocker_count[dependent] = old_cross - 1
                    if old_cross == 1:
                        family_cross_blocked[address_to_parent[dependent]] -= 1

    while remaining:
        ready_family_ids = sorted(
            family_id
            for family_id, members in remaining_by_family.items()
            if members and family_cross_blocked[family_id] == 0
        )
        if ready_family_ids:
            parent_id = ready_family_ids[0]
            batch = tuple(sorted(remaining_by_family[parent_id]))
            _emit(parent_id, batch, kind="whole_family")
            continue

        peel_by_family: dict[int, list[str]] = {}
        for family_id, family_remaining in remaining_by_family.items():
            if not family_remaining:
                continue
            peel = _within_family_peel(
                family_id,
                family_remaining=family_remaining,
                ready=ready,
                remaining=remaining,
                deps=deps,
                dependents=dependents,
                address_to_parent=address_to_parent,
            )
            if peel:
                peel_by_family[family_id] = list(peel)

        if not peel_by_family:
            raise ValueError(
                "No whole family or within-family peel remains but schedule is incomplete"
            )

        parent_id = _select_ready_parent_cluster(
            peel_by_family,
            blocker_buckets=blocker_buckets,
            deps=deps,
        )
        family_sample: tuple[tuple[int, int], ...] = ()
        edge_sample: tuple[tuple[str, str, int], ...] = ()
        if record_decisions:
            family_sample, edge_sample = _sample_blocking_cross_evidence(
                remaining_by_family=remaining_by_family,
                family_cross_blocked=family_cross_blocked,
                cross_blocker_count=cross_blocker_count,
                deps=deps,
                remaining=remaining,
                address_to_parent=address_to_parent,
                exclude_family_id=parent_id,
            )
        _emit(
            parent_id,
            tuple(sorted(peel_by_family[parent_id])),
            kind="peel",
            blocking_family_sample=family_sample,
            blocking_cross_edge_sample=edge_sample,
        )

    return tuple(units), tuple(decisions), dict(kind_counts_by_parent)


def compute_refactor_schedule(
    projection: ClusterableGraph,
    clusters: tuple[FormulaCluster, ...],
) -> tuple[RefactorUnit, ...]:
    """Return refactor units in dependency order (dependencies first).

    When the inter-family cluster graph is acyclic, each eligible family is one
    unit in the same order as ``compute_cluster_refactor_order``. When families
    form a cycle, prefer scheduling whole remaining families whenever any is
    free of cross-family hinges; only then peel a within-family ready frontier
    to unblock the family DAG.
    """
    units, _diagnostics = compute_refactor_schedule_with_diagnostics(
        projection,
        clusters,
        include_decisions=False,
    )
    return units


def compute_refactor_schedule_with_diagnostics(
    projection: ClusterableGraph,
    clusters: tuple[FormulaCluster, ...],
    *,
    include_decisions: bool = False,
) -> tuple[tuple[RefactorUnit, ...], ScheduleDiagnostics]:
    """Return refactor units plus summary schedule diagnostics.

    Summary aggregates (path, emit counts, per-family unit stats) are always
    returned. Per-emit ``decisions`` (including peel hinge samples) are omitted
    unless ``include_decisions=True``.
    """
    eligible = tuple(cluster for cluster in clusters if cluster.members)
    if not eligible:
        empty = ScheduleDiagnostics(
            path="dag",
            fingerprint_family_count=0,
            schedule_unit_count=0,
            dag_emits=0,
            whole_family_emits=0,
            peel_emits=0,
            families_by_unit_count=(),
            decisions=(),
        )
        return (), empty

    started = time.perf_counter()
    dag_order = _try_inter_family_dag_order(projection, eligible)
    if dag_order is not None:
        units = tuple(
            RefactorUnit(
                parent_cluster_id=cluster.cluster_id,
                refactor_group_id=index,
                members=cluster.members,
                canonical_template=cluster.canonical_template,
                row=cluster.row,
            )
            for index, cluster in enumerate(dag_order)
        )
        path: SchedulePath = "dag"
        kind_counts_by_parent: dict[int, dict[EmitKind, int]] = {
            cluster.cluster_id: {"dag": 1, "whole_family": 0, "peel": 0}
            for cluster in dag_order
        }
        decisions = (
            tuple(
                ScheduleEmitDecision(
                    kind="dag",
                    parent_cluster_id=cluster.cluster_id,
                    member_count=len(cluster.members),
                )
                for cluster in dag_order
            )
            if include_decisions
            else ()
        )
    else:
        units, decisions, kind_counts_by_parent = _schedule_refactor_units_on_cycle(
            projection,
            eligible,
            record_decisions=include_decisions,
        )
        path = "cycle_split"

    elapsed = time.perf_counter() - started
    singleton_schedule_units = sum(1 for unit in units if len(unit.members) == 1)
    multi_member_schedule_units = len(units) - singleton_schedule_units
    multi_member_families = sum(1 for cluster in eligible if len(cluster.members) > 1)
    singleton_families = len(eligible) - multi_member_families
    logger.info(
        "refactor schedule path=%s "
        "fingerprint_families=%d "
        "(multi_member_families=%d singleton_families=%d) "
        "schedule_units=%d "
        "(multi_member_schedule_units=%d singleton_schedule_units=%d) "
        "elapsed=%.3fs",
        path,
        len(eligible),
        multi_member_families,
        singleton_families,
        len(units),
        multi_member_schedule_units,
        singleton_schedule_units,
        elapsed,
    )
    diagnostics = _build_schedule_diagnostics(
        path=path,
        eligible=eligible,
        units=units,
        kind_counts_by_parent=kind_counts_by_parent,
        decisions=decisions,
    )
    return units, diagnostics


def compute_cluster_refactor_order(
    projection: ClusterableGraph,
    clusters: tuple[FormulaCluster, ...],
) -> tuple[FormulaCluster, ...]:
    """Return refactorable clusters in dependency order (dependencies first).

    Among clusters with no remaining inbound edges, ``cluster_id`` ascending
    breaks ties deterministically.
    """
    eligible = tuple(cluster for cluster in clusters if cluster.members)
    if not eligible:
        return ()

    dag_order = _try_inter_family_dag_order(projection, eligible)
    if dag_order is None:
        raise ValueError("Cycle detected in cluster refactor dependencies")

    return dag_order
