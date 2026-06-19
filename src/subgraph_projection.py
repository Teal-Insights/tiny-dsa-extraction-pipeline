from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from excel_grapher.exporter import (
    BaseProjectionManifest,
    CollapsedGroup,
    FormulaRewrite,
    IdentityTransitCompression,
    ProjectedNodeSnapshot,
    ProjectionResult,
    apply_projection,
)
from excel_grapher.grapher.graph import DependencyGraph

TINY_DSA_HYPOTHESIS_GROUPS: dict[str, tuple[str, ...]] = {
    "Group 1": (
        "Engine!C10",
        "Engine!C14",
        "Engine!C15",
        "Engine!C16",
        "Engine!C20",
    ),
    "Group 2": (
        "Engine!D10",
        "Engine!D14",
        "Engine!D15",
        "Engine!D16",
        "Engine!D20",
    ),
    "Group 3": (
        "Engine!E10",
        "Engine!E14",
        "Engine!E15",
        "Engine!E16",
        "Engine!E20",
    ),
    "Group 4": (
        "Engine!F10",
        "Engine!F14",
        "Engine!F15",
        "Engine!F16",
        "Engine!F20",
    ),
    "Group 5": (
        "Engine!G10",
        "Engine!G14",
        "Engine!G15",
        "Engine!G16",
        "Engine!G20",
    ),
    "Group 6": ("Engine!B6", "Engine!C6"),
}


@dataclass(frozen=True)
class _FormulaPair:
    formula: str
    normalized_formula: str


class SubgraphCollapse:
    """Collapse configured single-root subgraphs into projected formula nodes."""

    def __init__(self, groups: Mapping[str, Sequence[str]]) -> None:
        self._groups = {label: tuple(keys) for label, keys in groups.items()}

    def project(self, graph: DependencyGraph) -> ProjectionResult:
        projected = graph.copy()
        removed_node_snapshots: dict[str, ProjectedNodeSnapshot] = {}
        retained_to_collapsed_sources: dict[str, tuple[str, ...]] = {}
        collapsed_groups: list[CollapsedGroup] = []
        formula_rewrites: list[FormulaRewrite] = []

        for _label, group_keys in self._groups.items():
            root = _find_group_root(projected, group_keys)
            members = set(group_keys)
            removed = tuple(key for key in group_keys if key != root)

            for key in removed:
                outside_dependents = set(projected.get_dependents(key)) - members
                if outside_dependents:
                    raise ValueError(
                        f"Cannot delete {key}; outside dependents: {outside_dependents}"
                    )

            root_node_before = projected.get_node(root)
            if root_node_before is None:
                raise KeyError(f"Cell {root} not found in graph")

            expanded = _expanded_group_formula(projected, root, members)
            external_dependencies = tuple(
                sorted(
                    {
                        dependency
                        for key in group_keys
                        for dependency in projected.get_dependencies(key)
                        if dependency not in members
                    }
                )
            )

            formula_rewrites.append(
                FormulaRewrite(
                    dependent=root,
                    before_formula=root_node_before.formula,
                    after_formula=expanded.formula,
                    before_normalized=root_node_before.normalized_formula,
                    after_normalized=expanded.normalized_formula,
                )
            )

            projected.set_node_formula(
                root,
                expanded.formula,
                expanded.normalized_formula,
            )
            _set_collapsed_metadata(projected, root, group_keys)
            _add_external_dependencies(projected, root, external_dependencies)

            for key in removed:
                snapshot = _snapshot_node(projected, key)
                removed_node_snapshots[key] = snapshot
                projected.remove_node(key)

            retained_to_collapsed_sources[root] = removed
            collapsed_groups.append(
                CollapsedGroup(
                    retained=root,
                    collapsed_sources=removed,
                    statement_order=tuple(group_keys),
                    external_dependencies=external_dependencies,
                )
            )

        manifest = BaseProjectionManifest(
            kind="tiny_dsa_subgraph_collapse",
            forwarding_map={},
            retained_to_collapsed_sources=retained_to_collapsed_sources,
            removed_node_snapshots=removed_node_snapshots,
            formula_rewrites=tuple(formula_rewrites),
            collapsed_groups=tuple(collapsed_groups),
        )
        return ProjectionResult(
            original_graph=graph,
            projected_graph=projected,
            manifest=manifest,
        )


