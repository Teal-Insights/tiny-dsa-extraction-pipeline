from __future__ import annotations

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node

from src.workbook_addresses import parse_workbook_address


def formula_node(sheet: str, column: str, row: int, formula: str) -> Node:
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


def leaf_node(sheet: str, column: str, row: int, value: object = 0) -> Node:
    return Node(
        sheet=sheet,
        column=column,
        row=row,
        formula=None,
        normalized_formula=None,
        value=value,
        is_leaf=True,
        metadata={},
    )


def inter_cluster_cycle_graph() -> tuple[DependencyGraph, dict[str, dict[str, int]]]:
    """Acyclic cell graph whose formula clusters form a dependency cycle."""
    graph = DependencyGraph()
    for address, value in (
        ("Inputs!A2", 1),
        ("Inputs!A3", 1),
        ("Inputs!B1", 0),
    ):
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(leaf_node(sheet, column, row, value))

    formulas = {
        "Engine!B2": "=Inputs!A2+Inputs!B1",
        "Engine!C2": "=Inputs!A2*Engine!B2",
        "Engine!B3": "=Inputs!A3+Engine!C2",
        "Engine!C3": "=Inputs!A3*Engine!B3",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(formula_node(sheet, column, row, formula))

    for dependent, dependency in (
        ("Engine!B2", "Inputs!A2"),
        ("Engine!B2", "Inputs!B1"),
        ("Engine!C2", "Inputs!A2"),
        ("Engine!C2", "Engine!B2"),
        ("Engine!B3", "Inputs!A3"),
        ("Engine!B3", "Engine!C2"),
        ("Engine!C3", "Inputs!A3"),
        ("Engine!C3", "Engine!B3"),
    ):
        graph.add_edge(dependent, dependency)

    bindings = {
        "Inputs!A2": {"TIME_PERIOD": 1},
        "Inputs!A3": {"TIME_PERIOD": 2},
        "Inputs!B1": {"TIME_PERIOD": 0},
        "Engine!B2": {"TIME_PERIOD": 1},
        "Engine!C2": {"TIME_PERIOD": 1},
        "Engine!B3": {"TIME_PERIOD": 2},
        "Engine!C3": {"TIME_PERIOD": 2},
    }
    return graph, bindings
