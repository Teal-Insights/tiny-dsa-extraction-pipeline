from __future__ import annotations

from src.formula_clustering import ClusterableGraph, FormulaCluster


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

    owners = _address_owner_clusters(eligible)
    eligible_by_id = {cluster.cluster_id: cluster for cluster in eligible}
    depends_on: dict[int, set[int]] = {
        cluster.cluster_id: set() for cluster in eligible
    }

    for cluster in eligible:
        for dependency in _external_dependency_addresses(projection, cluster):
            owner = owners.get(dependency)
            if owner is None or owner.cluster_id == cluster.cluster_id:
                continue
            depends_on[cluster.cluster_id].add(owner.cluster_id)

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
        raise ValueError("Cycle detected in cluster refactor dependencies")

    return tuple(eligible_by_id[cluster_id] for cluster_id in ordered_ids)
