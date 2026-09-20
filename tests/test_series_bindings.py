from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from src.dependency_graph_viz import series_cell_keys


def _series_addresses(series: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    addresses: dict[str, list[str]] = {}
    for item in series:
        cells = cast(Sequence[Mapping[str, Any]], item["cells"])
        addresses[str(item["id"])] = [str(cell["address"]) for cell in cells]
    return addresses


def test_series_bindings_validate_against_graph(tiny_dsa_configured_pipeline):
    report = tiny_dsa_configured_pipeline.binding_validation_report
    assert report["ok"], report["issues"]


def test_binding_shards_merge_into_expected_series(tiny_dsa_configured_pipeline):
    series_bindings = tiny_dsa_configured_pipeline.series_bindings
    expected_public_ids = {
        "country_name",
        "country_initial_debt",
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
    expected_internal_ids = {
        "initial_debt_resolved",
        "engine_initial_debt_baseline",
        "engine_initial_debt_shocked",
        "shock_magnitude_resolved",
        "shock_active",
        "shocked_growth",
        "shocked_interest",
        "shocked_primary_balance",
        "baseline_path_internal",
        "shocked_path_internal",
    }
    expected_constant_ids = {
        "country_profile_names",
        "engine_year_labels",
    }

    assert series_bindings["schema_version"] == "1.17.0"
    series_ids = {series["id"] for series in series_bindings["series"]}
    assert series_ids == (
        expected_public_ids | expected_internal_ids | expected_constant_ids
    )


def test_input_series_resolve_to_expected_cells(tiny_dsa_configured_pipeline):
    addresses = _series_addresses(tiny_dsa_configured_pipeline.input_series)

    assert addresses["country_name"] == ["Inputs!B5"]
    assert addresses["country_initial_debt"] == [
        "Inputs!B10",
        "Inputs!B11",
        "Inputs!B12",
    ]
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


def test_output_series_resolve_to_expected_cells(tiny_dsa_configured_pipeline):
    addresses = _series_addresses(tiny_dsa_configured_pipeline.output_series)

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


def test_generated_keyword_api_computes_and_overrides_values():
    from dist.tiny_dsa import data
    from dist.tiny_dsa.api import compute_output_baseline

    baseline = compute_output_baseline(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
    )

    assert len(baseline.domain.axes[0].keys) == 5
    assert baseline[1] == pytest.approx(61.28985507246378)

    growth = data.GROWTH_BASELINE_DEFAULT
    slower_growth = growth.with_values(
        (2.5, *(growth[key] for key in growth.domain.axes[0].keys[1:]))
    )
    updated = compute_output_baseline(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=slower_growth,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
    )
    assert updated[1] != baseline[1]

    debt = data.COUNTRY_INITIAL_DEBT_DEFAULT
    higher_borvelia_debt = debt.with_values(
        (70.0, *(debt[key] for key in debt.domain.axes[0].keys[1:]))
    )
    updated_initial_debt = compute_output_baseline(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=higher_borvelia_debt,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
    )
    assert updated_initial_debt[1] != updated[1]


def test_constant_series_resolve_to_expected_cells(tiny_dsa_configured_pipeline):
    addresses = _series_addresses(tiny_dsa_configured_pipeline.constant_series)

    assert addresses["country_profile_names"] == [
        "Inputs!A10",
        "Inputs!A11",
        "Inputs!A12",
    ]
    assert addresses["engine_year_labels"] == [
        "Engine!C5",
        "Engine!D5",
        "Engine!E5",
        "Engine!F5",
        "Engine!G5",
    ]


def test_every_constant_leaf_has_constant_series_binding(
    tiny_dsa_configured_pipeline,
):
    pipeline = tiny_dsa_configured_pipeline
    constant_leaves = {
        key for key, kind in pipeline.leaf_classification.items() if kind == "constant"
    }
    bound_constant_cells = series_cell_keys(pipeline.constant_series)
    unbound = sorted(constant_leaves - bound_constant_cells)

    assert unbound == [], (
        "Constant leaves missing from constants.bindings.yaml: " + ", ".join(unbound)
    )


def test_every_mutable_input_leaf_has_input_series_binding(
    tiny_dsa_configured_pipeline,
):
    pipeline = tiny_dsa_configured_pipeline
    mutable_input_leaves = {
        key for key, kind in pipeline.leaf_classification.items() if kind == "input"
    }
    bound_input_cells = series_cell_keys(pipeline.input_series)
    unbound = sorted(mutable_input_leaves - bound_input_cells)

    assert unbound == [], (
        "Mutable input leaves missing from inputs.bindings.yaml: " + ", ".join(unbound)
    )
