import pytest
from pathlib import Path
from unittest.mock import patch

from excel_grapher.core.formula_ast import parse
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node

from src.formula_clustering import (
    FormulaCluster,
    address_only_structural_fingerprint,
    cluster_graph_formulas,
    cluster_has_independent_operand_variation,
    format_structural_skeleton,
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
    """Graph where the period-1 anchor uses different cell refs than the recurrence chain.

    Under binding-aware fingerprints the formulas still share the same skeleton
    because every operand varies only on ``TIME_PERIOD``.
    """
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


def _debt_to_gdp_bindings() -> dict[str, dict[str, int]]:
    bindings: dict[str, dict[str, int]] = {}
    for col, time_period in zip("BCDEFG", range(1, 7), strict=True):
        bindings[f"Inputs!{col}6"] = {"TIME_PERIOD": time_period}
        bindings[f"Inputs!{col}16"] = {"TIME_PERIOD": time_period}
        bindings[f"Inputs!{col}17"] = {"TIME_PERIOD": time_period}
    for col, time_period in zip("CDEFG", range(2, 7), strict=True):
        bindings[f"Engine!{col}16"] = {"TIME_PERIOD": time_period}
        bindings[f"Engine!{col}20"] = {"TIME_PERIOD": time_period}
    bindings["Engine!C16"] = {"TIME_PERIOD": 1}
    bindings["Engine!C20"] = {"TIME_PERIOD": 1}
    return bindings


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

DOMINANT_KEY_SPLIT_BINDINGS = {
    **TRADE_BALANCE_BINDINGS,
    "Inputs!C12": {"REF_AREA": "DE", "TIME_PERIOD": 3},
}


def _dominant_key_split_graph() -> DependencyGraph:
    return _trade_balance_graph()


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


def test_structural_fingerprint_abstracts_cell_addresses_but_preserves_literals() -> (
    None
):
    left = address_only_structural_fingerprint("=Paris!B13+1")
    right_address = address_only_structural_fingerprint("=Paris!B14+2")
    assert left is not None
    assert right_address is not None
    assert left[0] != right_address[0]
    assert left[1] != right_address[1]
    assert left[0] == (
        "bin",
        "+",
        ("ref", 0),
        ("num", 1.0),
    )
    assert right_address[0] == (
        "bin",
        "+",
        ("ref", 0),
        ("num", 2.0),
    )


def test_format_structural_skeleton_renders_formula_like_placeholders() -> None:
    skeleton = (
        "bin",
        "+",
        ("ref", 0, ("TIME_PERIOD",)),
        ("ref", 1, ("REF_AREA",)),
    )
    assert format_structural_skeleton(skeleton) == "=ref_0[TIME_PERIOD]+ref_1[REF_AREA]"


def test_format_structural_skeleton_joins_sorted_dimension_ids() -> None:
    skeleton = (
        "bin",
        "-",
        ("ref", 0, ("REF_AREA", "TIME_PERIOD")),
        ("ref", 1, ("REF_AREA", "TIME_PERIOD")),
    )
    assert (
        format_structural_skeleton(skeleton)
        == "=ref_0[REF_AREA,TIME_PERIOD]-ref_1[REF_AREA,TIME_PERIOD]"
    )


def test_format_structural_skeleton_renders_scalar_literals_and_functions() -> None:
    skeleton = (
        "fn",
        "IF",
        (
            ("bin", ">", ("ref", 0, ("TIME_PERIOD",)), ("num", 0)),
            ("ref", 1, None),
            ("str", "baseline"),
        ),
    )
    assert (
        format_structural_skeleton(skeleton)
        == '=IF(ref_0[TIME_PERIOD]>0,ref_1,"baseline")'
    )


def test_format_structural_skeleton_renders_bool_and_quoted_strings() -> None:
    skeleton = (
        "fn",
        "IF",
        (("bool", True), ("str", 'say "hi"'), ("empty",)),
    )
    assert format_structural_skeleton(skeleton) == '=IF(TRUE,"say ""hi""",)'


def test_format_structural_skeleton_round_trips_parsed_literals() -> None:
    formula = '=IF(TRUE,"say ""hi""",)'
    fingerprint = address_only_structural_fingerprint(formula)
    assert fingerprint is not None
    skeleton, refs = fingerprint
    assert refs == ()
    assert format_structural_skeleton(skeleton) == formula


def test_format_structural_skeleton_rejects_wrong_scalar_types() -> None:
    with pytest.raises(TypeError, match="bool is not a number literal"):
        format_structural_skeleton(("num", True))
    with pytest.raises(TypeError, match="expected int or float"):
        format_structural_skeleton(("num", "12"))
    with pytest.raises(TypeError, match="expected str"):
        format_structural_skeleton(("str", 12))
    with pytest.raises(TypeError, match="expected bool literal"):
        format_structural_skeleton(("bool", "TRUE"))


def test_format_structural_skeleton_rejects_incomplete_scalar_nodes() -> None:
    with pytest.raises(TypeError, match="num skeleton node requires a value"):
        format_structural_skeleton(("num",))
    with pytest.raises(TypeError, match="str skeleton node requires a value"):
        format_structural_skeleton(("str",))
    with pytest.raises(TypeError, match="bool skeleton node requires a value"):
        format_structural_skeleton(("bool",))


def test_formulas_are_not_parameterizable_for_different_literal_values() -> None:
    left = "=Paris!B13+1"
    right = "=Paris!C13+1"
    bindings = {
        "Paris!B13": {"TIME_PERIOD": 1},
        "Paris!C13": {"TIME_PERIOD": 2},
    }
    assert formulas_are_parameterizable(left, right, bound_address_keys=bindings)

    assert not formulas_are_parameterizable(
        "=Paris!B13+1",
        "=Paris!B13+2",
        bound_address_keys={"Paris!B13": {"TIME_PERIOD": 1}},
    )
    assert not formulas_are_parameterizable(
        '="US"+Inputs!C16',
        '="DE"+Inputs!C16',
        bound_address_keys={
            "Inputs!C16": {"REF_AREA": "US"},
        },
    )
    assert not formulas_are_parameterizable(
        "=IF(TRUE,Paris!B13,Paris!C13)",
        "=IF(FALSE,Paris!B13,Paris!C13)",
        bound_address_keys={
            "Paris!B13": {"TIME_PERIOD": 1},
            "Paris!C13": {"TIME_PERIOD": 2},
        },
    )


def test_structural_fingerprint_requires_bound_address_keys() -> None:
    with pytest.raises(ValueError, match="bound_address_keys is required"):
        structural_fingerprint(
            "=Paris!B13+1",
            bound_address_keys=None,
        )


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


def test_binding_aware_fingerprint_uses_distinct_dimension_ids() -> None:
    bindings = {
        "Engine!C10": {"PROJECTION_PERIOD": 1, "REFERENCE_PERIOD": 0},
        "Engine!D10": {"PROJECTION_PERIOD": 2, "REFERENCE_PERIOD": 0},
    }
    fingerprint = structural_fingerprint(
        "=Engine!C10",
        bound_address_keys=bindings,
    )
    assert fingerprint is not None
    skeleton, refs = fingerprint
    assert refs == ("Engine!C10",)
    assert skeleton == (
        "ref",
        0,
        ("PROJECTION_PERIOD", "REFERENCE_PERIOD"),
    )
    assert formulas_are_parameterizable(
        "=Engine!C10",
        "=Engine!D10",
        bound_address_keys=bindings,
    )


def test_formulas_are_parameterizable_requires_bound_address_keys() -> None:
    with pytest.raises(ValueError, match="bound_address_keys is required"):
        formulas_are_parameterizable(
            "=Paris!B13+Inputs!C16",
            "=Paris!C13+Inputs!D16",
            bound_address_keys=None,
        )


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
    assert not formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=COLUMN_SWEEP_BINDINGS,
    )
    assert formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=BOTH_AXES_BINDINGS,
    )


