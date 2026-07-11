from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

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

from src.refactor_bindings import BindingKeyValue, expected_keys_for_address
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address

ClusterableGraph: TypeAlias = DependencyGraph | ProjectionResult

StructuralFingerprint: TypeAlias = tuple[tuple, tuple[str, ...]]

VariationMode: TypeAlias = Literal["independent", "dominant_key_only"]

BoundAddressKeys: TypeAlias = Mapping[str, Mapping[str, BindingKeyValue]]


@dataclass
class _ClusteringKeyCache:
    bound_address_keys: BoundAddressKeys
    workbook_path: Path | None = None
    layout: ProjectionColumnLayout | None = None
    _concept_cache: dict[str, tuple[str, ...] | None] = field(
        default_factory=dict, repr=False
    )
    _value_cache: dict[str, dict[str, BindingKeyValue] | None] = field(
        default_factory=dict, repr=False
    )

    def warm_from_formulas(self, formulas: Mapping[str, str]) -> None:
        for formula in formulas.values():
            fingerprint = structural_fingerprint(formula)
            if fingerprint is None:
                continue
            for address in fingerprint[1]:
                self.concepts_for_address(address)
                self.values_for_address(address)

    def concepts_for_address(self, address: str) -> tuple[str, ...] | None:
        if address not in self._concept_cache:
            self._concept_cache[address] = self._resolve_concepts(address)
        return self._concept_cache[address]

    def values_for_address(self, address: str) -> dict[str, BindingKeyValue] | None:
        if address not in self._value_cache:
            self._value_cache[address] = self._resolve_values(address)
        return self._value_cache[address]

    def _resolve_concepts(self, address: str) -> tuple[str, ...] | None:
        if self.workbook_path is not None:
            keys = expected_keys_for_address(
                address,
                bound_address_keys=self.bound_address_keys,
                workbook_path=self.workbook_path,
                layout=self.layout,
            )
            return tuple(sorted(keys))
        raw_keys = self.bound_address_keys.get(address)
        if raw_keys is None:
            return None
        return tuple(sorted(raw_keys))

    def _resolve_values(self, address: str) -> dict[str, BindingKeyValue] | None:
        if self.workbook_path is not None:
            return expected_keys_for_address(
                address,
                bound_address_keys=self.bound_address_keys,
                workbook_path=self.workbook_path,
                layout=self.layout,
            )
        raw_keys = self.bound_address_keys.get(address)
        if raw_keys is None:
            return None
        return dict(raw_keys)


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


def _formula_body(normalized_formula: str) -> str:
    return (
        normalized_formula[1:]
        if normalized_formula.startswith("=")
        else normalized_formula
    )


