from src.formula_clustering import cluster_graph_formulas, levenshtein_ratio


def test_levenshtein_ratio_identical_strings() -> None:
    assert levenshtein_ratio("abc", "abc") == 0.0


def test_levenshtein_ratio_completely_different_lengths() -> None:
    assert levenshtein_ratio("", "abc") == 1.0


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