def test_formulas_are_not_parameterizable_for_different_structure() -> None:
    left = "=Paris!B13+1"
    right = "=Paris!B13*2"
    assert not formulas_are_parameterizable(
        left,
        right,
        bound_address_keys={"Paris!B13": {"TIME_PERIOD": 1}},
    )


def test_formulas_are_not_parameterizable_when_refs_vary_on_both_axes() -> None:
    left = "=Paris!B13+Inputs!C16"
    right = "=Paris!C14+Inputs!D16"
    assert not formulas_are_parameterizable(
        left,
        right,
        bound_address_keys=COLUMN_SWEEP_BINDINGS,
    )


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


def test_formulas_are_parameterizable_for_matching_anchor_and_recurrence_shapes() -> (
    None
):
    left = "=Inputs!B6*(1+Inputs!C17/100)"
    right = "=Engine!C6*(1+Inputs!D17/100)"
    bindings = {
        "Inputs!B6": {"TIME_PERIOD": 1},
        "Inputs!C17": {"TIME_PERIOD": 1},
        "Engine!C6": {"TIME_PERIOD": 1},
        "Inputs!D17": {"TIME_PERIOD": 2},
    }
    assert formulas_are_parameterizable(left, right, bound_address_keys=bindings)


def test_cluster_graph_formulas_requires_bound_address_keys(
    synthetic_projection,
) -> None:
    with pytest.raises(ValueError, match="bound_address_keys is required"):
        cluster_graph_formulas(synthetic_projection, bound_address_keys=None)


