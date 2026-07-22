"""Unit tests for scripts.inspect_cluster formatting helpers."""

from __future__ import annotations

from src.formula_clustering import FormulaCluster
from src.refactor_order import RefactorUnit
from scripts.inspect_cluster import (
    find_cluster,
    format_cluster_report,
    schedule_units_for_family,
)


def test_find_cluster_returns_matching_family() -> None:
    clusters = (
        FormulaCluster(
            cluster_id=1,
            members=("Engine!C1",),
            canonical_template="=1",
            row=1,
        ),
        FormulaCluster(
            cluster_id=365,
            members=("Engine!C10", "Engine!D10"),
            canonical_template="=Inputs!{col}1",
            row=10,
        ),
    )
    found = find_cluster(clusters, 365)
    assert found is not None
    assert found.cluster_id == 365
    assert find_cluster(clusters, 999) is None


def test_schedule_units_for_family_filters_by_parent_id() -> None:
    units = (
        RefactorUnit(
            parent_cluster_id=365,
            refactor_group_id=0,
            members=("Engine!C10",),
            canonical_template="=Inputs!{col}1",
            row=10,
        ),
        RefactorUnit(
            parent_cluster_id=12,
            refactor_group_id=1,
            members=("Engine!C2",),
            canonical_template="=2",
            row=2,
        ),
        RefactorUnit(
            parent_cluster_id=365,
            refactor_group_id=2,
            members=("Engine!D10",),
            canonical_template="=Inputs!{col}1",
            row=10,
        ),
    )
    peels = schedule_units_for_family(units, 365)
    assert [unit.refactor_group_id for unit in peels] == [0, 2]


def test_format_cluster_report_includes_shape_series_and_sources() -> None:
    cluster = FormulaCluster(
        cluster_id=365,
        members=("Engine!C10", "Engine!D10"),
        canonical_template="=Inputs!{col}1",
        row=10,
    )
    units = (
        RefactorUnit(
            parent_cluster_id=365,
            refactor_group_id=0,
            members=("Engine!C10",),
            canonical_template="=Inputs!{col}1",
            row=10,
        ),
        RefactorUnit(
            parent_cluster_id=365,
            refactor_group_id=1,
            members=("Engine!D10",),
            canonical_template="=Inputs!{col}1",
            row=10,
        ),
    )
    report = format_cluster_report(
        cluster,
        address_to_series_id={
            "Engine!C10": "mystery_series",
            "Engine!D10": "mystery_series",
        },
        formulas={
            "Engine!C10": "=Inputs!C1",
            "Engine!D10": "=Inputs!D1",
        },
        schedule_units=units,
        sources={
            "Engine!C10": "def cell_engine_c10(ctx):\n    return mystery(ctx)\n",
            "Engine!D10": "def cell_engine_d10(ctx):\n    return mystery(ctx)\n",
        },
    )
    assert "cluster_id: 365" in report
    assert "members: 2" in report
    assert "canonical_template: =Inputs!{col}1" in report
    assert "Engine!C10" in report
    assert "mystery_series" in report
    assert "=Inputs!C1" in report
    assert "schedule units: 2" in report
    assert "cluster_365_g0" in report
    assert "return mystery(ctx)" in report
    assert "[0]" in report
