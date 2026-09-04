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
) -> tuple[float, ...]:
    """Compute the baseline debt-to-GDP path for the Outputs sheet from the leaf inputs.

    Returns the five-year baseline debt-to-GDP trajectory stored in `output_baseline`.

    Args:
        country_name: Name of the selected country, matched against `country_profile_names` to resolve initial debt.
        country_initial_debt: Sequence of initial debt-to-GDP ratios for each country profile, used with the selected country to determine the starting debt level.
        growth_baseline: Five-year sequence of real GDP growth rates, percent per annum.
        interest_baseline: Five-year sequence of effective real interest rates on outstanding debt, percent per annum.
        primary_balance_baseline: Five-year sequence of primary balances as percent of GDP, positive for surplus.
        country_profile_names: Sequence of country profile names corresponding to `country_initial_debt`; used to look up the selected country.

    Returns:
        A tuple of five floats giving the baseline debt-to-GDP path for years 1 through 5.
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

    Return the shocked debt-to-GDP series after applying the configured shock to the baseline parameter path.

    Args:
        country_name: Name of the selected country from the country profile table; used to resolve the initial debt-to-GDP ratio.
        country_initial_debt: Sequence of length 3 containing initial debt-to-GDP ratios (percent of GDP) for the three country profiles, aligned with country_profile_names.
        growth_baseline: Baseline real GDP growth rates (percent per annum) for years 1 through 5.
        interest_baseline: Baseline real interest rates (percent per annum) for years 1 through 5.
        primary_balance_baseline: Baseline primary balance (percent of GDP, positive surplus) for years 1 through 5.
        shock_year: Year (1 to 5) in which the shock first takes effect, applying through the end of the horizon.
        shock_type: Integer 1, 2, or 3 indicating which parameter the shock affects: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance.
        shock_magnitudes: Sequence of length 3 shock magnitudes in percentage points, one per shock type, of which only the selected type's magnitude is applied.
        country_profile_names: Sequence of length 3 country profile names used to look up the selected country's initial debt; defaults to built-in profile names.
        engine_year_labels: Sequence of length 5 year labels (1 through 5) used to determine shock activation; defaults to built-in engine year labels.

    Returns:
        Tuple of five floats containing the shocked debt-to-GDP ratios (percent of GDP) for years 1 through 5.
    """
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    require_length(shock_magnitudes, 3)
    require_length(country_profile_names, 3)
    require_length(engine_year_labels, 5)
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
    """Compute the difference between shocked and baseline debt-to-GDP paths, in percentage points.

    Return the output_delta series for the Outputs sheet, representing the impact of the configured shock on the debt-to-GDP path.

    Args:
        country_name: User-selected country, drawn from the country profile table. Determines the initial debt-to-GDP ratio via lookup.
        country_initial_debt: Initial debt-to-GDP ratios for each country profile, in percent of GDP. The ratio for the selected country is resolved from these values.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates paid on outstanding debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balance for years 1 through 5, expressed as a percent of GDP. Positive values denote a surplus.
        shock_year: The year in which the shock begins, as an integer between 1 and 5. The shock applies from this year through the end of the horizon.
        shock_type: The parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance.
        shock_magnitudes: Shock magnitudes for each shock type, in percentage points. The magnitude corresponding to the selected shock type is applied from the shock year onwards.
        country_profile_names: Names of the country profiles in the lookup table (for example, Borvelia, Litellia, Aurelium). Used to resolve the initial debt-to-GDP ratio for the selected country.
        engine_year_labels: Year labels for the five-year horizon (typically 1 through 5). Used to determine the shock-active indicator for each year.

    Returns:
        A tuple of five floats representing the output delta series: the difference between the shocked and baseline debt-to-GDP paths, in percentage points, for years 1 through 5.
    """
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    require_length(shock_magnitudes, 3)
    require_length(country_profile_names, 3)
    require_length(engine_year_labels, 5)
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