def test_cluster_graph_formulas_groups_parallel_row_on_synthetic_projection(
    synthetic_projection,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture,
) -> None:
    clusters = cluster_graph_formulas(
        synthetic_projection,
        bound_address_keys=synthetic_bound_address_keys,
        clustering_mode="ast",
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
        layout=synthetic_pipeline_config_fixture.projection_layout,
    )
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
        clustering_mode="ast",
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


def test_dominant_key_only_split_resolves_each_member_once() -> None:
    """dominant_key_only splitting should materialize each member's ref keys once."""
    import src.formula_clustering as formula_clustering

    graph = _dominant_key_split_graph()
    call_counts: dict[str, int] = {}
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
        call_counts[member_address] = call_counts.get(member_address, 0) + 1
        return original(
            member_address,
            formula,
            bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
            key_cache=key_cache,
        )

    with patch.object(formula_clustering, "_ref_position_key_values", spy):
        cluster_graph_formulas(
            graph,
            bound_address_keys=DOMINANT_KEY_SPLIT_BINDINGS,
            clustering_mode="ast",
            variation_mode="dominant_key_only",
        )

    assert call_counts == {
        "Engine!B5": 1,
        "Engine!C5": 1,
        "Engine!D5": 1,
    }


def test_dominant_key_only_variation_mode_splits_cluster() -> None:
    graph = _dominant_key_split_graph()

    independent_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=DOMINANT_KEY_SPLIT_BINDINGS,
        clustering_mode="ast",
        variation_mode="independent",
    )
    assert len(independent_clusters) == 1

    constrained_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=DOMINANT_KEY_SPLIT_BINDINGS,
        clustering_mode="ast",
        variation_mode="dominant_key_only",
    )
    assert len(constrained_clusters) == 2
    member_sets = {cluster.members for cluster in constrained_clusters}
    assert ("Engine!B5", "Engine!C5") in member_sets
    assert ("Engine!D5",) in member_sets


def test_cluster_graph_formulas_groups_debt_recurrence_chain_with_binding_keys() -> (
    None
):
    clusters = cluster_graph_formulas(
        _debt_to_gdp_anchor_recurrence_graph(),
        bound_address_keys=_debt_to_gdp_bindings(),
        clustering_mode="ast",
    )
    debt_clusters = [
        cluster
        for cluster in clusters
        if any(member.endswith("20") for member in cluster.members)
    ]
    assert len(debt_clusters) == 1
    assert set(debt_clusters[0].members) == {
        "Engine!C20",
        "Engine!D20",
        "Engine!E20",
        "Engine!F20",
        "Engine!G20",
    }


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
        clustering_mode="ast",
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
            clustering_mode="ast",
            workbook_path=synthetic_workbook_path,
            layout=ENGINE_REF_LAYOUT,
        )

    assert resolver.call_count <= max_expected_resolutions


def test_cluster_graph_formulas_parses_each_formula_once(
    synthetic_workbook_path: Path,
) -> None:
    """Clustering should bucket by per-formula fingerprint, not re-parse pairwise."""
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

    with patch("src.formula_clustering.parse", wraps=parse) as parser:
        cluster_graph_formulas(
            graph,
            bound_address_keys=bound_address_keys,
            clustering_mode="ast",
            workbook_path=synthetic_workbook_path,
            layout=ENGINE_REF_LAYOUT,
        )

    assert parser.call_count <= len(formulas)


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


