"""Checks for partitioning refactor clusters by public output series."""

from __future__ import annotations

from src.internals_refactor import _dispatch_entries_for_collapse
from tests.fixtures.cluster_refactor_golden import (
    GOLDEN_CLUSTER_REFACTOR_RESPONSES,
)


def test_output_cluster_collapse_emits_address_dispatch_with_time_period() -> None:
    """Collapsed output members become `_ADDRESS_DISPATCH` entries with keys."""
    response = GOLDEN_CLUSTER_REFACTOR_RESPONSES[14]
    dispatch = _dispatch_entries_for_collapse(response)
    assert dispatch == {
        "Outputs!B14": ("output_delta", {"time_period": 1}),
        "Outputs!C14": ("output_delta", {"time_period": 2}),
        "Outputs!D14": ("output_delta", {"time_period": 3}),
        "Outputs!E14": ("output_delta", {"time_period": 4}),
        "Outputs!F14": ("output_delta", {"time_period": 5}),
    }


def test_engine_cluster_collapse_also_emits_address_dispatch() -> None:
    """Engine members are dispatched too: no sheet name is privileged."""
    response = GOLDEN_CLUSTER_REFACTOR_RESPONSES[10]
    dispatch = _dispatch_entries_for_collapse(response)
    assert dispatch == {
        "Engine!C10": ("shock_active", {"time_period": 1}),
        "Engine!D10": ("shock_active", {"time_period": 2}),
        "Engine!E10": ("shock_active", {"time_period": 3}),
        "Engine!F10": ("shock_active", {"time_period": 4}),
        "Engine!G10": ("shock_active", {"time_period": 5}),
    }
