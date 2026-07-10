from __future__ import annotations

from typing import cast

import pytest

from src.semantic_naming import (
    BindingRecordHints,
    cluster_binding_naming_hints,
    collect_semantic_helper_names,
    semantic_helpers_available_for_calls,
    binding_record_hints_from_cell,
    validate_semantic_identifier,
)


def test_binding_record_hints_from_cell() -> None:
    hints = binding_record_hints_from_cell(
        {
            "key": {"TIME_PERIOD": 1},
            "record": {"OBS_VALUE": 1.0, "INDICATOR": "debt_ratio"},
        }
    )
    assert hints.binding_keys == {"TIME_PERIOD": 1}
    assert hints.binding_record == {"OBS_VALUE": 1.0, "INDICATOR": "debt_ratio"}


def test_cluster_binding_naming_hints_includes_member_records() -> None:
    payload = cluster_binding_naming_hints(
        (
            BindingRecordHints(
                binding_keys={"TIME_PERIOD": 1},
                binding_record={"INDICATOR": "shock_active"},
            ),
            BindingRecordHints(
                binding_keys={"TIME_PERIOD": 2},
                binding_record={"INDICATOR": "shock_active"},
            ),
        )
    )
    members = payload["members"]
    assert isinstance(members, list)
    first = cast(dict[str, object], members[0])
    second = cast(dict[str, object], members[1])
    assert first["binding_keys"] == {"TIME_PERIOD": 1}
    assert second["binding_keys"] == {"TIME_PERIOD": 2}


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