def build_tiny_dsa_refactor_projection(graph: DependencyGraph) -> ProjectionResult:
    """Build the Tiny DSA projection used for refactor-oriented exports."""
    return apply_projection(
        graph,
        [
            SubgraphCollapse(TINY_DSA_HYPOTHESIS_GROUPS),
            IdentityTransitCompression(),
        ],
    )


def _snapshot_node(graph: DependencyGraph, key: str) -> ProjectedNodeSnapshot:
    node = graph.get_node(key)
    if node is None:
        raise KeyError(f"Cell {key} not found in graph")
    return ProjectedNodeSnapshot(
        address=key,
        sheet=node.sheet,
        column=node.column,
        row=node.row,
        formula=node.formula,
        normalized_formula=node.normalized_formula,
        value=node.value,
        is_target=node.is_target,
        is_leaf=node.is_leaf,
        metadata=dict(node.metadata),
    )


def _find_group_root(graph: DependencyGraph, group_keys: Sequence[str]) -> str:
    members = set(group_keys)
    roots = [
        key for key in group_keys if not (set(graph.get_dependents(key)) & members)
    ]
    if len(roots) != 1:
        raise ValueError(f"Expected one group root for {group_keys}, found {roots}")
    return roots[0]


def _expanded_group_formula(
    graph: DependencyGraph,
    key: str,
    members: set[str],
    visiting: set[str] | None = None,
) -> _FormulaPair:
    visiting = set() if visiting is None else visiting
    if key in visiting:
        raise ValueError(f"Cycle found while expanding {key}")
    visiting.add(key)

    node = graph.get_node(key)
    if node is None:
        raise KeyError(f"Cell {key} not found in graph")
    if node.formula is None or node.normalized_formula is None:
        raise ValueError(f"Cannot substitute non-formula group member {key}")

    formula = node.formula
    normalized_formula = node.normalized_formula
    for dependency in sorted(set(graph.get_dependencies(key)) & members):
        expanded_dependency = _expanded_group_formula(
            graph,
            dependency,
            members,
            visiting,
        )
        formula = _substitute_cell_reference(
            formula,
            dependency,
            expanded_dependency.formula,
        )
        normalized_formula = _substitute_cell_reference(
            normalized_formula,
            dependency,
            expanded_dependency.normalized_formula,
        )

    visiting.remove(key)
    return _FormulaPair(formula=formula, normalized_formula=normalized_formula)


def _formula_body(formula: str) -> str:
    return formula[1:] if formula.startswith("=") else formula


def _substitute_cell_reference(
    formula: str,
    cell_key: str,
    replacement_formula: str,
) -> str:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.$!':]){re.escape(cell_key)}(?![A-Za-z0-9_.$:])"
    )
    replacement = f"({_formula_body(replacement_formula)})"
    substituted, replacements = pattern.subn(replacement, formula)
    if replacements > 0:
        return substituted

    local_reference = cell_key.split("!", 1)[1]
    local_pattern = re.compile(
        rf"(?<![A-Za-z0-9_.$!':]){re.escape(local_reference)}(?![A-Za-z0-9_.$:])"
    )
    substituted, replacements = local_pattern.subn(replacement, formula)
    if replacements == 0:
        raise ValueError(f"Expected {cell_key} or {local_reference} in {formula!r}")
    return substituted


def _add_external_dependencies(
    graph: DependencyGraph,
    key: str,
    dependencies: Sequence[str],
) -> None:
    for dependency in dependencies:
        graph.add_edge(key, dependency)


def _set_collapsed_metadata(
    graph: DependencyGraph,
    root: str,
    group_keys: Sequence[str],
) -> None:
    node = graph.get_node(root)
    if node is None:
        raise KeyError(f"Cell {root} not found in graph")
    metadata = dict(node.metadata)
    metadata["collapsed_from"] = tuple(group_keys)
    graph.set_node_metadata(root, metadata)
