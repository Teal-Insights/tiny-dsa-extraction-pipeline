"""Output orchestrators for the inverted graph.

Each `compute_*` function takes the input leaf closure of its subgraph.
Constant leaves are read from `data` and listed on `__constants__`.
Key domains are listed on `__key__` / `__domain__`.
There is no evaluation context and no input setters.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import data
from . import internals
from .runtime import require_length


def compute_output_baseline(
    *,
    country_name: str,
    country_initial_debt: Sequence[float | str],
    growth_baseline: Sequence[float | str],
    interest_baseline: Sequence[float | str],
    primary_balance_baseline: Sequence[float | str],
) -> tuple[float | str, ...]:
    """Compute the baseline debt-to-GDP path for projection years 1 through 5.

    Return the baseline trajectory from the selected country and baseline scenario inputs.

    Args:
        country_name: Name of the selected country; must match one of the country profiles in the lookup table.
        country_initial_debt: Initial debt-to-GDP values (percent of GDP) for each profile country; the value for country_name is selected as the starting debt stock. Must have length 3.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates on general-government debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary fiscal balance for years 1 through 5, as percent of GDP; positive values denote a surplus.

    Returns:
        Baseline debt-to-GDP path for projection years 1 through 5, as a tuple of five values expressed in percent of GDP.
    """
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    initial_debt_resolved = internals.initial_debt_resolved(data.COUNTRY_PROFILE_NAMES, country_name, country_initial_debt)
    engine_initial_debt_baseline = internals.engine_initial_debt_baseline(initial_debt_resolved)
    baseline_path_internal = internals.baseline_path_internal(engine_initial_debt_baseline, growth_baseline, interest_baseline, primary_balance_baseline)
    output_baseline = internals.output_baseline(baseline_path_internal)
    return tuple(output_baseline)
setattr(compute_output_baseline, '__constants__', ('country_profile_names',))
setattr(compute_output_baseline, '__key__', ('TIME_PERIOD',))
setattr(compute_output_baseline, '__domain__', data.TIME_PERIOD_DOMAIN)

def _run_0(
    *,
    country_name: str,
    country_initial_debt: Sequence[float | str],
    growth_baseline: Sequence[float | str],
    interest_baseline: Sequence[float | str],
    primary_balance_baseline: Sequence[float | str],
    shock_year: int | str,
    shock_type: int | str,
    shock_magnitudes: Sequence[float | str],
) -> tuple[tuple[float | str, ...], tuple[float | str, ...]]:
    """Evaluate the shared formula closure of `output_shocked`, `output_delta`."""
    require_length(country_initial_debt, 3)
    require_length(growth_baseline, 5)
    require_length(interest_baseline, 5)
    require_length(primary_balance_baseline, 5)
    require_length(shock_magnitudes, 3)
    initial_debt_resolved = internals.initial_debt_resolved(data.COUNTRY_PROFILE_NAMES, country_name, country_initial_debt)
    shock_magnitude_resolved = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    shock_active = internals.shock_active(data.ENGINE_YEAR_LABELS, shock_year)
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
    country_initial_debt: Sequence[float | str],
    growth_baseline: Sequence[float | str],
    interest_baseline: Sequence[float | str],
    primary_balance_baseline: Sequence[float | str],
    shock_year: int | str,
    shock_type: int | str,
    shock_magnitudes: Sequence[float | str],
) -> tuple[float | str, ...]:
    """Compute the shocked debt-to-GDP path for projection years 1 through 5.

    Return the debt-to-GDP trajectory under the configured baseline parameters and a single additive shock.

    Args:
        country_name: Name of the selected country, used to identify the initial debt-to-GDP ratio from the country profile table.
        country_initial_debt: Initial debt-to-GDP ratio for the selected country, in percent of GDP, supplied as a single-element sequence.
        growth_baseline: Real GDP growth rates for projection years 1 through 5, expressed in percent per annum.
        interest_baseline: Real interest rates for projection years 1 through 5, expressed in percent per annum.
        primary_balance_baseline: Primary fiscal balance for projection years 1 through 5, expressed as a percent of GDP; positive values denote a surplus.
        shock_year: Year in which the shock begins, as an integer between 1 and 5; the shock applies from this year through the end of the horizon.
        shock_type: Integer indicating which parameter the shock affects: 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance.
        shock_magnitudes: Shock magnitudes in percentage points for the growth, interest-rate, and primary-balance shocks, respectively; only the magnitude corresponding to shock_type is applied.

    Returns:
        A tuple of five values representing the shocked debt-to-GDP path for projection years 1 through 5, in percent of GDP.
    """
    output_shocked, _ = _run_0(country_name=country_name, country_initial_debt=country_initial_debt, growth_baseline=growth_baseline, interest_baseline=interest_baseline, primary_balance_baseline=primary_balance_baseline, shock_year=shock_year, shock_type=shock_type, shock_magnitudes=shock_magnitudes)
    return tuple(output_shocked)
setattr(compute_output_shocked, '__constants__', ('country_profile_names', 'engine_year_labels'))
setattr(compute_output_shocked, '__key__', ('TIME_PERIOD',))
setattr(compute_output_shocked, '__domain__', data.TIME_PERIOD_DOMAIN)

def compute_output_delta(
    *,
    country_name: str,
    country_initial_debt: Sequence[float | str],
    growth_baseline: Sequence[float | str],
    interest_baseline: Sequence[float | str],
    primary_balance_baseline: Sequence[float | str],
    shock_year: int | str,
    shock_type: int | str,
    shock_magnitudes: Sequence[float | str],
) -> tuple[float | str, ...]:
    """Compute the difference between the shocked and baseline debt-to-GDP paths in percentage points.

    Return the year-by-year shocked-minus-baseline debt-to-GDP differences for output reporting.

    Args:
        country_name: Name of the selected country, drawn from the country profile lookup table. It determines the initial debt-to-GDP ratio in the workbook.
        country_initial_debt: Initial general-government debt-to-GDP ratio (in percent of GDP) for the selected country, provided as a one-element sequence. Corresponds to the country profile looked-up value.
        growth_baseline: Baseline real GDP growth rates for years 1 through 5, in percent per annum, used to build the baseline debt path.
        interest_baseline: Baseline real interest rates paid on outstanding general-government debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Baseline primary fiscal balance for years 1 through 5, expressed as percent of GDP; positive values denote surpluses.
        shock_year: First year (1 through 5) in which the configured shock takes effect; the shock persists through year 5.
        shock_type: Integer 1, 2, or 3 selecting the shocked parameter: 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance.
        shock_magnitudes: Three candidate shock magnitudes (in percentage points), corresponding respectively to growth, interest-rate, and primary-balance shocks; only the entry matching shock_type is applied.

    Returns:
        A five-element tuple of numbers (or Excel string representations) giving the difference between the shocked and baseline debt-to-GDP paths for years 1 through 5, in percentage points of GDP.
    """
    _, output_delta = _run_0(country_name=country_name, country_initial_debt=country_initial_debt, growth_baseline=growth_baseline, interest_baseline=interest_baseline, primary_balance_baseline=primary_balance_baseline, shock_year=shock_year, shock_type=shock_type, shock_magnitudes=shock_magnitudes)
    return tuple(output_delta)
setattr(compute_output_delta, '__constants__', ('country_profile_names', 'engine_year_labels'))
setattr(compute_output_delta, '__key__', ('TIME_PERIOD',))
setattr(compute_output_delta, '__domain__', data.TIME_PERIOD_DOMAIN)

__all__ = [
    'compute_output_baseline',
    'compute_output_shocked',
    'compute_output_delta',
]
