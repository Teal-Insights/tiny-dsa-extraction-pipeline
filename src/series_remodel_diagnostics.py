"""Detect shredded series and cyclical inter-series groups for binding remodeling.

Fingerprint families that schedule as many peel units often participate in
period-lag zippers across several parallel ``layout: series`` bindings (one
indicator row keyed by ``TIME_PERIOD``). Schema layouts are only
``scalar`` / ``series`` / ``matrix``. Prefer rebinding as ``layout: series``
along the indicator axis (one binding per period column) for small LLM
refactor batches, or ``layout: matrix`` with indicator × period for one
table ownership.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from src.formula_clustering import ClusterableGraph, FormulaCluster
from src.refactor_order import RefactorUnit
from src.workbook_addresses import parse_workbook_address

SuggestedLayout = Literal["series", "matrix"]
_SAMPLE_EDGE_LIMIT = 8


@dataclass(frozen=True)
class ShreddedSeriesStats:
    """Schedule fan-out for one binding series across its fingerprint families."""

    series_id: str
    member_count: int
    unit_count: int
    singleton_units: int
    family_count: int
    parent_cluster_ids: tuple[int, ...]

    @property
    def is_shredded(self) -> bool:
        return self.unit_count > max(self.family_count, 1)


@dataclass(frozen=True)
class RemodelRecommendation:
    """One cyclical series group that schedule atomization suggests remodeling."""

    series_ids: tuple[str, ...]
    suggested_layouts: tuple[SuggestedLayout, ...]
    reason: str
    member_count: int
    unit_count: int
    singleton_units: int
    parent_cluster_ids: tuple[int, ...]
    sample_edges: tuple[str, ...]
    sheets: tuple[str, ...]


def build_inter_series_depends_on(
    projection: ClusterableGraph,
    address_to_series_id: dict[str, str],
    *,
    addresses: tuple[str, ...] | None = None,
) -> dict[str, set[str]]:
    """Return series_id → prerequisite series ids from cell-level dependencies."""
    scope = addresses if addresses is not None else tuple(address_to_series_id)
    depends_on: dict[str, set[str]] = defaultdict(set)
    for address in scope:
        series_id = address_to_series_id.get(address)
        if series_id is None:
            continue
        depends_on.setdefault(series_id, set())
        for dependency in projection.get_dependencies(address):
            dep_series = address_to_series_id.get(dependency)
            if dep_series is None or dep_series == series_id:
                continue
            depends_on[series_id].add(dep_series)
    return dict(depends_on)


def strongly_connected_series_groups(
    depends_on: dict[str, set[str]],
) -> tuple[tuple[str, ...], ...]:
    """Return multi-member SCCs as sorted series-id tuples (Tarjan)."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[tuple[str, ...]] = []

    nodes = sorted(depends_on)

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for prerequisite in sorted(depends_on.get(node, ())):
            if prerequisite not in depends_on:
                continue
            if prerequisite not in indices:
                strongconnect(prerequisite)
                lowlinks[node] = min(lowlinks[node], lowlinks[prerequisite])
            elif prerequisite in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[prerequisite])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) >= 2:
                sccs.append(tuple(sorted(component)))

    for node in nodes:
        if node not in indices:
            strongconnect(node)

    sccs.sort(key=lambda group: (-len(group), group))
    return tuple(sccs)


def shredded_series_from_schedule(
    *,
    clusters: tuple[FormulaCluster, ...],
    units: tuple[RefactorUnit, ...],
    address_to_series_id: dict[str, str],
) -> tuple[ShreddedSeriesStats, ...]:
    """Aggregate schedule units/members by owning series id.

    Members are counted from addresses owned by the series. Units/singletons
    count only schedule units that contain at least one of those addresses, so
    mixed-series fingerprint families (``ast`` clustering) are not attributed
    in full to every participating series.
    """
    series_families: dict[str, set[int]] = defaultdict(set)
    series_members: dict[str, int] = defaultdict(int)
    series_units: dict[str, int] = defaultdict(int)
    series_singletons: dict[str, int] = defaultdict(int)

    for cluster in clusters:
        for member in cluster.members:
            series_id = address_to_series_id.get(member)
            if series_id is None:
                continue
            series_families[series_id].add(cluster.cluster_id)
            series_members[series_id] += 1

    for unit in units:
        series_in_unit = {
            address_to_series_id[member]
            for member in unit.members
            if member in address_to_series_id
        }
        is_singleton = len(unit.members) == 1
        for series_id in series_in_unit:
            series_units[series_id] += 1
            if is_singleton:
                series_singletons[series_id] += 1

    stats = [
        ShreddedSeriesStats(
            series_id=series_id,
            member_count=series_members[series_id],
            unit_count=series_units.get(series_id, 0),
            singleton_units=series_singletons.get(series_id, 0),
            family_count=len(family_ids),
            parent_cluster_ids=tuple(sorted(family_ids)),
        )
        for series_id, family_ids in series_families.items()
    ]
    stats.sort(
        key=lambda item: (
            -item.unit_count,
            -item.singleton_units,
            -item.member_count,
            item.series_id,
        )
    )
    return tuple(stats)


