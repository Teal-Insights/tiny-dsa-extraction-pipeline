"""Output orchestrators for the inverted graph.

Each `compute_*` function takes the leaf closure of its subgraph.
There is no evaluation context and no input setters.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import internals
from .data import COUNTRY_PROFILE_NAMES, ENGINE_YEAR_LABELS
from .runtime import require_aligned, trim


def compute_output_baseline(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
) -> tuple[float, ...]:
    """Compute the baseline debt-to-GDP path for projection years 1 through 5.

    Return the baseline trajectory that feeds the documented output_baseline result from the engine's internal calculation.

    Args:
        country_name: User-selected country name, drawn from the country profile lookup table.
        country_initial_debt: Sequence of initial debt-to-GDP ratios corresponding to each country in country_profile_names, in the same order.
        growth_baseline: Real GDP growth rates for projection years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates for projection years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balances for projection years 1 through 5, expressed as a percent of GDP; positive values denote a surplus.
        country_profile_names: Sequence of country profile names in the lookup table; defaults to the standard profile set.

    Returns:
        A tuple of floats giving the baseline debt-to-GDP ratio (in percent of GDP) for each projection year, typically years 1 through 5.
    """
    horizon = min(require_aligned(growth_baseline, interest_baseline, primary_balance_baseline), 5)
    growth_baseline = trim(growth_baseline, horizon)
    interest_baseline = trim(interest_baseline, horizon)
    primary_balance_baseline = trim(primary_balance_baseline, horizon)
    initial_debt_resolved = internals.initial_debt_resolved(country_profile_names, country_name, country_initial_debt)
    engine_initial_debt_baseline = internals.engine_initial_debt_baseline(initial_debt_resolved)
    baseline_path_internal = internals.baseline_path_internal(engine_initial_debt_baseline, growth_baseline, interest_baseline, primary_balance_baseline)
    output_baseline = internals.output_baseline(baseline_path_internal)
    return tuple(output_baseline)

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
) -> tuple[float, ...]:
    """Compute the shocked debt-to-GDP path for projection years 1 through 5.

    Return the `output_shocked` series for the Outputs sheet by applying a configured shock to the baseline debt-dynamics recursion.

    Args:
        country_name: The user-selected country name, drawn from the country profile table.
        country_initial_debt: Initial debt-to-GDP ratios for the country profiles, in percent of GDP, aligned with `country_profile_names`.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balance for years 1 through 5, expressed as a percent of GDP, with positive values denoting a surplus.
        shock_year: The year in which the shock begins, an integer between 1 and 5.
        shock_type: The parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance.
        shock_magnitudes: Shock magnitudes for each shock type, in percentage points. Only the magnitude corresponding to `shock_type` is applied.
        country_profile_names: Names of the country profiles in the lookup table, used to resolve the initial debt for the selected `country_name`.
        engine_year_labels: Year labels used by the engine recursion, typically 1 through 5.

    Returns:
        The shocked debt-to-GDP path for projection years 1 through 5, expressed as percentages of GDP.
    """
    horizon = min(require_aligned(growth_baseline, interest_baseline, primary_balance_baseline), 5)
    growth_baseline = trim(growth_baseline, horizon)
    interest_baseline = trim(interest_baseline, horizon)
    primary_balance_baseline = trim(primary_balance_baseline, horizon)
    engine_year_labels = trim(engine_year_labels, horizon)
    initial_debt_resolved = internals.initial_debt_resolved(country_profile_names, country_name, country_initial_debt)
    shock_magnitude_resolved = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    shock_active = internals.shock_active(engine_year_labels, shock_year)
    engine_initial_debt_shocked = internals.engine_initial_debt_shocked(initial_debt_resolved)
    shocked_growth = internals.shocked_growth(growth_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_interest = internals.shocked_interest(interest_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_primary_balance = internals.shocked_primary_balance(primary_balance_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_path_internal = internals.shocked_path_internal(engine_initial_debt_shocked, shocked_growth, shocked_interest, shocked_primary_balance)
    output_shocked = internals.output_shocked(shocked_path_internal)
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
) -> tuple[float, ...]:
    """Compute the difference between the shocked and baseline debt-to-GDP paths in percentage points.

    Return the output_delta series, the shock impact on the debt ratio relative to the baseline path.

    Args:
        country_name: User-selected country name, drawn from the country profile table (e.g., 'Borvelia').
        country_initial_debt: Initial debt-to-GDP ratios for the candidate countries, aligned with country_profile_names; the selected country's value is looked up by name.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balance as a percent of GDP for years 1 through 5; positive values denote a surplus.
        shock_year: Year in which the shock begins (integer between 1 and 5).
        shock_type: Parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance.
        shock_magnitudes: Shock magnitudes in percentage points, one per shock type; the magnitude for the selected shock_type is applied.
        country_profile_names: Names of the available country profiles, used to resolve the selected country's initial debt.
        engine_year_labels: Year labels for the engine horizon, used to determine shock activation.

    Returns:
        Tuple of output_delta values, the difference between the shocked and baseline debt-to-GDP paths in percentage points.
    """
    horizon = min(require_aligned(growth_baseline, interest_baseline, primary_balance_baseline), 5)
    growth_baseline = trim(growth_baseline, horizon)
    interest_baseline = trim(interest_baseline, horizon)
    primary_balance_baseline = trim(primary_balance_baseline, horizon)
    engine_year_labels = trim(engine_year_labels, horizon)
    initial_debt_resolved = internals.initial_debt_resolved(country_profile_names, country_name, country_initial_debt)
    shock_magnitude_resolved = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    shock_active = internals.shock_active(engine_year_labels, shock_year)
    engine_initial_debt_baseline = internals.engine_initial_debt_baseline(initial_debt_resolved)
    engine_initial_debt_shocked = internals.engine_initial_debt_shocked(initial_debt_resolved)
    shocked_growth = internals.shocked_growth(growth_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_interest = internals.shocked_interest(interest_baseline, shock_type, shock_magnitude_resolved, shock_active)
    shocked_primary_balance = internals.shocked_primary_balance(primary_balance_baseline, shock_type, shock_magnitude_resolved, shock_active)
    baseline_path_internal = internals.baseline_path_internal(engine_initial_debt_baseline, growth_baseline, interest_baseline, primary_balance_baseline)
    shocked_path_internal = internals.shocked_path_internal(engine_initial_debt_shocked, shocked_growth, shocked_interest, shocked_primary_balance)
    output_baseline = internals.output_baseline(baseline_path_internal)
    output_shocked = internals.output_shocked(shocked_path_internal)
    output_delta = internals.output_delta(output_baseline, output_shocked)
    return tuple(output_delta)

__all__ = [
    'compute_output_baseline',
    'compute_output_shocked',
    'compute_output_delta',
]
