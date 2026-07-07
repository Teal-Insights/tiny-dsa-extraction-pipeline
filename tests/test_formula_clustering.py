from src.formula_clustering import (
    cluster_graph_formulas,
    formulas_are_parameterizable,
    structural_fingerprint,
)


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


def test_cluster_graph_formulas_splits_anchor_from_recurrence_on_tiny_dsa(
    tiny_dsa_configured_pipeline,
) -> None:
    from src.subgraph_projection import build_refactor_projection

    projection = build_refactor_projection(tiny_dsa_configured_pipeline.graph)
    clusters = cluster_graph_formulas(projection, require_same_row=False)
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
