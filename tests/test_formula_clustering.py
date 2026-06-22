from src.extraction_pipeline import graph
from src.formula_clustering import (
    canonicalize_formula_for_clustering,
    cluster_graph_formulas,
    levenshtein_ratio,
)
from src.subgraph_projection import build_tiny_dsa_refactor_projection


def test_levenshtein_ratio_identical_strings() -> None:
    assert levenshtein_ratio("abc", "abc") == 0.0


def test_canonicalize_unifies_first_year_and_chain_columns() -> None:
    projection = build_tiny_dsa_refactor_projection(graph)

    c6 = projection.get_node("Engine!C6")
    d6 = projection.get_node("Engine!D6")
    assert c6 is not None and c6.normalized_formula is not None
    assert d6 is not None and d6.normalized_formula is not None

    assert canonicalize_formula_for_clustering(
        c6.normalized_formula, "Engine!C6"
    ) == canonicalize_formula_for_clustering(d6.normalized_formula, "Engine!D6")


def test_cluster_graph_formulas_finds_parallel_families_on_tiny_dsa() -> None:
    projection = build_tiny_dsa_refactor_projection(graph)
    clusters = cluster_graph_formulas(projection)

    parallel_clusters = [cluster for cluster in clusters if len(cluster.members) == 5]
    assert len(parallel_clusters) == 5

    rows = {cluster.row for cluster in parallel_clusters}
    assert rows == {6, 10, 16, 20, 14}

    shock_cluster = next(cluster for cluster in clusters if cluster.row == 10)
    assert shock_cluster.members == (
        "Engine!C10",
        "Engine!D10",
        "Engine!E10",
        "Engine!F10",
        "Engine!G10",
    )
    assert shock_cluster.canonical_template == "=IF(Engine!{COL}5>=Inputs!B21,1,0)"

    debt_cluster = next(cluster for cluster in clusters if cluster.row == 20)
    assert "Engine!C20" in debt_cluster.members
    assert "{PRIOR_DEBT}" in debt_cluster.canonical_template

    singleton_addresses = {
        member
        for cluster in clusters
        if len(cluster.members) == 1
        for member in cluster.members
    }
    assert singleton_addresses == {"Engine!B9", "Inputs!B6"}


def test_cluster_graph_formulas_keeps_different_rows_separate() -> None:
    projection = build_tiny_dsa_refactor_projection(graph)
    clusters = cluster_graph_formulas(projection, similarity_threshold=1.0)

    shock_cluster = next(
        cluster for cluster in clusters if "Engine!C10" in cluster.members
    )
    assert "Engine!C16" not in shock_cluster.members
