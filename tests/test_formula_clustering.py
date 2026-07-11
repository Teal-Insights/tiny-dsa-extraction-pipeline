from pathlib import Path
from unittest.mock import patch

from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node

from src.formula_clustering import (
    FormulaCluster,
    cluster_graph_formulas,
    cluster_has_independent_operand_variation,
    formulas_are_parameterizable,
    structural_fingerprint,
)
from src.refactor_bindings import expected_keys_for_address
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address


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
        "Engine!E20": ("=Engine!D20*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Engine!E16"),
        "Engine!F20": ("=Engine!E20*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Engine!F16"),
        "Engine!G20": ("=Engine!F20*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Engine!G16"),
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))
    return graph


def _trade_balance_graph() -> DependencyGraph:
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!D5": "=Inputs!B12-Inputs!C12",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))
    return graph


TRADE_BALANCE_BINDINGS = {
    "Inputs!B10": {"REF_AREA": "US", "TIME_PERIOD": 1},
    "Inputs!C10": {"REF_AREA": "CN", "TIME_PERIOD": 1},
    "Inputs!B11": {"REF_AREA": "US", "TIME_PERIOD": 2},
    "Inputs!C11": {"REF_AREA": "CN", "TIME_PERIOD": 2},
    "Inputs!B12": {"REF_AREA": "US", "TIME_PERIOD": 3},
    "Inputs!C12": {"REF_AREA": "CN", "TIME_PERIOD": 3},
}

COLUMN_SWEEP_BINDINGS = {
    "Paris!B13": {"TIME_PERIOD": 1},
    "Inputs!C16": {"REF_AREA": "US"},
    "Paris!C13": {"TIME_PERIOD": 2},
    "Inputs!D16": {"REF_AREA": "DE"},
}

BOTH_AXES_BINDINGS = {
    "Paris!B13": {"TIME_PERIOD": 1},
    "Inputs!C16": {"REF_AREA": "US"},
    "Paris!C14": {"TIME_PERIOD": 2},
    "Inputs!D16": {"REF_AREA": "DE"},
}

MISMATCHED_KEY_SET_BINDINGS = {
    "Paris!B13": {"TIME_PERIOD": 1},
    "Inputs!C16": {"TIME_PERIOD": 1, "REF_AREA": "US"},
    "Paris!C13": {"TIME_PERIOD": 2},
    "Inputs!D16": {"REF_AREA": "DE"},
}

VARIABLE_COUNTRY_PAIR_BINDINGS = {
    "Inputs!B10": {"REF_AREA": "US", "TIME_PERIOD": 1},
    "Inputs!C10": {"REF_AREA": "CN", "TIME_PERIOD": 1},
    "Inputs!B11": {"REF_AREA": "DE", "TIME_PERIOD": 1},
    "Inputs!C11": {"REF_AREA": "FR", "TIME_PERIOD": 1},
    "Inputs!B12": {"REF_AREA": "JP", "TIME_PERIOD": 1},
    "Inputs!C12": {"REF_AREA": "KR", "TIME_PERIOD": 1},
}

ENGINE_REF_LAYOUT = ProjectionColumnLayout(
    engine_sheet="Engine",
    engine_columns=("C", "D", "E", "F"),
    outputs_sheet="Outputs",
    outputs_column_to_engine={"B": "C"},
    time_period_to_engine_column={1: "C", 2: "D", 3: "E", 4: "F"},
)


def test_structural_fingerprint_abstracts_cell_addresses_and_scalars() -> None:
    left = structural_fingerprint("=Paris!B13+1")
    right_address = structural_fingerprint("=Paris!B14+2")
    assert left is not None
    assert right_address is not None
    assert left[0] == right_address[0]
    assert left[1] != right_address[1]


def test_binding_aware_fingerprint_includes_sorted_key_concepts() -> None:
    fingerprint = structural_fingerprint(
        "=Paris!B13+Inputs!C16",
        bound_address_keys=COLUMN_SWEEP_BINDINGS,
    )
    assert fingerprint is not None
    skeleton, refs = fingerprint
    assert refs == ("Paris!B13", "Inputs!C16")
    assert skeleton == (
        "bin",
        "+",
        ("ref", 0, ("TIME_PERIOD",)),
        ("ref", 1, ("REF_AREA",)),
    )