def _shared_ast_multi_series_graph() -> DependencyGraph:
    """Two internal series whose members share one AST family on TIME_PERIOD only."""
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10+1",
        "Engine!C5": "=Inputs!B11+1",
        "Engine!B6": "=Inputs!B10+1",
        "Engine!C6": "=Inputs!B11+1",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))
    return graph


SHARED_AST_MULTI_SERIES_BINDINGS = {
    "Inputs!B10": {"TIME_PERIOD": 1},
    "Inputs!B11": {"TIME_PERIOD": 2},
}

SHARED_AST_ADDRESS_TO_SERIES_ID = {
    "Engine!B5": "revenue_growth",
    "Engine!C5": "revenue_growth",
    "Engine!B6": "expenditure_growth",
    "Engine!C6": "expenditure_growth",
}


def test_series_mode_skips_formula_fingerprinting() -> None:
    graph = _shared_ast_multi_series_graph()
    with patch("src.formula_clustering.parse") as parser:
        cluster_graph_formulas(
            graph,
            bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
            clustering_mode="series",
            address_to_series_id=SHARED_AST_ADDRESS_TO_SERIES_ID,
        )
    parser.assert_not_called()


def test_series_ast_partitions_shared_ast_cluster_by_series() -> None:
    graph = _shared_ast_multi_series_graph()

    ast_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
        clustering_mode="ast",
        address_to_series_id=SHARED_AST_ADDRESS_TO_SERIES_ID,
    )
    assert len(ast_clusters) == 1
    assert set(ast_clusters[0].members) == {
        "Engine!B5",
        "Engine!C5",
        "Engine!B6",
        "Engine!C6",
    }

    series_ast_clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
        clustering_mode="series_ast",
        address_to_series_id=SHARED_AST_ADDRESS_TO_SERIES_ID,
    )
    assert len(series_ast_clusters) == 2
    member_sets = {cluster.members for cluster in series_ast_clusters}
    assert member_sets == {
        ("Engine!B5", "Engine!C5"),
        ("Engine!B6", "Engine!C6"),
    }


def test_series_mode_clusters_one_unit_per_internal_series() -> None:
    graph = _shared_ast_multi_series_graph()
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
        clustering_mode="series",
        address_to_series_id=SHARED_AST_ADDRESS_TO_SERIES_ID,
    )
    assert len(clusters) == 2
    member_sets = {cluster.members for cluster in clusters}
    assert member_sets == {
        ("Engine!B5", "Engine!C5"),
        ("Engine!B6", "Engine!C6"),
    }


def test_series_ast_applies_variation_mode_within_each_series() -> None:
    graph = DependencyGraph()
    formulas = {
        "Engine!B5": "=Inputs!B10-Inputs!C10",
        "Engine!C5": "=Inputs!B11-Inputs!C11",
        "Engine!B6": "=Inputs!B10-Inputs!C10",
        "Engine!C6": "=Inputs!B11-Inputs!C11",
        "Engine!D6": "=Inputs!B12-Inputs!C12",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))

    bindings = {
        **TRADE_BALANCE_BINDINGS,
        "Inputs!C12": {"REF_AREA": "DE", "TIME_PERIOD": 3},
    }
    address_to_series_id = {
        "Engine!B5": "series_a",
        "Engine!C5": "series_a",
        "Engine!B6": "series_b",
        "Engine!C6": "series_b",
        "Engine!D6": "series_b",
    }

    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=bindings,
        clustering_mode="series_ast",
        variation_mode="dominant_key_only",
        address_to_series_id=address_to_series_id,
    )
    member_sets = {cluster.members for cluster in clusters}
    assert ("Engine!B5", "Engine!C5") in member_sets
    assert ("Engine!B6", "Engine!C6") in member_sets
    assert ("Engine!D6",) in member_sets


def test_series_ast_defaults_without_explicit_mode() -> None:
    graph = _shared_ast_multi_series_graph()
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
        address_to_series_id=SHARED_AST_ADDRESS_TO_SERIES_ID,
    )
    assert len(clusters) == 2