def _unique_schedule_totals_for_series_group(
    *,
    clusters: tuple[FormulaCluster, ...],
    units: tuple[RefactorUnit, ...],
    series_ids: set[str],
    address_to_series_id: dict[str, str],
) -> tuple[int, int, int, tuple[int, ...]]:
    """Return unique member/unit/singleton counts and parent cluster ids for a series group."""
    members = {
        member
        for cluster in clusters
        for member in cluster.members
        if address_to_series_id.get(member) in series_ids
    }
    matched_units = [
        unit
        for unit in units
        if any(
            address_to_series_id.get(member) in series_ids for member in unit.members
        )
    ]
    singleton_units = sum(1 for unit in matched_units if len(unit.members) == 1)
    parent_cluster_ids = tuple(
        sorted({unit.parent_cluster_id for unit in matched_units})
    )
    return len(members), len(matched_units), singleton_units, parent_cluster_ids


def recommend_series_remodels(
    *,
    projection: ClusterableGraph,
    clusters: tuple[FormulaCluster, ...],
    units: tuple[RefactorUnit, ...],
    address_to_series_id: dict[str, str],
) -> tuple[RemodelRecommendation, ...]:
    """Recommend matrix/column remodel for shredded cyclical series groups."""
    addresses = tuple(member for cluster in clusters for member in cluster.members)
    depends_on = build_inter_series_depends_on(
        projection,
        address_to_series_id,
        addresses=addresses,
    )
    sccs = strongly_connected_series_groups(depends_on)
    shredded = {
        item.series_id: item
        for item in shredded_series_from_schedule(
            clusters=clusters,
            units=units,
            address_to_series_id=address_to_series_id,
        )
        if item.is_shredded
    }

    recommendations: list[RemodelRecommendation] = []
    for series_ids in sccs:
        group_set = set(series_ids)
        shredded_in_group = [shredded[sid] for sid in series_ids if sid in shredded]

        sheets, row_count, column_count, group_addresses = _geometry_for_series_group(
            clusters=clusters,
            series_ids=group_set,
            address_to_series_id=address_to_series_id,
        )
        member_count, unit_count, singleton_units, parent_cluster_ids = (
            _unique_schedule_totals_for_series_group(
                clusters=clusters,
                units=units,
                series_ids=group_set,
                address_to_series_id=address_to_series_id,
            )
        )
        # Remodel when the SCC itself fans out, or any participating series shreds.
        if not shredded_in_group and unit_count <= max(len(parent_cluster_ids), 1):
            continue

        sample_edges = _sample_cross_series_edges(
            projection,
            series_ids=group_set,
            address_to_series_id=address_to_series_id,
            addresses=group_addresses,
        )

        suggested: list[SuggestedLayout] = []
        reason_bits = [
            f"{len(series_ids)} series form a cyclical dependence group",
            f"schedule fan-out {unit_count} units / {member_count} members",
        ]
        if singleton_units:
            reason_bits.append(f"{singleton_units} singleton peels")

        # Prefer layout: series reoriented to period columns; matrix is the
        # full-table alternative. Never invent non-schema layouts.
        if column_count >= 2 and len(series_ids) >= 2:
            suggested.append("series")
            reason_bits.append(
                f"geometry spans {row_count} indicator rows × {column_count} "
                "period columns — today these are parallel layout: series "
                "along TIME_PERIOD (one row each); prefer layout: series along "
                "the indicator axis instead (one binding per period column, "
                "key=INDICATOR) so each schedule/refactor unit is one column "
                "and period t only depends on t-1"
            )
        if row_count >= 2 and column_count >= 2:
            suggested.append("matrix")
            reason_bits.append(
                "layout: matrix with INDICATOR × TIME_PERIOD also collapses "
                "the zipper into one series (AST diversity across rows is fine "
                "under one ownership) — use when a full-table refactor batch "
                "is acceptable"
            )
        if not suggested:
            suggested.extend(["series", "matrix"])
            reason_bits.append(
                "re-author as layout: series along the indicator axis "
                "(preferred for LLM tractability: one period column per "
                "binding) or layout: matrix with INDICATOR × TIME_PERIOD"
            )

        recommendations.append(
            RemodelRecommendation(
                series_ids=series_ids,
                suggested_layouts=tuple(suggested),
                reason="; ".join(reason_bits),
                member_count=member_count,
                unit_count=unit_count,
                singleton_units=singleton_units,
                parent_cluster_ids=parent_cluster_ids,
                sample_edges=sample_edges,
                sheets=sheets,
            )
        )

    recommendations.sort(
        key=lambda item: (-item.unit_count, -item.singleton_units, item.series_ids)
    )
    return tuple(recommendations)


