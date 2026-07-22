"""Checks for partitioning refactor clusters by public output series."""

from __future__ import annotations

from src.internals_refactor import _dispatch_entries_for_collapse
from tests.fixtures.cluster_refactor_golden import (
    GOLDEN_CLUSTER_REFACTOR_RESPONSES,
)


def test_output_cluster_collapse_emits_address_dispatch_with_time_period() -> None:
    """Non-engine output members become `_ADDRESS_DISPATCH` entries with keys."""
    response = GOLDEN_CLUSTER_REFACTOR_RESPONSES[14]
    dispatch = _dispatch_entries_for_collapse(response)
    assert dispatch == {
        "Outputs!B14": ("output_delta", {"time_period": 1}),
        "Outputs!C14": ("output_delta", {"time_period": 2}),
        "Outputs!D14": ("output_delta", {"time_period": 3}),
        "Outputs!E14": ("output_delta", {"time_period": 4}),
        "Outputs!F14": ("output_delta", {"time_period": 5}),
    }
