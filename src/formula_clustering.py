from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeAlias

from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph

ClusterableGraph: TypeAlias = DependencyGraph | ProjectionResult

ENGINE_COLUMNS: tuple[str, ...] = ("C", "D", "E", "F", "G")
ENGINE_COLUMN_SET = frozenset(ENGINE_COLUMNS)
OUTPUTS_COLUMN_TO_ENGINE = {"B": "C", "C": "D", "D": "E", "E": "F", "F": "G"}


@dataclass(frozen=True)
class FormulaCluster:
    """A group of workbook cells whose normalized formulas match after canonicalization."""

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


def parse_workbook_address(address: str) -> tuple[str, str, int]:
    sheet, colrow = address.split("!", 1)
    column = "".join(character for character in colrow if character.isalpha())
    row = int("".join(character for character in colrow if character.isdigit()))
    return sheet, column, row


def logical_engine_column(address: str) -> str | None:
    """Map a workbook address to its Engine-sheet projection column, if any."""
    sheet, column, _row = parse_workbook_address(address)
    if sheet == "Engine" and column in ENGINE_COLUMN_SET:
        return column
    if sheet == "Outputs":
        return OUTPUTS_COLUMN_TO_ENGINE.get(column)
    return None


def previous_engine_column(column: str) -> str | None:
    index = ENGINE_COLUMNS.index(column)
    if index == 0:
        return None
    return ENGINE_COLUMNS[index - 1]


def canonicalize_formula_for_clustering(formula: str, address: str) -> str:
    """Normalize column-specific refs so parallel year columns share one template.

    First-year carry-ins are unified as ``{PRIOR_DEBT}`` so year-one cells such as
    ``Engine!C6`` cluster with chain cells such as ``Engine!D6``.
    """
    column = logical_engine_column(address)
    if column is None:
        return formula

    _sheet, _column, row = parse_workbook_address(address)
    canonical = formula
    canonical = re.sub(
        r"(?<![A-Za-z0-9_.$!':])Inputs!B6(?![A-Za-z0-9_.$:])",
        "{PRIOR_DEBT}",
        canonical,
    )

    previous_column = previous_engine_column(column)
    if previous_column is not None:
        canonical = re.sub(
            rf"(?<![A-Za-z0-9_.$!':])Engine!{previous_column}{row}(?![A-Za-z0-9_.$:])",
            "{PRIOR_DEBT}",
            canonical,
        )

    for engine_column in ENGINE_COLUMNS:
        canonical = re.sub(
            rf"Inputs!{engine_column}(\d+)",
            r"Inputs!{COL}\1",
            canonical,
        )
        canonical = re.sub(
            rf"Engine!{engine_column}(\d+)",
            r"Engine!{COL}\1",
            canonical,
        )
    return canonical


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


def _should_cluster(
    left_address: str,
    left_template: str,
    right_address: str,
    right_template: str,
    *,
    similarity_threshold: float,
    require_same_row: bool,
) -> bool:
    if require_same_row:
        left_row = parse_workbook_address(left_address)[2]
        right_row = parse_workbook_address(right_address)[2]
        if left_row != right_row:
            return False
    return levenshtein_ratio(left_template, right_template) <= similarity_threshold


def cluster_graph_formulas(
    graph: ClusterableGraph,
    *,
    similarity_threshold: float = 0.0,
    require_same_row: bool = True,
) -> tuple[FormulaCluster, ...]:
    """Cluster non-leaf formula nodes by canonicalized normalized formula similarity."""
    formula_nodes = _formula_nodes(graph)
    addresses = sorted(formula_nodes)
    templates = {
        address: canonicalize_formula_for_clustering(formula_nodes[address], address)
        for address in addresses
    }

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
                templates[left_address],
                right_address,
                templates[right_address],
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
        template = templates[ordered_members[0]]
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
