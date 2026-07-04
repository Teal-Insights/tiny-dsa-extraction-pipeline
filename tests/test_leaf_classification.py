from __future__ import annotations

from typing import Annotated, Literal

import pytest
from excel_grapher.core.cell_types import RealBetween

from src.extraction_pipeline import classify_leaves_from_constraints


def test_classify_leaves_accepts_alternate_constraint_key_normalization(
    synthetic_graph,
) -> None:
    constraints = {
        "'Inputs'!a1": Annotated[float, RealBetween(0.0, 100.0)],
        "Inputs!B1": Literal[0],
    }

    classification = classify_leaves_from_constraints(
        constraints, synthetic_graph.leaf_keys()
    )

    assert classification == {
        "Inputs!A1": "input",
        "Inputs!B1": "constant",
    }


def test_classify_leaves_reports_missing_constraints_with_graph_leaf_keys(
    synthetic_graph,
) -> None:
    with pytest.raises(
        KeyError,
        match=r"missing constraints for leaf cells: \['Inputs!A1', 'Inputs!B1'\]",
    ):
        classify_leaves_from_constraints({}, synthetic_graph.leaf_keys())
