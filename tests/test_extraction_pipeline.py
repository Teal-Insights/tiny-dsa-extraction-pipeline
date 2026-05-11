from src.extraction_pipeline import constraints, graph


# Test that all leaf cells are constrained
def test_all_leaf_cells_are_constrained():
    assert all([key in constraints.keys() for key in graph.leaf_keys()])