def test_formulas_are_parameterizable_for_column_sweep() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C13+Inputs!D16"
    assert formulas_are_parameterizable(left, right)


def test_formulas_are_parameterizable_with_binding_keys_for_column_sweep() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C13+Inputs!D16"
    assert formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=COLUMN_SWEEP_BINDINGS,
    )


def test_formulas_are_parameterizable_with_binding_keys_for_both_axes() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C14+Inputs!D16"
    assert not formulas_are_parameterizable(left, right)
    assert formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=BOTH_AXES_BINDINGS,
    )


def test_formulas_are_not_parameterizable_for_different_structure() -> None:
    left = "=Paris!B13+1"
    right = "=Paris!B13*2"
    assert not formulas_are_parameterizable(left, right)


def test_formulas_are_not_parameterizable_when_refs_vary_on_both_axes() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C14+Inputs!D16"
    assert not formulas_are_parameterizable(left, right)


def test_formulas_are_not_parameterizable_when_ref_key_sets_differ() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C13+Inputs!D16"
    assert not formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=MISMATCHED_KEY_SET_BINDINGS,
    )


def test_formulas_are_not_parameterizable_when_binding_metadata_missing() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C13+Inputs!D16"
    assert not formulas_are_parameterizable(
        left,
        right,
        bound_address_keys={"Paris!B13": {"TIME_PERIOD": 1}},
    )


def test_formulas_are_not_parameterizable_for_anchor_vs_recurrence() -> None:
    left = "=Inputs!B6*(1+Inputs!C17/100)"
    right = "=Engine!C6*(1+Inputs!D17/100)"
    assert not formulas_are_parameterizable(left, right)


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


def test_cluster_graph_formulas_groups_trade_balance_with_binding_keys() -> None:
    clusters = cluster_graph_formulas(
        _trade_balance_graph(),
        bound_address_keys=TRADE_BALANCE_BINDINGS,
    )
    trade_clusters = [
        cluster for cluster in clusters if cluster.members[0].endswith("5")
    ]
    assert len(trade_clusters) == 1
    assert trade_clusters[0].members == ("Engine!B5", "Engine!C5", "Engine!D5")


def test_cluster_has_independent_operand_variation_detects_trade_balance_pattern() -> (
    None
):
    cluster = FormulaCluster(
        cluster_id=0,
        members=("Engine!B5", "Engine!C5", "Engine!D5"),
        canonical_template="=Inputs!B10-Inputs!C10",
        row=5,
    )
    formula_nodes = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!D5": "=Inputs!B12-Inputs!C12",
    }
    assert cluster_has_independent_operand_variation(
        cluster,
        formula_nodes,
        TRADE_BALANCE_BINDINGS,
        frozenset({"REF_AREA", "TIME_PERIOD"}),
    )


def test_dominant_key_only_variation_mode_splits_cluster() -> None:
    bindings = {
        "Inputs!B10": {"REF_AREA": "US", "TIME_PERIOD": 1},
        "Inputs!C10": {"REF_AREA": "CN", "TIME_PERIOD": 1},
        "Inputs!B11": {"REF_AREA": "US", "TIME_PERIOD": 2},
        "Inputs!C11": {"REF_AREA": "CN", "TIME_PERIOD": 2},
        "Inputs!B12": {"REF_AREA": "US", "TIME_PERIOD": 3},
        "Inputs!C12": {"REF_AREA": "DE", "TIME_PERIOD": 3},
    }
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!D5": "=Inputs!B12-Inputs!C12",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))

    independent_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=bindings,
        variation_mode="independent",
    )
    assert len(independent_clusters) == 1

    constrained_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=bindings,
        variation_mode="dominant_key_only",
    )
    assert len(constrained_clusters) == 2
    member_sets = {cluster.members for cluster in constrained_clusters}
    assert ("Engine!B5", "Engine!C5") in member_sets
    assert ("Engine!D5",) in member_sets


