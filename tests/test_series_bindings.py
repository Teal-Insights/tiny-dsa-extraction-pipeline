from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from src.extraction_pipeline import (
    binding_validation_report,
    input_series,
    output_series,
    series_bindings,
)


def _series_addresses(series: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    addresses: dict[str, list[str]] = {}
    for item in series:
        cells = cast(Sequence[Mapping[str, Any]], item["cells"])
        addresses[str(item["id"])] = [str(cell["address"]) for cell in cells]
    return addresses


def test_series_bindings_validate_against_graph():
    assert binding_validation_report["ok"], binding_validation_report["issues"]


def test_binding_shards_merge_into_expected_series():
    expected_ids = {
        "country_name",
        "growth_baseline",
        "interest_baseline",
        "output_baseline",
        "output_delta",
        "output_shocked",
        "primary_balance_baseline",
        "shock_magnitudes",
        "shock_type",
        "shock_year",
    }

    assert series_bindings["schema_version"] == "1.2.0"
    assert {series["id"] for series in series_bindings["series"]} == expected_ids


def test_input_series_resolve_to_expected_cells():
    addresses = _series_addresses(input_series)

    assert addresses["country_name"] == ["Inputs!B5"]
    assert addresses["growth_baseline"] == [
        "Inputs!C16",
        "Inputs!D16",
        "Inputs!E16",
        "Inputs!F16",
        "Inputs!G16",
    ]
    assert addresses["interest_baseline"] == [
        "Inputs!C17",
        "Inputs!D17",
        "Inputs!E17",
        "Inputs!F17",
        "Inputs!G17",
    ]
    assert addresses["primary_balance_baseline"] == [
        "Inputs!C18",
        "Inputs!D18",
        "Inputs!E18",
        "Inputs!F18",
        "Inputs!G18",
    ]
    assert addresses["shock_year"] == ["Inputs!B21"]
    assert addresses["shock_type"] == ["Inputs!B22"]
    assert addresses["shock_magnitudes"] == ["Inputs!B26", "Inputs!C26", "Inputs!D26"]


def test_output_series_resolve_to_expected_cells():
    addresses = _series_addresses(output_series)

    assert addresses["output_baseline"] == [
        "Outputs!B12",
        "Outputs!C12",
        "Outputs!D12",
        "Outputs!E12",
        "Outputs!F12",
    ]
    assert addresses["output_shocked"] == [
        "Outputs!B13",
        "Outputs!C13",
        "Outputs!D13",
        "Outputs!E13",
        "Outputs!F13",
    ]
    assert addresses["output_delta"] == [
        "Outputs!B14",
        "Outputs!C14",
        "Outputs!D14",
        "Outputs!E14",
        "Outputs!F14",
    ]


def test_generated_records_api_computes_and_sets_values():
    from dist.api import compute_output_baseline, make_context, set_growth_baseline

    ctx = make_context()
    baseline = compute_output_baseline(ctx=ctx)

    assert len(baseline) == 5
    assert baseline[0]["TIME_PERIOD"] == 1
    assert baseline[0]["SCENARIO"] == "baseline"
    assert baseline[0]["OBS_VALUE"] == pytest.approx(61.28985507246378)

    set_growth_baseline(ctx, [{"TIME_PERIOD": 1, "OBS_VALUE": 2.5}])
    updated = compute_output_baseline(ctx=ctx)

    assert updated[0]["OBS_VALUE"] != baseline[0]["OBS_VALUE"]
