"""Output orchestrators for the inverted graph.

Each `compute_*` function takes the leaf closure of its subgraph.
There is no evaluation context and no input setters.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import internals
from .data import COUNTRY_PROFILE_NAMES, ENGINE_YEAR_LABELS
from .runtime import require_length


def compute_output_baseline(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
) -> tuple[float | str, ...]:
    """Compute the baseline debt-to-GDP path for projection years 1 through 5.

    Produce the stable baseline output series for projection years 1 through 5 from the resolved country and baseline inputs.

    Args:
        country_name: Name of the selected country profile. Must be one of the country profile names in `country_profile_names`.
        country_initial_debt: Initial debt-to-GDP ratios in percent of GDP for each country profile, aligned positionally with `country_profile_names`. Must contain exactly 3 entries.
        growth_baseline: Real GDP growth rates for projection years 1 through 5, in percent per annum. Must contain exactly 5 entries.
        interest_baseline: Real interest rates paid on outstanding general-government debt for projection years 1 through 5, in percent per annum. Must contain exactly 5 entries.
        primary_balance_baseline: Primary fiscal balances for projection years 1 through 5, in percent of GDP; positive values denote a surplus. Must contain exactly 5 entries.
        country_profile_names: Country profile names accepted by `country_name`, aligned positionally with `country_initial_debt`. Must contain exactly 3 entries.

    Returns:
        Tuple of five end-of-period general-government debt-to-GDP ratios in percent of GDP, corresponding to projection years 1 through 5.
    """
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    require_length(country_profile_names, 3)
    initial_debt_resolved = internals.initial_debt_resolved(country_profile_names, country_name, country_initial_debt)
    engine_initial_debt_baseline = internals.engine_initial_debt_baseline(initial_debt_resolved)
    baseline_path_internal = internals.baseline_path_internal(engine_initial_debt_baseline, growth_baseline, interest_baseline, primary_balance_baseline)
    output_baseline = internals.output_baseline(baseline_path_internal)
    return tuple(output_baseline)

def _run_0(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
    engine_year_labels: Sequence[int] = ENGINE_YEAR_LABELS,
) -> tuple[tuple[float | str, ...], tuple[float | str, ...]]:
    """Evaluate the shared formula closure of `output_shocked`, `output_delta`."""
    require_length(country_profile_names, 3)
    require_length(engine_year_labels, 5)
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    require_length(shock_magnitudes, 3)
    initial_debt_resolved = internals.initial_debt_resolved(country_profile_names, country_name, country_initial_debt)
    shock_magnitude_resolved = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    shock_active = internals.shock_active(engine_year_labels, shock_year)
    engine_initial_debt_shocked = internals.engine_initial_debt_shocked(initial_debt_resolved)
    shocked_growth = internals.shocked_growth(growth_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_interest = internals.shocked_interest(interest_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_primary_balance = internals.shocked_primary_balance(primary_balance_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_path_internal = internals.shocked_path_internal(engine_initial_debt_shocked, shocked_growth, shocked_interest, shocked_primary_balance)
    output_shocked = internals.output_shocked(shocked_path_internal)
    engine_initial_debt_baseline = internals.engine_initial_debt_baseline(initial_debt_resolved)
    baseline_path_internal = internals.baseline_path_internal(engine_initial_debt_baseline, growth_baseline, interest_baseline, primary_balance_baseline)
    output_baseline = internals.output_baseline(baseline_path_internal)
    output_delta = internals.output_delta(output_baseline, output_shocked)
    return output_shocked, output_delta

def compute_output_shocked(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
    engine_year_labels: Sequence[int] = ENGINE_YEAR_LABELS,
) -> tuple[float | str, ...]:
    """Compute the shocked debt-to-GDP path for projection years 1 through 5.

    Return the shocked debt trajectory implied by the configured baseline and shock for comparison against the baseline path.

    Args:
        country_name: Name of the selected country; must match an entry in country_profile_names.
        country_initial_debt: Initial debt-to-GDP ratio (end of year 0) for the selected country, expressed as a percentage of GDP and used to seed the debt-dynamics recursion.
        growth_baseline: Baseline real GDP growth rates for projection years 1 through 5, in percent per annum.
        interest_baseline: Baseline effective real interest rates on public debt for projection years 1 through 5, in percent per annum.
        primary_balance_baseline: Baseline primary fiscal balance for projection years 1 through 5, expressed as a percentage of GDP; positive values denote a surplus.
        shock_year: First projection year in which the shock is active; the shock persists through the end of the five-year horizon.
        shock_type: Integer shock selector: 1 for the real GDP growth shock, 2 for the real interest rate shock, or 3 for the primary balance shock.
        shock_magnitudes: Three shock magnitudes in percentage points, ordered for growth, interest rate, and primary balance; the magnitude corresponding to shock_type is applied.
        country_profile_names: Recognized country profile names used to validate the selected country and resolve country-specific parameters.
        engine_year_labels: Projection year labels (1 through 5) used in the recursive engine and retained in the output ordering.

    Returns:
        Shocked debt-to-GDP path for projection years 1 through 5, as a tuple of values in spreadsheet order.
    """
    output_shocked, _ = _run_0(country_profile_names=country_profile_names, engine_year_labels=engine_year_labels, country_name=country_name, country_initial_debt=country_initial_debt, growth_baseline=growth_baseline, interest_baseline=interest_baseline, primary_balance_baseline=primary_balance_baseline, shock_year=shock_year, shock_type=shock_type, shock_magnitudes=shock_magnitudes)
    return tuple(output_shocked)

def compute_output_delta(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
    engine_year_labels: Sequence[int] = ENGINE_YEAR_LABELS,
) -> tuple[float | str, ...]:
    """Compute the shocked-minus-baseline debt-to-GDP path in percentage points.

    Return the output_delta row that the Outputs sheet exposes to downstream consumers.

    Args:
        country_name: User-selected country name, matched against the country profile table.
        country_initial_debt: Initial general-government debt-to-GDP ratio, expressed as a percentage of GDP.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balance for years 1 through 5, expressed as a percentage of GDP; positive values denote a surplus.
        shock_year: First year in which the shock takes effect, an integer from 1 to 5.
        shock_type: Parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, or 3 for the primary balance.
        shock_magnitudes: Shock magnitudes for the three shock types, in percentage points; only the magnitude corresponding to shock_type is applied.
        country_profile_names: Names of the available country profiles in the lookup table.
        engine_year_labels: Year labels for the projection columns, identifying the years 1 through 5.

    Returns:
        Tuple of output_delta values for the projection years, defined as shocked debt-to-GDP minus baseline debt-to-GDP in percentage points.
    """
    _, output_delta = _run_0(country_profile_names=country_profile_names, engine_year_labels=engine_year_labels, country_name=country_name, country_initial_debt=country_initial_debt, growth_baseline=growth_baseline, interest_baseline=interest_baseline, primary_balance_baseline=primary_balance_baseline, shock_year=shock_year, shock_type=shock_type, shock_magnitudes=shock_magnitudes)
    return tuple(output_delta)

__all__ = [
    'compute_output_baseline',
    'compute_output_shocked',
    'compute_output_delta',
]