def test_cluster_graph_formulas_splits_anchor_from_recurrence_chain() -> None:
    clusters = cluster_graph_formulas(_debt_to_gdp_anchor_recurrence_graph())
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


def test_dominant_key_only_split_passes_layout_to_ref_key_resolution(
    synthetic_workbook_path: Path,
    monkeypatch,
) -> None:
    """dominant_key_only splitting must resolve operand keys with workbook layout."""
    import src.formula_clustering as formula_clustering

    bindings = {
        "Inputs!B10": {"REF_AREA": "US", "SCENARIO": "base"},
        "Inputs!C10": {"REF_AREA": "CN", "SCENARIO": "base"},
        "Inputs!B11": {"REF_AREA": "US", "SCENARIO": "base"},
        "Inputs!C11": {"REF_AREA": "CN", "SCENARIO": "base"},
        "Inputs!B12": {"REF_AREA": "US", "SCENARIO": "shock"},
        "Inputs!C12": {"REF_AREA": "CN", "SCENARIO": "shock"},
    }
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10-Engine!D5",
        "Engine!C5": "=Inputs!B11-Engine!E5",
        "Engine!D5": "=Inputs!B12-Engine!F5",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))

    captured_workbook_paths: list[Path | None] = []
    original = formula_clustering._ref_position_key_values

    def spy(
        member_address: str,
        formula: str,
        bound_address_keys,
        *,
        workbook_path: Path | None = None,
        layout: ProjectionColumnLayout | None = None,
        key_cache=None,
    ):
        captured_workbook_paths.append(workbook_path)
        return original(
            member_address,
            formula,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )

    monkeypatch.setattr(formula_clustering, "_ref_position_key_values", spy)

    cluster_graph_formulas(
        graph,
        bound_address_keys=bindings,
        variation_mode="dominant_key_only",
        workbook_path=synthetic_workbook_path,
        layout=ENGINE_REF_LAYOUT,
    )

    assert any(path == synthetic_workbook_path for path in captured_workbook_paths)


def test_cluster_graph_formulas_caches_resolved_binding_keys(
    synthetic_workbook_path: Path,
) -> None:
    """Binding key resolution should be cached, not repeated per pairwise comparison."""
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!D5": "=Inputs!B12-Inputs!C12",
        "Engine!E5": "=Inputs!B13-Inputs!C13",
        "Engine!F5": "=Inputs!B14-Inputs!C14",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))

    bound_address_keys = {
        **TRADE_BALANCE_BINDINGS,
        "Inputs!B13": {"REF_AREA": "US", "TIME_PERIOD": 4},
        "Inputs!C13": {"REF_AREA": "CN", "TIME_PERIOD": 4},
        "Inputs!B14": {"REF_AREA": "US", "TIME_PERIOD": 5},
        "Inputs!C14": {"REF_AREA": "CN", "TIME_PERIOD": 5},
    }
    formula_count = len(formulas)
    max_expected_resolutions = formula_count * 4

    with patch(
        "src.formula_clustering.expected_keys_for_address",
        wraps=expected_keys_for_address,
    ) as resolver:
        cluster_graph_formulas(
            graph,
            bound_address_keys=bound_address_keys,
            workbook_path=synthetic_workbook_path,
            layout=ENGINE_REF_LAYOUT,
        )

    assert resolver.call_count <= max_expected_resolutions


def test_cluster_has_independent_operand_variation_detects_variable_country_pairs() -> (
    None
):
    """Distinct country pairs per member still need operand-level parameterization."""
    cluster = FormulaCluster(
        cluster_id=0,
        members=("Engine!B5", "Engine!C5", "Engine!D5"),
        canonical_template="=Inputs!B10-Inputs!C10",
        row=5,
    )
    formula_nodes = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!D5": "=Inputs!B12-Inputs!C12",
    }
    assert cluster_has_independent_operand_variation(
        cluster,
        formula_nodes,
        VARIABLE_COUNTRY_PAIR_BINDINGS,
        frozenset({"REF_AREA"}),
    )
