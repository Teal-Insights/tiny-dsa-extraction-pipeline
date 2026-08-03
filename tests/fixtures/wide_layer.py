from __future__ import annotations

from excel_grapher.grapher.graph import DependencyGraph

from src.workbook_addresses import parse_workbook_address
from tests.fixtures.inter_cluster_cycle import formula_node, leaf_node


def wide_layer_graph() -> tuple[DependencyGraph, dict[str, dict[str, int]]]:
    """Three independent formula cells plus one cell depending on all three.

    Produces a two-layer schedule: units for B2/C2/D2 share no edges, while E2
    reads every one of them.
    """
    graph = DependencyGraph()
    sheet, column, row = parse_workbook_address("Inputs!A2")
    graph.add_node(leaf_node(sheet, column, row, 1))

    formulas = {
        "Engine!B2": "=Inputs!A2+1",
        "Engine!C2": "=Inputs!A2*2",
        "Engine!D2": "=MAX(Inputs!A2,3)",
        "Engine!E2": "=Engine!B2+Engine!C2*Engine!D2",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(formula_node(sheet, column, row, formula))

    for dependent, dependency in (
        ("Engine!B2", "Inputs!A2"),
        ("Engine!C2", "Inputs!A2"),
        ("Engine!D2", "Inputs!A2"),
        ("Engine!E2", "Engine!B2"),
        ("Engine!E2", "Engine!C2"),
        ("Engine!E2", "Engine!D2"),
    ):
        graph.add_edge(dependent, dependency)

    bindings = {
        "Inputs!A2": {"TIME_PERIOD": 1},
        "Engine!B2": {"TIME_PERIOD": 1},
        "Engine!C2": {"TIME_PERIOD": 1},
        "Engine!D2": {"TIME_PERIOD": 1},
        "Engine!E2": {"TIME_PERIOD": 1},
    }
    return graph, bindings
