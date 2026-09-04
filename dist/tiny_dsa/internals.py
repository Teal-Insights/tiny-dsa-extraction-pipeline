"""First-level-dependency internals for the inverted graph."""

from __future__ import annotations

from collections.abc import Sequence

from .runtime import require_aligned, xl_at, xl_choose, xl_div, xl_match


def initial_debt_resolved(country_profile_names: Sequence[str], country_name: str, country_initial_debt: Sequence[float]) -> float:
    """Resolve the initial debt-to-GDP ratio for a selected country from the country profile table.

    Return the initial debt-to-GDP ratio corresponding to the selected country name via an exact-match lookup.

    Args:
        country_profile_names: Sequence of country names in the profile table that serves as the lookup key column.
        country_name: Name of the selected country to match against the profile names.
        country_initial_debt: Sequence of initial debt-to-GDP ratios aligned with country_profile_names, from which the matched value is returned.

    Returns:
        The initial debt-to-GDP ratio (as a float) for the country whose name matches country_name in country_profile_names.
    """
    return float(xl_at(country_initial_debt, (xl_match(country_name, country_profile_names, 0)) - 1))

def engine_initial_debt_baseline(initial_debt_resolved: float) -> float:
    """Return the resolved year-0 debt-to-GDP anchor for the baseline recursion on the Engine sheet.

    Provide the initial debt stock from which the Engine sheet's baseline debt path is recursively projected.

    Args:
        initial_debt_resolved: Resolved initial debt-to-GDP ratio (percent of GDP), corresponding to the year-0 anchor used by the baseline recursion.

    Returns:
        The same resolved initial debt-to-GDP ratio as a float, serving as the year-0 input to the baseline debt-dynamics recursion.
    """
    return float(initial_debt_resolved)

def engine_initial_debt_shocked(initial_debt_resolved: float) -> float:
    """Return the resolved initial debt-to-GDP ratio as the year-0 debt anchor for the shocked recursion.

    Provides the starting debt stock used by the Engine sheet shocked path.

    Args:
        initial_debt_resolved: The resolved initial debt-to-GDP ratio for the selected country, serving as the year-0 anchor for the shocked debt recursion.

    Returns:
        The same value as the input, representing the year-0 debt stock for the shocked path on the Engine sheet.
    """
    return float(initial_debt_resolved)

def shock_magnitude_resolved(shock_type: int, shock_magnitudes: Sequence[float]) -> float:
    """Return the shock magnitude for the selected shock type from the shock table.

    Isolate the lookup of the resolved shock magnitude for a given shock type and magnitudes sequence.

    Args:
        shock_type: Integer between 1 and 3 selecting the parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance.
        shock_magnitudes: Sequence of three shock magnitudes in percentage points, one per shock type, in the order growth, interest, primary balance.

    Returns:
        The shock magnitude associated with the selected shock type, in percentage points.
    """
    return float(xl_at(shock_magnitudes, ((shock_type - 1))))

def shock_active(engine_year_labels: Sequence[int], shock_year: int) -> tuple[int, ...]:
    """Compute the shock activation flag for each projection year.

    Returns a tuple of 1s and 0s indicating whether the shock is active in each year, defined as the year label being at or after the shock year.

    Args:
        engine_year_labels: Sequence of projection year labels (e.g., 1, 2, ..., 5) used to determine shock activation.
        shock_year: First year in which the shock takes effect; years at or after this label are flagged as active.

    Returns:
        Tuple of integers, one per projection year, where 1 indicates the shock is active in that year and 0 otherwise.
    """
    n = require_aligned(engine_year_labels)
    return tuple(int((1 if (engine_year_labels[i] >= shock_year) else 0)) for i in range(n))

