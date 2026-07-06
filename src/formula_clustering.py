from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph

from src.workbook_addresses import parse_workbook_address

ClusterableGraph: TypeAlias = DependencyGraph | ProjectionResult

# Default normalized-formula Levenshtein ratio for parallel formula families.
DEFAULT_SIMILARITY_THRESHOLD = 0.16


@dataclass(frozen=True)
class FormulaCluster:
    """A group of workbook cells whose normalized formulas cluster by similarity."""

    cluster_id: int
    members: tuple[str, ...]
    canonical_template: str
    row: int | None


def levenshtein_distance(left: str, right: str) -> int:
    """Return the Levenshtein edit distance between two strings."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            insert_cost = current[right_index - 1] + 1
            delete_cost = previous[right_index] + 1
            replace_cost = previous[right_index - 1] + (left_char != right_char)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def levenshtein_ratio(left: str, right: str) -> float:
    """Return normalized Levenshtein distance in [0, 1]."""
    if left == right:
        return 0.0
    max_len = max(len(left), len(right))
    if max_len == 0:
        return 0.0
    return levenshtein_distance(left, right) / max_len


def _projected_graph(graph: ClusterableGraph) -> DependencyGraph:
    if isinstance(graph, ProjectionResult):
        return graph.projected_graph
    return graph


def _formula_nodes(graph: ClusterableGraph) -> dict[str, str]:
    projected = _projected_graph(graph)
    nodes: dict[str, str] = {}
    for address in projected:
        node = projected.get_node(address)
        if node is None or node.is_leaf or node.normalized_formula is None:
            continue
        nodes[address] = node.normalized_formula
    return nodes


def _cluster_row_key(address: str) -> tuple[str, int]:
    sheet, _column, row = parse_workbook_address(address)
    return sheet, row


def _should_cluster(
    left_address: str,
    left_formula: str,
    right_address: str,
    right_formula: str,
    *,
    similarity_threshold: float,
    require_same_row: bool,
) -> bool:
    if require_same_row:
        if _cluster_row_key(left_address) != _cluster_row_key(right_address):
            return False
    return levenshtein_ratio(left_formula, right_formula) <= similarity_threshold


def cluster_graph_formulas(
    graph: ClusterableGraph,
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    require_same_row: bool = True,
) -> tuple[FormulaCluster, ...]:
    """Cluster non-leaf formula nodes by ``normalized_formula`` similarity."""
    formula_nodes = _formula_nodes(graph)
    addresses = sorted(formula_nodes)

    parent = {address: address for address in addresses}

    def find(address: str) -> str:
        while parent[address] != address:
            parent[address] = parent[parent[address]]
            address = parent[address]
        return address

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left_address in enumerate(addresses):
        for right_address in addresses[left_index + 1 :]:
            if _should_cluster(
                left_address,
                formula_nodes[left_address],
                right_address,
                formula_nodes[right_address],
                similarity_threshold=similarity_threshold,
                require_same_row=require_same_row,
            ):
                union(left_address, right_address)

    grouped: dict[str, list[str]] = {}
    for address in addresses:
        grouped.setdefault(find(address), []).append(address)

    clusters: list[FormulaCluster] = []
    for cluster_id, (_root, members) in enumerate(
        sorted(grouped.items(), key=lambda item: item[1])
    ):
        ordered_members = tuple(sorted(members))
        template = formula_nodes[ordered_members[0]]
        rows = {parse_workbook_address(address)[2] for address in ordered_members}
        row = next(iter(rows)) if len(rows) == 1 else None
        clusters.append(
            FormulaCluster(
                cluster_id=cluster_id,
                members=ordered_members,
                canonical_template=template,
                row=row,
            )
        )
    return tuple(clusters)
