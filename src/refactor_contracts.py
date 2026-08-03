"""Shape-based selection between the two cluster-refactor contracts.

Contract A (``member_sweep``) keeps today's rules: helper parameters are
exactly the varying binding keys of the member cells, and derivable operand
offsets (e.g. a constant ``t - 1`` lag) stay in the helper body. Contract B
(``dimension_aware``) applies when a cluster's formula operands vary
independently along one concept and every such operand role is routable from
a distinct member dimension id (e.g. ``REF_AREA`` vs ``COUNTERPART_REF_AREA``);
parameters and member keys are then keyed by effective dimension id so
counterpart parameters do not collide.

Selection works position by position: for each varying dimension that the
operand-variation detector flags, every reference position carrying that
dimension must be *covered* — its value must be a constant offset of the
member's own key for some member dimension sharing the concept. Positions
covered only by the dimension itself are derivable in the helper body
(Contract A); positions needing a counterpart dimension id require Contract
B; an uncovered position returns ``None`` (historically labeled
``operand_level_variation_unsupported``). Since #132 a ``None`` here is a routing
hint rather than a hard skip: the caller (``build_cluster_refactor_context``)
attempts key-dispatch and then a verified member_sweep mechanical draft before
skipping, so a cluster this selector cannot route may still be refactored when
mechanical synthesis reproduces it (e.g. via an operand lookup/lag).

``variation_mode`` controls whether dimension-aware clusters are even formed
(``dominant_key_only`` splits them away); the contract itself is always
selected from cluster shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from src.formula_clustering import (
    BoundAddressKeys,
    FormulaCluster,
    _ref_position_key_values,
    cluster_has_independent_operand_variation,
)
from src.refactor_bindings import (
    BindingKeyValue,
    KeyConceptSpec,
    expected_keys_for_address,
)
from src.workbook_addresses import ProjectionColumnLayout

type ClusterRefactorContract = Literal[
    "member_sweep",
    "dimension_aware",
    "key_dispatch",
]


def concepts_with_multiple_dimensions(
    dimension_ids: frozenset[str],
    key_vocabulary: Sequence[KeyConceptSpec],
) -> dict[str, tuple[str, ...]]:
    """Group the given dimension ids by concept, keeping concepts with >1 id."""
    concept_by_dimension = {item.dimension_id: item.concept for item in key_vocabulary}
    grouped: dict[str, list[str]] = {}
    for dimension_id in sorted(dimension_ids):
        concept = concept_by_dimension.get(dimension_id)
        if concept is None:
            continue
        grouped.setdefault(concept, []).append(dimension_id)
    return {
        concept: tuple(dimension_ids)
        for concept, dimension_ids in grouped.items()
        if len(dimension_ids) > 1
    }


def _dimensions_with_operand_variation(
    cluster: FormulaCluster,
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    varying_dimension_ids: frozenset[str],
    *,
    workbook_path: Path | None,
    layout: ProjectionColumnLayout | None,
) -> frozenset[str]:
    return frozenset(
        dimension_id
        for dimension_id in varying_dimension_ids
        if cluster_has_independent_operand_variation(
            cluster,
            formula_nodes,
            bound_address_keys,
            frozenset({dimension_id}),
            workbook_path=workbook_path,
            layout=layout,
        )
    )


def _member_key_values(
    address: str,
    bound_address_keys: BoundAddressKeys,
    *,
    workbook_path: Path | None,
    layout: ProjectionColumnLayout | None,
) -> dict[str, BindingKeyValue]:
    if workbook_path is not None:
        return expected_keys_for_address(
            address,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
    return dict(bound_address_keys.get(address) or {})


def _position_covered_by_dimension(
    values_by_member: Mapping[str, BindingKeyValue],
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    dimension_id: str,
) -> bool:
    """Return whether one operand position is a constant offset of a member key.

    Covered means every member's operand value equals its own key for
    ``dimension_id`` plus one shared constant (zero for exact routing; any
    constant for numeric values, which is a derivable lag/offset).
    """
    offsets: set[BindingKeyValue] = set()
    for member, value in values_by_member.items():
        key = member_keys[member].get(dimension_id)
        if key is None:
            return False
        if value == key:
            offsets.add(0)
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isinstance(key, (int, float))
            and not isinstance(key, bool)
        ):
            offsets.add(value - key)
        else:
            return False
    return len(offsets) == 1


def select_cluster_refactor_contract(
    cluster: FormulaCluster,
    formula_nodes: Mapping[str, str],
    bound_address_keys: BoundAddressKeys,
    varying_dimension_ids: frozenset[str],
    *,
    key_vocabulary: Sequence[KeyConceptSpec],
    workbook_path: Path | None = None,
    layout: ProjectionColumnLayout | None = None,
) -> ClusterRefactorContract | None:
    """Select the refactor contract for one cluster from its shape.

    Returns ``"member_sweep"`` (Contract A) when every operand position is
    derivable from the member cells' own sweep keys (including constant
    lags/offsets), ``"dimension_aware"`` (Contract B) when some operand
    positions instead route through counterpart dimension ids sharing the
    concept, and ``None`` when any operand position cannot be routed by the
    declared bindings. Since #132 a ``None`` is a routing hint, not a verdict:
    the caller may still refactor the cluster if verified mechanical synthesis
    reproduces it (see this module's docstring).
    """
    flagged = _dimensions_with_operand_variation(
        cluster,
        formula_nodes,
        bound_address_keys,
        varying_dimension_ids,
        workbook_path=workbook_path,
        layout=layout,
    )
    if not flagged:
        return "member_sweep"

    concept_by_dimension = {item.dimension_id: item.concept for item in key_vocabulary}
    member_keys = {
        member: _member_key_values(
            member,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        for member in cluster.members
    }
    ref_values_by_member = {
        member: _ref_position_key_values(
            member,
            formula_nodes[member],
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        for member in cluster.members
    }

    needs_counterpart = False
    for dimension_id in sorted(flagged):
        concept = concept_by_dimension.get(dimension_id)
        counterpart_dimension_ids = [
            candidate
            for candidate in sorted(varying_dimension_ids)
            if candidate != dimension_id
            and concept is not None
            and concept_by_dimension.get(candidate) == concept
        ]

        patterns: dict[str, tuple[BindingKeyValue, ...]] = {}
        for member in cluster.members:
            ref_values = ref_values_by_member[member]
            if ref_values is None:
                continue
            pattern = tuple(
                ref_keys[dimension_id]
                for ref_keys in ref_values
                if dimension_id in ref_keys
            )
            if pattern:
                patterns[member] = pattern

        pattern_lengths = {len(pattern) for pattern in patterns.values()}
        if len(pattern_lengths) != 1:
            return None
        for position in range(pattern_lengths.pop()):
            values_by_member = {
                member: pattern[position] for member, pattern in patterns.items()
            }
            if _position_covered_by_dimension(
                values_by_member, member_keys, dimension_id
            ):
                continue
            if any(
                _position_covered_by_dimension(values_by_member, member_keys, candidate)
                for candidate in counterpart_dimension_ids
            ):
                needs_counterpart = True
                continue
            return None

    return "dimension_aware" if needs_counterpart else "member_sweep"