def shocked_growth(growth_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Compute the shocked real GDP growth path by adding the selected growth shock magnitude to baseline growth in years where the shock is active.

    First-level helper for the bound shocked_growth series.

    Args:
        growth_baseline: Sequence of baseline real GDP growth rates (percent per annum) for years 1 through 5.
        shock_type: Integer 1, 2, or 3 selecting which parameter the shock affects; only type 1 (real GDP growth) applies the resolved magnitude here.
        shock_magnitude_resolved: Resolved shock magnitude for the selected shock type, in percentage points, looked up from the shock table.
        shock_active: Sequence of 0/1 indicators, one per year, equal to 1 from the configured shock year through the end of the horizon.

    Returns:
        Tuple of shocked real GDP growth rates (percent per annum) for years 1 through 5, equal to baseline growth plus the resolved magnitude in years where shock_active is 1 and unchanged otherwise.
    """
    n = require_aligned(growth_baseline, shock_active)
    return tuple(float((growth_baseline[i] + (xl_choose(shock_type, shock_magnitude_resolved, 0, 0) * shock_active[i]))) for i in range(n))

def shocked_interest(interest_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Shocked real interest rate path after applying the selected shock magnitude.

    Constructs the shocked real interest rate path for the five-year horizon, used to compute the shocked debt trajectory.

    Args:
        interest_baseline: Baseline real interest rates for years 1 through 5, in percent per annum.
        shock_type: Integer shock type (1 = growth, 2 = real interest rate, 3 = primary balance); the shock magnitude is applied only when this equals 2.
        shock_magnitude_resolved: Shock magnitude looked up from the shock table for the selected shock type, in percentage points.
        shock_active: Year-by-year indicator (0 or 1) of whether the shock is active, with 1 for years from the shock year onwards.

    Returns:
        A tuple of the shocked real interest rates, in percent per annum, for years 1 through 5.
    """
    n = require_aligned(interest_baseline, shock_active)
    return tuple(float((interest_baseline[i] + (xl_choose(shock_type, 0, shock_magnitude_resolved, 0) * shock_active[i]))) for i in range(n))

def shocked_primary_balance(primary_balance_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Compute the shocked primary balance path after applying the selected shock magnitude.

    First-level helper for the bound series `shocked_primary_balance`; applies the resolved shock magnitude to the baseline primary balance in each year the shock is active.

    Args:
        primary_balance_baseline: Baseline primary balance for years 1 through 5, expressed as a percent of GDP, with positive values denoting a surplus.
        shock_type: Shock type selector integer (1, 2, or 3). For primary-balance shocks, a value of 3 selects the resolved magnitude; other types contribute zero to the primary-balance adjustment.
        shock_magnitude_resolved: Shock magnitude in percentage points for the selected shock type, resolved from the shock table via OFFSET on the Engine sheet.
        shock_active: Indicator sequence, equal to 1 in each year the shock is active (year >= shock_year) and 0 otherwise.

    Returns:
        Tuple of shocked primary balance values for each year, computed as the baseline primary balance plus the selected shock magnitude in years the shock is active, and equal to the baseline otherwise.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    return tuple(float((primary_balance_baseline[i] + (xl_choose(shock_type, 0, 0, shock_magnitude_resolved) * shock_active[i]))) for i in range(n))

def baseline_path_internal(engine_initial_debt_baseline: float, growth_baseline: Sequence[float], interest_baseline: Sequence[float], primary_balance_baseline: Sequence[float]) -> tuple[float, ...]:
    """Compute the internal baseline debt-to-GDP path over the projection horizon.

    Compute the baseline debt-to-GDP path for the Engine sheet, recursing from the initial debt level using the standard real-terms debt-dynamics identity.

    Args:
        engine_initial_debt_baseline: Initial general government debt-to-GDP ratio at the end of the prior year, expressed as a percentage.
        growth_baseline: Sequence of annual real GDP growth rates, in percent per annum, one per projection year.
        interest_baseline: Sequence of annual effective real interest rates on outstanding general government debt, in percent per annum, one per projection year.
        primary_balance_baseline: Sequence of annual primary balances as a percent of GDP, positive for a surplus and negative for a deficit, one per projection year.

    Returns:
        Tuple of projected debt-to-GDP ratios, one per projection year, computed via debt(t) = debt(t-1) * (1 + r/100) / (1 + g/100) - primary_balance(t).
    """
    n = require_aligned(growth_baseline, interest_baseline, primary_balance_baseline)
    path: list[float] = []
    prior = engine_initial_debt_baseline
    for i in range(n):
        prior = float((xl_div((prior * (1 + xl_div(interest_baseline[i], 100))), (1 + xl_div(growth_baseline[i], 100))) - primary_balance_baseline[i]))
        path.append(prior)
    return tuple(path)

def shocked_path_internal(engine_initial_debt_shocked: float, shocked_growth: Sequence[float], shocked_interest: Sequence[float], shocked_primary_balance: Sequence[float]) -> tuple[float, ...]:
    """Compute the internal shocked debt-to-GDP path over the five-year horizon.

    Recursively apply the real-terms debt-dynamics identity to the shocked parameter series, returning the year-by-year debt-to-GDP ratios.

    Args:
        engine_initial_debt_shocked: Initial (year-0) general-government debt-to-GDP ratio used as the starting value for the shocked path.
        shocked_growth: Shocked real GDP growth rates for years 1 through 5, in percent per annum.
        shocked_interest: Shocked real interest rates for years 1 through 5, in percent per annum.
        shocked_primary_balance: Shocked primary balances for years 1 through 5, expressed as percent of GDP with positive values denoting a surplus.

    Returns:
        A tuple of five year-end debt-to-GDP ratios, one per year, following the shocked path.
    """
    n = require_aligned(shocked_growth, shocked_interest, shocked_primary_balance)
    path: list[float] = []
    prior = engine_initial_debt_shocked
    for i in range(n):
        prior = float((xl_div((prior * (1 + xl_div(shocked_interest[i], 100))), (1 + xl_div(shocked_growth[i], 100))) - shocked_primary_balance[i]))
        path.append(prior)
    return tuple(path)

def output_baseline(baseline_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Return baseline debt-to-GDP path as a tuple of floats.

    Bind the internal baseline path to the output baseline series.

    Args:
        baseline_path_internal: The internal baseline debt-to-GDP path for projection years 1 through 5.

    Returns:
        A tuple of floats containing the baseline debt-to-GDP path for years 1 through 5.
    """
    n = require_aligned(baseline_path_internal)
    return tuple(float(baseline_path_internal[i]) for i in range(n))

def output_shocked(shocked_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Convert the internal shocked debt-to-GDP path into the output tuple for the Outputs sheet.

    First-level helper for the bound series `output_shocked`, aligning and casting the internal shocked path values for downstream exposure.

    Args:
        shocked_path_internal: The internal shocked debt-to-GDP path for projection years 1 through 5, as calculated on the Engine sheet (typically the `shocked_path` range).

    Returns:
        A tuple of floats representing the shocked debt-to-GDP path for projection years 1 through 5, ready to be exposed on the Outputs sheet as the `output_shocked` range.
    """
    n = require_aligned(shocked_path_internal)
    return tuple(float(shocked_path_internal[i]) for i in range(n))

def output_delta(output_baseline: Sequence[float], output_shocked: Sequence[float]) -> tuple[float, ...]:
    """Compute the difference between the shocked and baseline debt-to-GDP paths in percentage points.

    Return the per-year shocked-minus-baseline difference for the output_delta bound series.

    Args:
        output_baseline: Baseline debt-to-GDP path on the Outputs sheet, expressed in percent of GDP.
        output_shocked: Shocked debt-to-GDP path on the Outputs sheet, expressed in percent of GDP.

    Returns:
        A tuple of per-year differences in percentage points, each computed as shocked minus baseline.
    """
    n = require_aligned(output_baseline, output_shocked)
    return tuple(float((output_shocked[i] - output_baseline[i])) for i in range(n))