def _binding_key_concepts_for_address(
    address: str,
    bound_address_keys: BoundAddressKeys,
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> tuple[str, ...] | None:
    if key_cache is not None:
        return key_cache.concepts_for_address(address)
    if workbook_path is not None:
        keys = expected_keys_for_address(
            address,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        return tuple(sorted(keys))
    keys = bound_address_keys.get(address)
    if keys is None:
        return None
    return tuple(sorted(keys))


def _structural_tuple(
    node: AstNode,
    refs: list[str],
    *,
    bound_address_keys: BoundAddressKeys | None = None,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> tuple:
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
        ref_index = refs.index(node.address)
        if bound_address_keys is None:
            return ("ref", ref_index)
        key_concepts = _binding_key_concepts_for_address(
            node.address,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )
        if key_concepts is None:
            return ("ref", ref_index, None)
        return ("ref", ref_index, key_concepts)
    if isinstance(node, RangeNode):
        for address in (node.start, node.end):
            if address not in refs:
                refs.append(address)
        start_index = refs.index(node.start)
        end_index = refs.index(node.end)
        if bound_address_keys is None:
            return ("range", start_index, end_index)
        start_keys = _binding_key_concepts_for_address(
            node.start,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )
        end_keys = _binding_key_concepts_for_address(
            node.end,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )
        if start_keys is None or end_keys is None:
            return ("range", start_index, end_index, None, None)
        return ("range", start_index, end_index, start_keys, end_keys)
    if isinstance(node, WholeColumnNode):
        return ("wcol", node.sheet, node.column)
    if isinstance(node, WholeRowNode):
        return ("wrow", node.sheet, node.row)
    if isinstance(node, EmptyArgNode):
        return ("empty",)
    if isinstance(node, UnaryOpNode):
        return (
            "unary",
            node.op,
            _structural_tuple(
                node.operand,
                refs,
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            ),
        )
    if isinstance(node, BinaryOpNode):
        return (
            "bin",
            node.op,
            _structural_tuple(
                node.left,
                refs,
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            ),
            _structural_tuple(
                node.right,
                refs,
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            ),
        )
    if isinstance(node, FunctionCallNode):
        return (
            "fn",
            node.name,
            tuple(
                _structural_tuple(
                    arg,
                    refs,
                    bound_address_keys=bound_address_keys,
                    workbook_path=workbook_path,
                    layout=layout,
                    key_cache=key_cache,
                )
                for arg in node.args
            ),
        )
    raise TypeError(type(node))


def structural_fingerprint(
    normalized_formula: str,
    *,
    bound_address_keys: BoundAddressKeys | None = None,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> StructuralFingerprint | None:
    """Return ``(skeleton, refs)`` with refs in deterministic AST visit order."""
    try:
        ast = parse(_formula_body(normalized_formula))
    except FormulaParseError:
        return None
    refs: list[str] = []
    return (
        _structural_tuple(
            ast,
            refs,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        ),
        tuple(refs),
    )


def _binding_aware_fingerprint_complete(
    fingerprint: StructuralFingerprint,
) -> bool:
    skeleton, _refs = fingerprint

    def walk(node: tuple) -> bool:
        if not node:
            return True
        kind = node[0]
        if kind == "ref":
            return len(node) == 3 and node[2] is not None
        if kind == "range":
            return len(node) == 5 and node[3] is not None and node[4] is not None
        if kind in {"num", "str", "bool", "empty"}:
            return True
        if kind == "err":
            return True
        if kind in {"wcol", "wrow"}:
            return True
        if kind == "unary":
            return walk(node[2])
        if kind == "bin":
            return walk(node[2]) and walk(node[3])
        if kind == "fn":
            return all(walk(arg) for arg in node[2])
        return False

    return walk(skeleton)


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
    bound_address_keys: BoundAddressKeys | None = None,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> bool:
    """Return whether two normalized formulas belong in the same parameterizable bucket."""
    left_fingerprint = structural_fingerprint(
        left_formula,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )
    right_fingerprint = structural_fingerprint(
        right_formula,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )
    if left_fingerprint is None or right_fingerprint is None:
        return False
    left_skeleton, left_refs = left_fingerprint
    right_skeleton, right_refs = right_fingerprint
    if left_skeleton != right_skeleton:
        return False

    if bound_address_keys is not None:
        if not _binding_aware_fingerprint_complete(
            left_fingerprint
        ) or not _binding_aware_fingerprint_complete(right_fingerprint):
            return False
        if len(left_refs) != len(right_refs):
            return False
        return True

    deltas = _reference_deltas(left_refs, right_refs)
    if deltas is None:
        return False
    return _single_axis_reference_deltas(deltas)


def _should_cluster(
    left_formula: str,
    right_formula: str,
    *,
    bound_address_keys: BoundAddressKeys | None,
    workbook_path: Path | None,
    layout: ProjectionColumnLayout | None,
    key_cache: _ClusteringKeyCache | None,
) -> bool:
    return formulas_are_parameterizable(
        left_formula,
        right_formula,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )


def _ref_position_key_values(
    member_address: str,
    formula: str,
    bound_address_keys: BoundAddressKeys,
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> list[dict[str, BindingKeyValue]] | None:
    fingerprint = structural_fingerprint(
        formula,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )
    if fingerprint is None or not _binding_aware_fingerprint_complete(fingerprint):
        return None
    _skeleton, refs = fingerprint
    values_by_ref: list[dict[str, BindingKeyValue]] = []
    for ref_address in refs:
        if key_cache is not None:
            keys = key_cache.values_for_address(ref_address)
            if keys is None:
                return None
            values_by_ref.append(keys)
            continue
        if workbook_path is not None:
            keys = expected_keys_for_address(
                ref_address,
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
            )
        else:
            raw_keys = bound_address_keys.get(ref_address)
            if raw_keys is None:
                return None
            keys = dict(raw_keys)
        values_by_ref.append(keys)
    return values_by_ref


def _dominant_varying_concepts_at_ref(
    members: tuple[str, ...],
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    ref_index: int,
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> frozenset[str]:
    concept_values: dict[str, set[BindingKeyValue]] = {}
    for member in members:
        ref_values = _ref_position_key_values(
            member,
            formula_nodes[member],
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )
        if ref_values is None or ref_index >= len(ref_values):
            return frozenset()
        for concept, value in ref_values[ref_index].items():
            concept_values.setdefault(concept, set()).add(value)
    return frozenset(
        concept for concept, values in concept_values.items() if len(values) > 1
    )


def _dominant_key_for_ref(
    members: tuple[str, ...],
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    ref_index: int,
    varying_concepts: frozenset[str],
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> str | None:
    if not varying_concepts:
        return None
    counts: dict[str, int] = {}
    for concept in varying_concepts:
        values: set[BindingKeyValue] = set()
        for member in members:
            ref_values = _ref_position_key_values(
                member,
                formula_nodes[member],
                bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            )
            if ref_values is None or ref_index >= len(ref_values):
                continue
            if concept in ref_values[ref_index]:
                values.add(ref_values[ref_index][concept])
        counts[concept] = len(values)
    return max(counts, key=lambda concept: (counts[concept], concept))


def _dominant_key_split_signature(
    member: str,
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    ref_index: int,
    dominant_key: str,
    varying_concepts: frozenset[str],
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> tuple[BindingKeyValue, ...] | None:
    ref_values = _ref_position_key_values(
        member,
        formula_nodes[member],
        bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )
    if ref_values is None or ref_index >= len(ref_values):
        return None
    keys_at_ref = ref_values[ref_index]
    signature: list[BindingKeyValue] = []
    for concept in sorted(varying_concepts):
        if concept == dominant_key:
            continue
        if concept not in keys_at_ref:
            return None
        signature.append(keys_at_ref[concept])
    return tuple(signature)


def _split_cluster_by_dominant_keys(
    members: tuple[str, ...],
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> tuple[tuple[str, ...], ...]:
    if len(members) < 2:
        return (members,)

    canonical_formula = formula_nodes[members[0]]
    fingerprint = structural_fingerprint(
        canonical_formula,
        bound_address_keys=bound_address_keys,
        workbook_path=workbook_path,
        layout=layout,
        key_cache=key_cache,
    )
    if fingerprint is None:
        return (members,)
    _skeleton, refs = fingerprint
    if not refs:
        return (members,)

    groups: dict[tuple[tuple[BindingKeyValue, ...], ...], list[str]] = {}
    for member in members:
        signatures: list[tuple[BindingKeyValue, ...]] = []
        for ref_index in range(len(refs)):
            varying = _dominant_varying_concepts_at_ref(
                members,
                formula_nodes,
                bound_address_keys,
                ref_index,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            )
            dominant = _dominant_key_for_ref(
                members,
                formula_nodes,
                bound_address_keys,
                ref_index,
                varying,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            )
            if dominant is None:
                signatures.append(())
                continue
            signature = _dominant_key_split_signature(
                member,
                formula_nodes,
                bound_address_keys,
                ref_index,
                dominant,
                varying,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            )
            if signature is None:
                signatures.append(())
            else:
                signatures.append(signature)
        groups.setdefault(tuple(signatures), []).append(member)

    return tuple(
        tuple(sorted(group_members))
        for _signature, group_members in sorted(
            groups.items(), key=lambda item: item[1]
        )
    )


def formula_nodes_for_clustering(graph: ClusterableGraph) -> dict[str, str]:
    """Return non-leaf formula addresses and normalized formulas from a clusterable graph."""
    return _formula_nodes(graph)


def cluster_has_independent_operand_variation(
    cluster: FormulaCluster,
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    varying_concepts: frozenset[str],
    *,
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
    key_cache: _ClusteringKeyCache | None = None,
) -> bool:
    """Return whether a cluster needs unsupported per-operand parameterization."""
    if not varying_concepts:
        return False

    for concept in varying_concepts:
        operand_patterns: list[tuple[BindingKeyValue, ...]] = []
        for member in cluster.members:
            ref_values = _ref_position_key_values(
                member,
                formula_nodes[member],
                bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            )
            if ref_values is None:
                continue
            pattern = tuple(
                ref_keys[concept] for ref_keys in ref_values if concept in ref_keys
            )
            if pattern:
                operand_patterns.append(pattern)

        if not operand_patterns:
            continue

        if (
            len(set(operand_patterns)) == 1
            and len(operand_patterns[0]) > 1
            and len(set(operand_patterns[0])) > 1
        ):
            return True

        if len(set(operand_patterns)) > 1 and all(
            len(pattern) > 1 and len(set(pattern)) == len(pattern)
            for pattern in operand_patterns
        ):
            return True
    return False


def cluster_graph_formulas(
    graph: ClusterableGraph,
    *,
    bound_address_keys: BoundAddressKeys | None = None,
    variation_mode: VariationMode = "independent",
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
) -> tuple[FormulaCluster, ...]:
    """Cluster non-leaf formula nodes by AST shape and binding-aware ref placeholders."""
    formula_nodes = _formula_nodes(graph)
    addresses = sorted(formula_nodes)

    key_cache: _ClusteringKeyCache | None = None
    if bound_address_keys is not None:
        key_cache = _ClusteringKeyCache(
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        key_cache.warm_from_formulas(formula_nodes)

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
                formula_nodes[left_address],
                formula_nodes[right_address],
                bound_address_keys=bound_address_keys,
                workbook_path=workbook_path,
                layout=layout,
                key_cache=key_cache,
            ):
                union(left_address, right_address)

    grouped: dict[str, list[str]] = {}
    for address in addresses:
        grouped.setdefault(find(address), []).append(address)

    raw_clusters: list[tuple[str, ...]] = []
    for _root, members in sorted(grouped.items(), key=lambda item: item[1]):
        ordered_members = tuple(sorted(members))
        if (
            variation_mode == "dominant_key_only"
            and bound_address_keys is not None
            and len(ordered_members) >= 2
        ):
            raw_clusters.extend(
                _split_cluster_by_dominant_keys(
                    ordered_members,
                    formula_nodes,
                    bound_address_keys,
                    workbook_path=workbook_path,
                    layout=layout,
                    key_cache=key_cache,
                )
            )
        else:
            raw_clusters.append(ordered_members)

    clusters: list[FormulaCluster] = []
    for cluster_id, ordered_members in enumerate(
        sorted(raw_clusters, key=lambda members: members)
    ):
        if not ordered_members:
            continue
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
