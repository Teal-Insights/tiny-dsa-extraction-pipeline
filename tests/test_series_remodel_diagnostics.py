"""Tests for shredded-series / inter-series SCC remodel diagnostics."""

from __future__ import annotations

from typing import cast

from excel_grapher.exporter import ProjectionResult

from src.formula_clustering import FormulaCluster
from src.refactor_order import RefactorUnit, compute_refactor_schedule
from src.series_remodel_diagnostics import (
    RemodelRecommendation,
    build_inter_series_depends_on,
    recommend_series_remodels,
    shredded_series_from_schedule,
    strongly_connected_series_groups,
)


class _ZipperProjection:
    """Two row-series zippered across period columns: B2→B1→C2→C1→D2→D1."""

    def get_dependencies(self, address: str) -> tuple[str, ...]:
        deps = {
            "Engine!B2": (),
            "Engine!B1": ("Engine!B2",),
            "Engine!C2": ("Engine!B1",),
            "Engine!C1": ("Engine!C2",),
            "Engine!D2": ("Engine!C1",),
            "Engine!D1": ("Engine!D2",),
        }
        return deps.get(address, ())


def _zipper_clusters() -> tuple[FormulaCluster, FormulaCluster]:
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!B1", "Engine!C1", "Engine!D1"),
        canonical_template="=RATIO",
        row=1,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B2", "Engine!C2", "Engine!D2"),
        canonical_template="=LAG",
        row=2,
    )
    return family_a, family_b


def _zipper_series_map() -> dict[str, str]:
    return {
        "Engine!B1": "row_series_a",
        "Engine!C1": "row_series_a",
        "Engine!D1": "row_series_a",
        "Engine!B2": "row_series_b",
        "Engine!C2": "row_series_b",
        "Engine!D2": "row_series_b",
    }


def test_inter_series_depends_on_builds_cross_series_edges_only() -> None:
    projection = cast(ProjectionResult, _ZipperProjection())
    address_to_series_id = _zipper_series_map()
    depends = build_inter_series_depends_on(
        projection,
        address_to_series_id,
        addresses=tuple(address_to_series_id),
    )
    assert depends["row_series_a"] == {"row_series_b"}
    assert depends["row_series_b"] == {"row_series_a"}


def test_strongly_connected_series_groups_finds_zipper_scc() -> None:
    depends = {
        "row_series_a": {"row_series_b"},
        "row_series_b": {"row_series_a"},
        "unrelated_series": set(),
    }
    groups = strongly_connected_series_groups(depends)
    assert groups == (("row_series_a", "row_series_b"),)


def test_shredded_series_from_schedule_marks_zippered_families() -> None:
    projection = cast(ProjectionResult, _ZipperProjection())
    clusters = _zipper_clusters()
    units = compute_refactor_schedule(
        projection,
        clusters,
    )
    assert len(units) == 6
    shredded = shredded_series_from_schedule(
        clusters=clusters,
        units=units,
        address_to_series_id=_zipper_series_map(),
    )
    by_id = {item.series_id: item for item in shredded}
    assert by_id["row_series_a"].unit_count == 3
    assert by_id["row_series_a"].member_count == 3
    assert by_id["row_series_a"].singleton_units == 3
    assert by_id["row_series_b"].unit_count == 3


def test_shredded_series_does_not_double_count_mixed_family_members_or_units() -> None:
    """One fingerprint family spanning two series must not credit full family totals to each.

    Under ``clustering_mode="ast"`` a family can mix series. Attribution must use
    each series' own member addresses and only the schedule units that actually
    contain those addresses — not the whole family's member/unit counts.
    """
    mixed = FormulaCluster(
        cluster_id=0,
        members=("Engine!B1", "Engine!C1", "Engine!D1", "Engine!E1"),
        canonical_template="=MIXED",
        row=None,
    )
    address_to_series_id = {
        "Engine!B1": "series_x",
        "Engine!D1": "series_x",
        "Engine!C1": "series_y",
        "Engine!E1": "series_y",
    }
    units = (
        RefactorUnit(
            parent_cluster_id=0,
            refactor_group_id=0,
            members=("Engine!B1",),
            canonical_template="=MIXED",
            row=1,
        ),
        RefactorUnit(
            parent_cluster_id=0,
            refactor_group_id=1,
            members=("Engine!C1",),
            canonical_template="=MIXED",
            row=1,
        ),
        RefactorUnit(
            parent_cluster_id=0,
            refactor_group_id=2,
            members=("Engine!D1", "Engine!E1"),
            canonical_template="=MIXED",
            row=1,
        ),
    )
    # Pre-fix double-count would set each series to members=4 and units=3.
    shredded = shredded_series_from_schedule(
        clusters=(mixed,),
        units=units,
        address_to_series_id=address_to_series_id,
    )
    by_id = {item.series_id: item for item in shredded}
    assert by_id["series_x"].member_count == 2
    assert by_id["series_y"].member_count == 2
    assert by_id["series_x"].unit_count == 2  # g0 + g2
    assert by_id["series_y"].unit_count == 2  # g1 + g2
    assert by_id["series_x"].singleton_units == 1
    assert by_id["series_y"].singleton_units == 1
    assert by_id["series_x"].family_count == 1
    assert sum(item.member_count for item in shredded) == 4


