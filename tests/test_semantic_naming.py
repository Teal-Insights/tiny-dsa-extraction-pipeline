from __future__ import annotations

from typing import cast

import pytest

from src.semantic_naming import (
    BindingRecordHints,
    allocate_schedule_helper_names,
    binding_record_hints_from_cell,
    cluster_binding_naming_hints,
    collect_semantic_helper_names,
    semantic_helpers_available_for_calls,
    sole_series_id_for_addresses,
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


def test_sole_series_id_for_addresses_requires_unique_mapping() -> None:
    assert (
        sole_series_id_for_addresses(
            ("Sheet!A1", "Sheet!B1"),
            {"Sheet!A1": "growth_threshold_met", "Sheet!B1": "growth_threshold_met"},
        )
        == "growth_threshold_met"
    )


def test_sole_series_id_for_addresses_rejects_missing_and_mixed() -> None:
    with pytest.raises(ValueError, match="missing series_id"):
        sole_series_id_for_addresses(
            ("Sheet!A1", "Sheet!B1"),
            {"Sheet!A1": "growth_threshold_met"},
        )
    with pytest.raises(ValueError, match="exactly one series_id"):
        sole_series_id_for_addresses(
            ("Sheet!A1", "Sheet!B1"),
            {"Sheet!A1": "series_a", "Sheet!B1": "series_b"},
        )


def test_allocate_schedule_helper_names_keeps_bare_series_id_for_sole_unit() -> None:
    names = allocate_schedule_helper_names(
        (("Engine!C20", "Engine!D20"),),
        {"Engine!C20": "shocked_path_internal", "Engine!D20": "shocked_path_internal"},
    )
    assert names == ("shocked_path_internal",)


def test_allocate_schedule_helper_names_uniquifies_peeled_series_units() -> None:
    """Multiple schedule units from one series must not share a locked helper name."""
    names = allocate_schedule_helper_names(
        (
            ("Engine!C20",),
            ("Engine!D20", "Engine!E20"),
        ),
        {
            "Engine!C20": "shocked_path_internal",
            "Engine!D20": "shocked_path_internal",
            "Engine!E20": "shocked_path_internal",
        },
    )
    assert names == ("shocked_path_internal", "shocked_path_internal_2")
    assert len(set(names)) == len(names)


def test_allocate_schedule_helper_names_avoids_existing_and_earlier_unit_names() -> (
    None
):
    """Collisions with internals and earlier schedule units are resolved before LLM."""
    names = allocate_schedule_helper_names(
        (
            ("Engine!C20",),
            ("Engine!D20",),
            ("Engine!E20",),
        ),
        {
            "Engine!C20": "shocked_path_internal",
            "Engine!D20": "shocked_path_internal",
            "Engine!E20": "other_series",
        },
        existing_names=frozenset({"shocked_path_internal", "other_series"}),
    )
    assert names[0] == "shocked_path_internal_2"
    assert names[1] == "shocked_path_internal_3"
    # Sole units also treat existing_names as blocked (no overwrite).
    assert names[2] == "other_series_2"
    assert len(set(names)) == len(names)


def test_allocate_schedule_helper_names_blocks_existing_for_sole_units() -> None:
    """A sole unit for a series does not reuse a name already present in internals."""
    names = allocate_schedule_helper_names(
        (("Engine!C20", "Engine!D20"),),
        {"Engine!C20": "shocked_path_internal", "Engine!D20": "shocked_path_internal"},
        existing_names=frozenset({"shocked_path_internal"}),
    )
    assert names == ("shocked_path_internal_2",)


def test_allocate_schedule_helper_names_detects_unresolvable_shape_collision() -> None:
    with pytest.raises(ValueError, match="snake_case|collides|identifier"):
        allocate_schedule_helper_names(
            (("Engine!C20",),),
            {"Engine!C20": "NotASnake"},
        )
