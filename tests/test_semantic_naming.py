from __future__ import annotations

import pytest
from typing import cast

from src.semantic_naming import (
    SemanticLabelHints,
    cluster_naming_hints,
    collect_semantic_helper_names,
    semantic_helpers_available_for_calls,
    semantic_label_hints_from_metadata,
    validate_semantic_identifier,
)


def test_semantic_label_hints_from_metadata() -> None:
    hints = semantic_label_hints_from_metadata(
        {
            "table_labels": [{"label": "Country profile", "concept": "TABLE"}],
            "row_labels": [{"label": "Debt-to-GDP ratio", "concept": "INDICATOR"}],
            "column_labels": [{"label": "Borvelia", "concept": "COUNTRY"}],
        }
    )
    assert hints.table_labels == "Country profile"
    assert hints.row_labels == "Debt-to-GDP ratio"
    assert hints.column_labels == "Borvelia"


def test_cluster_naming_hints_includes_member_column_labels() -> None:
    payload = cluster_naming_hints(
        (
            SemanticLabelHints(
                table_labels="Engine",
                row_labels="Shock active",
                column_labels="Year 1",
            ),
            SemanticLabelHints(
                table_labels="Engine",
                row_labels="Shock active",
                column_labels="Year 2",
            ),
        )
    )
    assert payload["table_labels"] == "Engine"
    assert payload["row_labels"] == "Shock active"
    members = cast(list[dict[str, str]], payload["members"])
    assert members[0]["column_labels"] == "Year 1"
    assert members[1]["column_labels"] == "Year 2"


def test_validate_semantic_identifier_accepts_safe_unique_name() -> None:
    validate_semantic_identifier(
        "shock_active",
        existing_names=frozenset({"cell_engine_c10"}),
    )


def test_validate_semantic_identifier_rejects_cell_prefix() -> None:
    with pytest.raises(ValueError, match="cell_"):
        validate_semantic_identifier(
            "cell_engine_c10",
            existing_names=frozenset(),
        )


def test_validate_semantic_identifier_rejects_collision() -> None:
    with pytest.raises(ValueError, match="collides"):
        validate_semantic_identifier(
            "shock_active",
            existing_names=frozenset({"shock_active"}),
        )


def test_validate_semantic_identifier_allows_reapply() -> None:
    validate_semantic_identifier(
        "shock_active",
        existing_names=frozenset({"shock_active"}),
        allow_name="shock_active",
    )


def test_collect_semantic_helper_names() -> None:
    source = """
def shock_active(ctx, time_period: int):
    return 1.0

def initial_debt_to_gdp(ctx):
    return 0.0

def cell_engine_c10(ctx):
    return 0.0

def _resolve_formula(address):
    return None
"""
    names = collect_semantic_helper_names(source)
    assert names == frozenset({"shock_active", "initial_debt_to_gdp"})


def test_semantic_helpers_available_for_calls_includes_allocated_names() -> None:
    source = "def shock_active(ctx, time_period: int):\n    return 1.0\n"
    available = semantic_helpers_available_for_calls(
        source,
        frozenset({"shock_active", "primary_balance_shocked", "cell_engine_c10"}),
    )
    assert available == frozenset({"shock_active", "primary_balance_shocked"})


def test_collect_semantic_helper_names_requires_ctx_parameter() -> None:
    source = "def helper(x): return x\n"
    assert collect_semantic_helper_names(source) == frozenset()