def test_recommend_series_remodels_scc_totals_dedupe_shared_units() -> None:
    """SCC fan-out metrics must count each schedule unit/member once, not per series.

    Mixed-series fingerprint families (``ast``-style) attributed fully to each
    series would inflate summed recommendation totals above the true schedule.
    """
    family_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!B1", "Engine!C2"),
        canonical_template="=MIX_A",
        row=None,
    )
    family_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B2", "Engine!C1"),
        canonical_template="=MIX_B",
        row=None,
    )

    class _MixedFamilyZipper:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!B2": (),
                "Engine!B1": ("Engine!B2",),
                "Engine!C2": ("Engine!B1",),
                "Engine!C1": ("Engine!C2",),
            }
            return deps.get(address, ())

    # Each family mixes both series — the latent double-count case.
    address_to_series_id = {
        "Engine!B1": "series_x",
        "Engine!C1": "series_x",
        "Engine!B2": "series_y",
        "Engine!C2": "series_y",
    }
    units = compute_refactor_schedule(
        cast(ProjectionResult, _MixedFamilyZipper()),
        (family_a, family_b),
    )
    assert len(units) == 3
    recommendations = recommend_series_remodels(
        projection=cast(ProjectionResult, _MixedFamilyZipper()),
        clusters=(family_a, family_b),
        units=units,
        address_to_series_id=address_to_series_id,
    )
    assert len(recommendations) == 1
    rec = recommendations[0]
    assert rec.series_ids == ("series_x", "series_y")
    # 4 cells / 3 units total — not 8/6 from summing per-series double-counts.
    assert rec.member_count == 4
    assert rec.unit_count == 3


def test_recommend_series_remodels_for_shredded_scc() -> None:
    projection = cast(ProjectionResult, _ZipperProjection())
    clusters = _zipper_clusters()
    units = compute_refactor_schedule(
        projection,
        clusters,
    )
    recommendations = recommend_series_remodels(
        projection=projection,
        clusters=clusters,
        units=units,
        address_to_series_id=_zipper_series_map(),
    )
    assert len(recommendations) == 1
    rec = recommendations[0]
    assert isinstance(rec, RemodelRecommendation)
    assert rec.series_ids == ("row_series_a", "row_series_b")
    assert rec.suggested_layouts == ("series", "matrix")
    assert rec.unit_count == 6
    assert rec.member_count == 6
    assert "cyclical" in rec.reason.lower() or "cycle" in rec.reason.lower()
    assert "layout: series" in rec.reason
    assert "indicator" in rec.reason.lower()
    assert "time_period" in rec.reason.lower()
    assert "matrix" in rec.suggested_layouts
    assert "column_series" not in rec.suggested_layouts
    assert "row_series" not in rec.reason
    assert any("Engine!B1" in edge or "Engine!B2" in edge for edge in rec.sample_edges)


def test_recommend_series_remodels_skips_acyclic_intact_series() -> None:
    cluster_a = FormulaCluster(
        cluster_id=0,
        members=("Engine!B1", "Engine!C1"),
        canonical_template="=1",
        row=1,
    )
    cluster_b = FormulaCluster(
        cluster_id=1,
        members=("Engine!B2", "Engine!C2"),
        canonical_template="=A",
        row=2,
    )

    class _DagProjection:
        def get_dependencies(self, address: str) -> tuple[str, ...]:
            deps = {
                "Engine!B1": (),
                "Engine!C1": ("Engine!B1",),
                "Engine!B2": ("Engine!B1",),
                "Engine!C2": ("Engine!B2", "Engine!C1"),
            }
            return deps.get(address, ())

    address_to_series_id = {
        "Engine!B1": "series_a",
        "Engine!C1": "series_a",
        "Engine!B2": "series_b",
        "Engine!C2": "series_b",
    }
    units = compute_refactor_schedule(
        cast(ProjectionResult, _DagProjection()),
        (cluster_a, cluster_b),
    )
    recommendations = recommend_series_remodels(
        projection=cast(ProjectionResult, _DagProjection()),
        clusters=(cluster_a, cluster_b),
        units=units,
        address_to_series_id=address_to_series_id,
    )
    assert recommendations == ()