def _sample_cross_series_edges(
    projection: ClusterableGraph,
    *,
    series_ids: set[str],
    address_to_series_id: dict[str, str],
    addresses: tuple[str, ...],
    limit: int = _SAMPLE_EDGE_LIMIT,
) -> tuple[str, ...]:
    samples: list[str] = []
    for address in sorted(addresses):
        series_id = address_to_series_id.get(address)
        if series_id not in series_ids:
            continue
        for dependency in projection.get_dependencies(address):
            dep_series = address_to_series_id.get(dependency)
            if dep_series is None or dep_series == series_id:
                continue
            if dep_series not in series_ids:
                continue
            samples.append(f"{address} <- {dependency} ({series_id} <- {dep_series})")
            if len(samples) >= limit:
                return tuple(samples)
    return tuple(samples)


def _geometry_for_series_group(
    *,
    clusters: tuple[FormulaCluster, ...],
    series_ids: set[str],
    address_to_series_id: dict[str, str],
) -> tuple[tuple[str, ...], int, int, tuple[str, ...]]:
    """Return sheets, distinct rows, distinct columns, and member addresses."""
    sheets: set[str] = set()
    rows: set[int] = set()
    columns: set[str] = set()
    addresses: list[str] = []
    for cluster in clusters:
        for member in cluster.members:
            series_id = address_to_series_id.get(member)
            if series_id not in series_ids:
                continue
            addresses.append(member)
            sheet, column, row = parse_workbook_address(member)
            sheets.add(sheet)
            rows.add(row)
            columns.add(column)
    return tuple(sorted(sheets)), len(rows), len(columns), tuple(sorted(addresses))


def format_remodel_recommendations(
    recommendations: tuple[RemodelRecommendation, ...],
    *,
    shredded: tuple[ShreddedSeriesStats, ...] | None = None,
    top_shredded: int = 15,
    top_recommendations: int = 20,
) -> str:
    """Human-readable report for shredded series and remodel recommendations."""
    lines: list[str] = []
    if shredded is not None:
        shredded_only = [item for item in shredded if item.is_shredded]
        lines.append(
            f"=== shredded series (top {top_shredded}; {len(shredded_only)} total) ==="
        )
        if not shredded_only:
            lines.append("None — every series stayed at one schedule unit per family.")
            lines.append("")
        else:
            for item in shredded_only[:top_shredded]:
                lines.append(
                    f"{item.series_id}: members={item.member_count} "
                    f"units={item.unit_count} singletons={item.singleton_units} "
                    f"families={item.family_count}"
                )
            lines.append("")

    lines.append(
        f"=== remodel recommendations "
        f"(cyclical shredded groups; top {top_recommendations}) ==="
    )
    lines.append(
        "Schema layouts only: scalar | series | matrix. "
        "Preference: layout: series along INDICATOR (one binding per period "
        "column) for LLM tractability; layout: matrix with INDICATOR × "
        "TIME_PERIOD also restores schedule cohesion."
    )
    if not recommendations:
        lines.append(
            "None — no multi-series dependence cycle coincides with schedule shredding."
        )
        lines.append("")
        return "\n".join(lines)

    for index, rec in enumerate(recommendations[:top_recommendations], start=1):
        layouts = ", ".join(rec.suggested_layouts)
        lines.append(
            f"{index}. series ({len(rec.series_ids)}): {', '.join(rec.series_ids)}"
        )
        lines.append(
            f"   suggest: {layouts}; "
            f"units={rec.unit_count} members={rec.member_count} "
            f"singletons={rec.singleton_units}"
        )
        if rec.sheets:
            lines.append(f"   sheets: {', '.join(rec.sheets)}")
        lines.append(f"   reason: {rec.reason}")
        for edge in rec.sample_edges[:5]:
            lines.append(f"   cross hinge: {edge}")
        lines.append("")
    return "\n".join(lines)
