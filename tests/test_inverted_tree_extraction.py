"""RED-then-GREEN tests for the inverted-tree Tiny DSA prototype.

The prototype lives at ``dist/tiny_dsa_inverted`` and is the intended
mechanical shape of ``dist/tiny_dsa`` under graph inversion: each public
``compute_*`` function takes the leaf closure of its subgraph, internals
take first-level dependencies only, and series can be trimmed so unused
years do not expand the required argument set.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import TypedDict

import pytest

from dist.tiny_dsa_inverted import api, internals
from dist.tiny_dsa_inverted.data import (
    COUNTRY_INITIAL_DEBT_DEFAULT,
    COUNTRY_NAME_DEFAULT,
    COUNTRY_PROFILE_NAMES,
    ENGINE_YEAR_LABELS,
    GROWTH_BASELINE_DEFAULT,
    INTEREST_BASELINE_DEFAULT,
    PRIMARY_BALANCE_BASELINE_DEFAULT,
    SHOCK_MAGNITUDES_DEFAULT,
    SHOCK_TYPE_DEFAULT,
    SHOCK_YEAR_DEFAULT,
)
from dist.tiny_dsa_inverted.runtime import XlError, trim

# Workbook-cached values for the default Borvelia / growth-shock scenario.
_DEFAULT_BASELINE = (
    61.28985507246378,
    62.085941328852499,
    62.385873412566767,
    62.187254443545363,
    61.487675962596306,
)
_DEFAULT_SHOCKED = (
    61.28985507246378,
    63.299457414150091,
    64.858557350459208,
    65.956058763032104,
    66.580592230101857,
)
_DEFAULT_DELTA = tuple(
    shocked - baseline
    for shocked, baseline in zip(_DEFAULT_SHOCKED, _DEFAULT_BASELINE, strict=True)
)


class _BaselineKwargs(TypedDict):
    country_name: str
    country_initial_debt: tuple[float, ...]
    growth_baseline: tuple[float, ...]
    interest_baseline: tuple[float, ...]
    primary_balance_baseline: tuple[float, ...]


class _ShockKwargs(_BaselineKwargs):
    shock_year: int
    shock_type: int
    shock_magnitudes: tuple[float, ...]


_BASELINE_KWARGS: _BaselineKwargs = {
    "country_name": COUNTRY_NAME_DEFAULT,
    "country_initial_debt": COUNTRY_INITIAL_DEBT_DEFAULT,
    "growth_baseline": GROWTH_BASELINE_DEFAULT,
    "interest_baseline": INTEREST_BASELINE_DEFAULT,
    "primary_balance_baseline": PRIMARY_BALANCE_BASELINE_DEFAULT,
}
_SHOCK_KWARGS: _ShockKwargs = {
    **_BASELINE_KWARGS,
    "shock_year": SHOCK_YEAR_DEFAULT,
    "shock_type": SHOCK_TYPE_DEFAULT,
    "shock_magnitudes": SHOCK_MAGNITUDES_DEFAULT,
}


def _required_param_names(function: Callable[..., object]) -> tuple[str, ...]:
    names: list[str] = []
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.default is inspect.Parameter.empty:
            names.append(name)
    return tuple(names)


def _all_param_names(function: Callable[..., object]) -> tuple[str, ...]:
    return tuple(inspect.signature(function).parameters)


def test_compute_output_baseline_matches_workbook_defaults() -> None:
    values = api.compute_output_baseline(**_BASELINE_KWARGS)
    assert values == pytest.approx(_DEFAULT_BASELINE)


def test_compute_output_shocked_matches_workbook_defaults() -> None:
    values = api.compute_output_shocked(**_SHOCK_KWARGS)
    assert values == pytest.approx(_DEFAULT_SHOCKED)


def test_compute_output_delta_matches_workbook_defaults() -> None:
    values = api.compute_output_delta(**_SHOCK_KWARGS)
    assert values == pytest.approx(_DEFAULT_DELTA)


def test_output_delta_is_shocked_minus_baseline() -> None:
    baseline = api.compute_output_baseline(**_BASELINE_KWARGS)
    shocked = api.compute_output_shocked(**_SHOCK_KWARGS)
    delta = api.compute_output_delta(**_SHOCK_KWARGS)
    assert delta == pytest.approx(
        tuple(s - b for s, b in zip(shocked, baseline, strict=True))
    )


def test_baseline_leaf_closure_excludes_shock_inputs() -> None:
    required = _required_param_names(api.compute_output_baseline)
    all_names = _all_param_names(api.compute_output_baseline)
    assert required == (
        "country_name",
        "country_initial_debt",
        "growth_baseline",
        "interest_baseline",
        "primary_balance_baseline",
    )
    assert "shock_year" not in all_names
    assert "shock_type" not in all_names
    assert "shock_magnitudes" not in all_names
    assert "engine_year_labels" not in all_names
    assert "ctx" not in all_names
    assert all_names[-1] == "country_profile_names"


def test_shocked_leaf_closure_includes_shock_inputs_and_year_labels() -> None:
    required = _required_param_names(api.compute_output_shocked)
    all_names = _all_param_names(api.compute_output_shocked)
    assert required == (
        "country_name",
        "country_initial_debt",
        "growth_baseline",
        "interest_baseline",
        "primary_balance_baseline",
        "shock_year",
        "shock_type",
        "shock_magnitudes",
    )
    assert "engine_year_labels" in all_names
    assert "country_profile_names" in all_names
    assert "ctx" not in all_names


def test_delta_leaf_closure_is_union_of_baseline_and_shocked() -> None:
    assert _required_param_names(api.compute_output_delta) == _required_param_names(
        api.compute_output_shocked
    )
    assert set(_all_param_names(api.compute_output_delta)) == set(
        _all_param_names(api.compute_output_shocked)
    )


def test_public_compute_functions_have_no_setters_or_context() -> None:
    assert not hasattr(api, "make_context")
    assert not hasattr(api, "set_growth_baseline")
    assert not hasattr(api, "set_country_name")
    public_names = {name for name in api.__all__ if name.startswith("compute_")}
    assert public_names == {
        "compute_output_baseline",
        "compute_output_delta",
        "compute_output_shocked",
    }


def test_initial_debt_resolved_uses_index_match_first_level_deps() -> None:
    assert internals.initial_debt_resolved(
        country_name="Litellia",
        country_profile_names=COUNTRY_PROFILE_NAMES,
        country_initial_debt=COUNTRY_INITIAL_DEBT_DEFAULT,
    ) == pytest.approx(80.0)
    assert internals.initial_debt_resolved(
        country_name="Aurelium",
        country_profile_names=COUNTRY_PROFILE_NAMES,
        country_initial_debt=(60.0, 80.0, 40.0),
    ) == pytest.approx(40.0)


def test_shock_magnitude_resolved_offsets_into_the_shock_table() -> None:
    magnitudes = (-2.0, 2.0, -1.0)
    assert internals.shock_magnitude_resolved(1, magnitudes) == pytest.approx(-2.0)
    assert internals.shock_magnitude_resolved(2, magnitudes) == pytest.approx(2.0)
    assert internals.shock_magnitude_resolved(3, magnitudes) == pytest.approx(-1.0)


def test_shock_active_is_elementwise_on_year_labels() -> None:
    assert internals.shock_active(ENGINE_YEAR_LABELS, shock_year=2) == (0, 1, 1, 1, 1)
    assert internals.shock_active(trim(ENGINE_YEAR_LABELS, 3), shock_year=2) == (
        0,
        1,
        1,
    )


def test_internals_take_first_level_dependencies_only() -> None:
    assert _required_param_names(internals.shocked_growth) == (
        "growth_baseline",
        "shock_type",
        "shock_magnitude",
        "shock_active",
    )
    assert _required_param_names(internals.baseline_path_internal) == (
        "initial_debt",
        "growth_baseline",
        "interest_baseline",
        "primary_balance_baseline",
    )
    assert _required_param_names(internals.shocked_path_internal) == (
        "initial_debt",
        "shocked_growth",
        "shocked_interest",
        "shocked_primary_balance",
    )
    assert "ctx" not in _all_param_names(internals.shocked_path_internal)
    assert "shock_type" not in _all_param_names(internals.shocked_path_internal)
    assert "shock_magnitudes" not in _all_param_names(internals.shocked_path_internal)


def test_trim_keeps_recursive_path_from_expanding_leaf_arguments() -> None:
    initial_debt = internals.initial_debt_resolved(
        country_name=COUNTRY_NAME_DEFAULT,
        country_profile_names=COUNTRY_PROFILE_NAMES,
        country_initial_debt=COUNTRY_INITIAL_DEBT_DEFAULT,
    )
    two_year_path = internals.baseline_path_internal(
        initial_debt,
        trim(GROWTH_BASELINE_DEFAULT, 2),
        trim(INTEREST_BASELINE_DEFAULT, 2),
        trim(PRIMARY_BALANCE_BASELINE_DEFAULT, 2),
    )
    assert two_year_path == pytest.approx(_DEFAULT_BASELINE[:2])
    assert len(two_year_path) == 2


def test_misaligned_series_fail_fast() -> None:
    with pytest.raises(ValueError, match="misaligned"):
        internals.baseline_path_internal(
            60.0,
            GROWTH_BASELINE_DEFAULT,
            trim(INTEREST_BASELINE_DEFAULT, 3),
            PRIMARY_BALANCE_BASELINE_DEFAULT,
        )


def test_litellia_changes_baseline_without_shock_arguments() -> None:
    borvelia = api.compute_output_baseline(**_BASELINE_KWARGS)
    litellia_kwargs: _BaselineKwargs = {**_BASELINE_KWARGS, "country_name": "Litellia"}
    litellia = api.compute_output_baseline(**litellia_kwargs)
    assert litellia[0] != pytest.approx(borvelia[0])
    assert litellia[0] == pytest.approx(
        80.0 * (1.0 + 4.0 / 100.0) / (1.0 + 3.5 / 100.0) - (-1.0)
    )


def test_interest_shock_does_not_change_baseline() -> None:
    baseline = api.compute_output_baseline(**_BASELINE_KWARGS)
    interest_shock: _ShockKwargs = {**_SHOCK_KWARGS, "shock_type": 2}
    shocked = api.compute_output_shocked(**interest_shock)
    assert api.compute_output_baseline(**_BASELINE_KWARGS) == pytest.approx(baseline)
    assert shocked[0] == pytest.approx(baseline[0])
    assert shocked[1] != pytest.approx(baseline[1])


def test_records_wrapper_preserves_binding_keys() -> None:
    values = api.compute_output_baseline(**_BASELINE_KWARGS)
    records = api.as_records(
        values,
        scenario="baseline",
        unit_measure="PC_GDP",
    )
    assert [record["TIME_PERIOD"] for record in records] == [1, 2, 3, 4, 5]
    assert [record["OBS_VALUE"] for record in records] == pytest.approx(list(values))
    assert records[0]["SCENARIO"] == "baseline"


def test_unknown_country_raises_excel_error() -> None:
    with pytest.raises(XlError):
        internals.initial_debt_resolved(
            country_name="NotACountry",
            country_profile_names=COUNTRY_PROFILE_NAMES,
            country_initial_debt=COUNTRY_INITIAL_DEBT_DEFAULT,
        )


def test_trim_rejects_out_of_range_slices() -> None:
    with pytest.raises(ValueError):
        trim(GROWTH_BASELINE_DEFAULT, 6)


def test_guide_table_1_growth_shock_year_5() -> None:
    """Guide Table 1 reports Borvelia year-5 debt of 66.58 under the growth shock."""
    shocked = api.compute_output_shocked(**_SHOCK_KWARGS)
    assert shocked[-1] == pytest.approx(66.58, abs=0.005)


def test_orchestrator_trims_recursive_path_when_output_horizon_is_shorter() -> None:
    """Calling internals with a 3-year slice must not require years 4–5."""
    growth: Sequence[float] = (3.5, 3.5, 3.5)
    interest: Sequence[float] = (4.0, 4.0, 4.0)
    primary_balance: Sequence[float] = (-1.0, -0.5, 0.0)
    path = internals.baseline_path_internal(60.0, growth, interest, primary_balance)
    full = api.compute_output_baseline(**_BASELINE_KWARGS)
    assert path == pytest.approx(full[:3])
