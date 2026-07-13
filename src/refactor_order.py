from __future__ import annotations

from dataclasses import dataclass

from src.formula_clustering import ClusterableGraph, FormulaCluster
from src.workbook_addresses import parse_workbook_address


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
    inbound: dict[int, set[int]] = {cluster.cluster_id: set() for cluster in eligible}
    for cluster_id, prerequisites in depends_on.items():
        for prerequisite_id in prerequisites:
            inbound[cluster_id].add(prerequisite_id)

    remaining_inbound = {
        cluster_id: len(edges) for cluster_id, edges in inbound.items()
    }
    ready = sorted(
        cluster_id for cluster_id, count in remaining_inbound.items() if count == 0
    )
    ordered_ids: list[int] = []

    while ready:
        cluster_id = ready.pop(0)
        ordered_ids.append(cluster_id)
        for dependent_id, prerequisites in inbound.items():
            if cluster_id not in prerequisites:
                continue
            remaining_inbound[dependent_id] -= 1
            if remaining_inbound[dependent_id] == 0:
                ready.append(dependent_id)
        ready.sort()

    if len(ordered_ids) != len(eligible):
        return None

    return tuple(eligible_by_id[cluster_id] for cluster_id in ordered_ids)


def _try_inter_family_dag_order(
    projection: ClusterableGraph,
    eligible: tuple[FormulaCluster, ...],
) -> tuple[FormulaCluster, ...] | None:
    depends_on = _inter_family_depends_on(projection, eligible)
    return _kahn_cluster_order(eligible, depends_on)


def _is_member_ready(
    address: str,
    *,
    remaining: set[str],
    scheduled_members: set[str],
    projection: ClusterableGraph,
) -> bool:
    for dependency in projection.get_dependencies(address):
        if dependency in scheduled_members:
            continue
        if dependency in remaining:
            return False
    return True


def _member_blocker_count(
    address: str,
    *,
    remaining: set[str],
    scheduled_members: set[str],
    projection: ClusterableGraph,
) -> int:
    blockers = 0
    for dependency in projection.get_dependencies(address):
        if dependency in scheduled_members:
            continue
        if dependency in remaining:
            blockers += 1
    return blockers


def _select_ready_parent_cluster(
    ready_by_family: dict[int, list[str]],
    *,
    ready: frozenset[str],
    remaining: set[str],
    scheduled_members: set[str],
    projection: ClusterableGraph,
) -> int:
    if len(ready_by_family) == 1:
        return next(iter(ready_by_family))

    waiting = tuple(sorted(address for address in remaining if address not in ready))
    if not waiting:
        return min(
            ready_by_family,
            key=lambda parent_id: (-len(ready_by_family[parent_id]), parent_id),
        )

    min_blockers = min(
        _member_blocker_count(
            address,
            remaining=remaining,
            scheduled_members=scheduled_members,
            projection=projection,
        )
        for address in waiting
    )
    priority_waiters = frozenset(
        address
        for address in waiting
        if _member_blocker_count(
            address,
            remaining=remaining,
            scheduled_members=scheduled_members,
            projection=projection,
        )
        == min_blockers
    )

    def unblocks_score(parent_id: int) -> int:
        batch = frozenset(ready_by_family[parent_id])
        return sum(
            1
            for waiter in priority_waiters
            if any(
                dependency in batch
                for dependency in projection.get_dependencies(waiter)
            )
        )

    return min(
        ready_by_family,
        key=lambda parent_id: (
            -unblocks_score(parent_id),
            -len(ready_by_family[parent_id]),
            parent_id,
        ),
    )


def _schedule_refactor_units_on_cycle(
    projection: ClusterableGraph,
    eligible: tuple[FormulaCluster, ...],
) -> tuple[RefactorUnit, ...]:
    clusters_by_id = {cluster.cluster_id: cluster for cluster in eligible}
    address_to_parent: dict[str, int] = {}
    for cluster in eligible:
        for address in cluster.members:
            address_to_parent[address] = cluster.cluster_id

    remaining = {address for cluster in eligible for address in cluster.members}
    scheduled_members: set[str] = set()
    units: list[RefactorUnit] = []
    refactor_group_id = 0

    while remaining:
        ready = tuple(
            sorted(
                address
                for address in remaining
                if _is_member_ready(
                    address,
                    remaining=remaining,
                    scheduled_members=scheduled_members,
                    projection=projection,
                )
            )
        )
        if not ready:
            raise ValueError(
                "No refactor-ready members remain but schedule is incomplete"
            )

        ready_by_family: dict[int, list[str]] = {}
        for address in ready:
            parent_id = address_to_parent[address]
            ready_by_family.setdefault(parent_id, []).append(address)

        ready_set = frozenset(ready)
        parent_id = _select_ready_parent_cluster(
            ready_by_family,
            ready=ready_set,
            remaining=remaining,
            scheduled_members=scheduled_members,
            projection=projection,
        )
        batch = tuple(sorted(ready_by_family[parent_id]))
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
        refactor_group_id += 1
        for address in batch:
            remaining.remove(address)
            scheduled_members.add(address)

    return tuple(units)


def compute_refactor_schedule(
    projection: ClusterableGraph,
    clusters: tuple[FormulaCluster, ...],
) -> tuple[RefactorUnit, ...]:
    """Return refactor units in dependency order (dependencies first).

    When the inter-family cluster graph is acyclic, each eligible family is one
    unit in the same order as ``compute_cluster_refactor_order``. When families
    form a cycle, members are scheduled in ready subsets that respect the
    cell-level dependency graph.
    """
    eligible = tuple(cluster for cluster in clusters if cluster.members)
    if not eligible:
        return ()

    dag_order = _try_inter_family_dag_order(projection, eligible)
    if dag_order is not None:
        return tuple(
            RefactorUnit(
                parent_cluster_id=cluster.cluster_id,
                refactor_group_id=index,
                members=cluster.members,
                canonical_template=cluster.canonical_template,
                row=cluster.row,
            )
            for index, cluster in enumerate(dag_order)
        )

    return _schedule_refactor_units_on_cycle(projection, eligible)


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