def test_series_aware_clustering_requires_address_to_series_id() -> None:
    graph = _shared_ast_multi_series_graph()
    with pytest.raises(ValueError, match="address_to_series_id"):
        cluster_graph_formulas(
            graph,
            bound_address_keys=SHARED_AST_MULTI_SERIES_BINDINGS,
            clustering_mode="series_ast",
        )


def _output_time_sweep_graph() -> DependencyGraph:
    """Public output time-sweep whose members share one AST family."""
    graph = DependencyGraph()
    formulas = {
        "Hot!D11": "=Baseline!D5+1",
        "Hot!E11": "=Baseline!E5+1",
        "Hot!F11": "=Baseline!F5+1",
        "Paris!D11": "=Baseline!D5+1",
        "Paris!E11": "=Baseline!E5+1",
    }
    for address, formula in formulas.items():
        sheet, column, row = parse_workbook_address(address)
        graph.add_node(_formula_node(sheet, column, row, formula))
    return graph


OUTPUT_TIME_SWEEP_BINDINGS = {
    "Hot!D11": {"TIME_PERIOD": 2010},
    "Hot!E11": {"TIME_PERIOD": 2011},
    "Hot!F11": {"TIME_PERIOD": 2012},
    "Paris!D11": {"TIME_PERIOD": 2010},
    "Paris!E11": {"TIME_PERIOD": 2011},
    "Baseline!D5": {"TIME_PERIOD": 2010},
    "Baseline!E5": {"TIME_PERIOD": 2011},
    "Baseline!F5": {"TIME_PERIOD": 2012},
}

OUTPUT_TIME_SWEEP_SERIES_IDS = {
    "Hot!D11": "scenario_gdp_growth_hot",
    "Hot!E11": "scenario_gdp_growth_hot",
    "Hot!F11": "scenario_gdp_growth_hot",
    "Paris!D11": "scenario_gdp_growth_paris",
    "Paris!E11": "scenario_gdp_growth_paris",
}


def test_series_ast_keeps_output_series_time_sweep_as_multi_member_cluster() -> None:
    """Output-bound cells without internal owners stay clustered by public series."""
    graph = _output_time_sweep_graph()

    without_public = cluster_graph_formulas(
        graph,
        bound_address_keys=OUTPUT_TIME_SWEEP_BINDINGS,
        clustering_mode="series_ast",
        address_to_series_id={},
    )
    assert {cluster.members for cluster in without_public} == {
        ("Hot!D11",),
        ("Hot!E11",),
        ("Hot!F11",),
        ("Paris!D11",),
        ("Paris!E11",),
    }

    with_public = cluster_graph_formulas(
        graph,
        bound_address_keys=OUTPUT_TIME_SWEEP_BINDINGS,
        clustering_mode="series_ast",
        address_to_series_id=OUTPUT_TIME_SWEEP_SERIES_IDS,
    )
    member_sets = {cluster.members for cluster in with_public}
    assert ("Hot!D11", "Hot!E11", "Hot!F11") in member_sets
    assert ("Paris!D11", "Paris!E11") in member_sets


def test_series_ast_does_not_merge_distinct_output_series_sharing_ast() -> None:
    """Scenario shards keep separate partition ids even when compute names match."""
    graph = _output_time_sweep_graph()
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=OUTPUT_TIME_SWEEP_BINDINGS,
        clustering_mode="series_ast",
        address_to_series_id=OUTPUT_TIME_SWEEP_SERIES_IDS,
    )
    assert len(clusters) == 2
    assert {cluster.members for cluster in clusters} == {
        ("Hot!D11", "Hot!E11", "Hot!F11"),
        ("Paris!D11", "Paris!E11"),
    }


def test_series_mode_clusters_output_series_without_internal_owner() -> None:
    graph = _output_time_sweep_graph()
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=OUTPUT_TIME_SWEEP_BINDINGS,
        clustering_mode="series",
        address_to_series_id=OUTPUT_TIME_SWEEP_SERIES_IDS,
    )
    assert {cluster.members for cluster in clusters} == {
        ("Hot!D11", "Hot!E11", "Hot!F11"),
        ("Paris!D11", "Paris!E11"),
    }
