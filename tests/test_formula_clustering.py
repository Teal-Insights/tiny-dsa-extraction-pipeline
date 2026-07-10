from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node

from src.formula_clustering import (
    cluster_graph_formulas,
    formulas_are_parameterizable,
    structural_fingerprint,
)
from src.workbook_addresses import parse_workbook_address


def _formula_node(sheet: str, column: str, row: int, formula: str) -> Node:
    return Node(
        sheet=sheet,
        column=column,
        row=row,
        formula=formula,
        normalized_formula=formula,
        value=None,
        is_leaf=False,
        metadata={},
    )


def _debt_to_gdp_anchor_recurrence_graph() -> DependencyGraph:
    """Graph where period-1 anchor differs structurally from the recurrence chain."""
    graph = DependencyGraph()
    formulas = {
        "Engine!C20": ("=Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Engine!C16"),
        "Engine!D20": ("=Engine!C20*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Engine!D16"),
        "Engine!E20": ("=Engine!D20*(1+Inputs!E17/100)/(1+Inputs!D16/100)-Engine!E16"),
        "Engine!F20": ("=Engine!E20*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Engine!F16"),
        "Engine!G20": ("=Engine!F20*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Engine!G16"),
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))
    return graph


def test_structural_fingerprint_abstracts_cell_addresses_and_scalars() -> None:
    left = structural_fingerprint("=Paris!B13+1")
    right_address = structural_fingerprint("=Paris!B14+2")
    assert left is not None
    assert right_address is not None
    assert left[0] == right_address[0]
    assert left[1] != right_address[1]


def test_formulas_are_parameterizable_for_column_sweep() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C13+Inputs!D16"
    assert formulas_are_parameterizable(left, right)


def test_formulas_are_not_parameterizable_for_different_structure() -> None:
    left = "=Paris!B13+1"
    right = "=Paris!B13*2"
    assert not formulas_are_parameterizable(left, right)


def test_formulas_are_not_parameterizable_when_refs_vary_on_both_axes() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C14+Inputs!D16"
    assert not formulas_are_parameterizable(left, right)


def test_formulas_are_not_parameterizable_for_anchor_vs_recurrence() -> None:
    left = "=Inputs!B6*(1+Inputs!C17/100)"
    right = "=Engine!C6*(1+Inputs!D17/100)"
    assert not formulas_are_parameterizable(left, right)


def test_formulas_are_parameterizable_respects_require_same_row() -> None:
    left = "=Paris!B13+1"
    right = "=Paris!B14+1"
    assert formulas_are_parameterizable(left, right, require_same_row=False)
    assert not formulas_are_parameterizable(
        left,
        right,
        left_address="Paris!B13",
        right_address="Paris!B14",
        require_same_row=True,
    )


def test_cluster_graph_formulas_groups_parallel_row_on_synthetic_projection(
    synthetic_projection,
) -> None:
    clusters = cluster_graph_formulas(synthetic_projection)
    engine_cluster = next(
        cluster
        for cluster in clusters
        if set(cluster.members) == {"Engine!B2", "Engine!C2"}
    )
    assert engine_cluster.row == 2
    assert engine_cluster.canonical_template == "=Inputs!A1+Inputs!B1+1"


def test_cluster_graph_formulas_splits_anchor_from_recurrence_chain() -> None:
    clusters = cluster_graph_formulas(
        _debt_to_gdp_anchor_recurrence_graph(),
        require_same_row=False,
    )
    debt_clusters = [
        cluster
        for cluster in clusters
        if any(member.endswith("20") for member in cluster.members)
        and len(cluster.members) >= 2
    ]
    assert len(debt_clusters) == 1
    assert debt_clusters[0].members == (
        "Engine!D20",
        "Engine!E20",
        "Engine!F20",
        "Engine!G20",
    )
    singletons = [cluster for cluster in clusters if cluster.members == ("Engine!C20",)]
    assert len(singletons) == 1
