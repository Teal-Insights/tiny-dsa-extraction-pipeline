from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from excel_grapher.core.formula_ast import (
    AstNode,
    BinaryOpNode,
    BoolNode,
    CellRefNode,
    EmptyArgNode,
    ErrorNode,
    FormulaParseError,
    FunctionCallNode,
    NumberNode,
    RangeNode,
    StringNode,
    UnaryOpNode,
    WholeColumnNode,
    WholeRowNode,
    parse,
)
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph
from fastpyxl.utils.cell import column_index_from_string

from src.workbook_addresses import parse_workbook_address

ClusterableGraph: TypeAlias = DependencyGraph | ProjectionResult

StructuralFingerprint: TypeAlias = tuple[tuple, tuple[str, ...]]


@dataclass(frozen=True)
class FormulaCluster:
    """A group of workbook cells whose normalized formulas share one AST shape."""

    cluster_id: int
    members: tuple[str, ...]
    canonical_template: str
    row: int | None


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


def _formula_body(normalized_formula: str) -> str:
    return (
        normalized_formula[1:]
        if normalized_formula.startswith("=")
        else normalized_formula
    )


def _structural_tuple(node: AstNode, refs: list[str]) -> tuple:
    if isinstance(node, NumberNode):
        return ("num",)
    if isinstance(node, StringNode):
        return ("str",)
    if isinstance(node, BoolNode):
        return ("bool",)
    if isinstance(node, ErrorNode):
        return ("err", str(node.error))
    if isinstance(node, CellRefNode):
        if node.address not in refs:
            refs.append(node.address)
        return ("ref", refs.index(node.address))
    if isinstance(node, RangeNode):
        for address in (node.start, node.end):
            if address not in refs:
                refs.append(address)
        return ("range", refs.index(node.start), refs.index(node.end))
    if isinstance(node, WholeColumnNode):
        return ("wcol", node.sheet, node.column)
    if isinstance(node, WholeRowNode):
        return ("wrow", node.sheet, node.row)
    if isinstance(node, EmptyArgNode):
        return ("empty",)
    if isinstance(node, UnaryOpNode):
        return ("unary", node.op, _structural_tuple(node.operand, refs))
    if isinstance(node, BinaryOpNode):
        return (
            "bin",
            node.op,
            _structural_tuple(node.left, refs),
            _structural_tuple(node.right, refs),
        )
    if isinstance(node, FunctionCallNode):
        return (
            "fn",
            node.name,
            tuple(_structural_tuple(arg, refs) for arg in node.args),
        )
    raise TypeError(type(node))


def structural_fingerprint(
    normalized_formula: str,
) -> StructuralFingerprint | None:
    """Return ``(skeleton, refs)`` with refs in deterministic AST visit order."""
    try:
        ast = parse(_formula_body(normalized_formula))
    except FormulaParseError:
        return None
    refs: list[str] = []
    return (_structural_tuple(ast, refs), tuple(refs))


def _reference_deltas(
    left_refs: tuple[str, ...], right_refs: tuple[str, ...]
) -> list[tuple[int, int]] | None:
    if len(left_refs) != len(right_refs):
        return None
    deltas: list[tuple[int, int]] = []
    for left_ref, right_ref in zip(left_refs, right_refs, strict=True):
        left_sheet, left_col, left_row = parse_workbook_address(left_ref)
        right_sheet, right_col, right_row = parse_workbook_address(right_ref)
        if left_sheet != right_sheet:
            return None
        left_col_index = column_index_from_string(left_col)
        right_col_index = column_index_from_string(right_col)
        deltas.append((right_col_index - left_col_index, right_row - left_row))
    return deltas


def _single_axis_reference_deltas(deltas: list[tuple[int, int]]) -> bool:
    if not deltas:
        return True
    col_deltas = {delta[0] for delta in deltas}
    row_deltas = {delta[1] for delta in deltas}
    if col_deltas == {0} and row_deltas == {0}:
        return True
    if len(row_deltas) == 1 and row_deltas == {0}:
        return True
    if len(col_deltas) == 1 and col_deltas == {0}:
        return True
    for col_delta, row_delta in deltas:
        if col_delta != 0 and row_delta != 0:
            return False
    cols_vary = any(delta[0] != 0 for delta in deltas)
    rows_vary = any(delta[1] != 0 for delta in deltas)
    return not (cols_vary and rows_vary)


def formulas_are_parameterizable(
    left_formula: str,
    right_formula: str,
    *,
    left_address: str | None = None,
    right_address: str | None = None,
    require_same_row: bool = False,
) -> bool:
    """Return whether two normalized formulas differ only by refs or scalars."""
    left_fingerprint = structural_fingerprint(left_formula)
    right_fingerprint = structural_fingerprint(right_formula)
    if left_fingerprint is None or right_fingerprint is None:
        return False
    left_skeleton, left_refs = left_fingerprint
    right_skeleton, right_refs = right_fingerprint
    if left_skeleton != right_skeleton:
        return False
    if require_same_row:
        if left_address is None or right_address is None:
            raise ValueError(
                "left_address and right_address are required when require_same_row=True"
            )
        if _cluster_row_key(left_address) != _cluster_row_key(right_address):
            return False
    deltas = _reference_deltas(left_refs, right_refs)
    if deltas is None:
        return False
    return _single_axis_reference_deltas(deltas)


def _should_cluster(
    left_address: str,
    left_formula: str,
    right_address: str,
    right_formula: str,
    *,
    require_same_row: bool,
) -> bool:
    return formulas_are_parameterizable(
        left_formula,
        right_formula,
        left_address=left_address,
        right_address=right_address,
        require_same_row=require_same_row,
    )


def cluster_graph_formulas(
    graph: ClusterableGraph,
    *,
    require_same_row: bool = False,
) -> tuple[FormulaCluster, ...]:
    """Cluster non-leaf formula nodes by AST shape and single-axis ref variation."""
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
