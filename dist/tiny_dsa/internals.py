"""First-level-dependency internals for the inverted graph."""

from __future__ import annotations

from collections.abc import Sequence

from .runtime import require_aligned, xl_at, xl_choose, xl_div, xl_match


def initial_debt_resolved(country_profile_names: Sequence[str], country_name: str, country_initial_debt: Sequence[float]) -> float:
    """Initial debt-to-GDP ratio resolved from the country profile via INDEX/MATCH.

    Looks up and returns the initial debt-to-GDP ratio for the selected country from the profile table.

    Args:
        country_profile_names: Sequence of country names in the country profile lookup table (first column). Used as the lookup range for the MATCH step.
        country_name: Name of the selected country, drawn from the Inputs sheet. Must match an entry in country_profile_names.
        country_initial_debt: Sequence of initial debt-to-GDP ratios corresponding to the country profile rows (second column). The matched row's value is returned.

    Returns:
        Float value of the initial debt-to-GDP ratio for the selected country, as a percentage of GDP.
    """
    return float(xl_at(country_initial_debt, (xl_match(country_name, country_profile_names, 0)) - 1))

def engine_initial_debt_baseline(initial_debt_resolved: float) -> float:
    """Return the resolved year-0 debt-to-GDP anchor for the baseline recursion on the Engine sheet.

    Provides the initial debt stock from which the baseline debt-to-GDP path is recursively projected.

    Args:
        initial_debt_resolved: The resolved initial general-government debt-to-GDP ratio at end of year 0, expressed as a percentage of GDP.

    Returns:
        The initial debt-to-GDP ratio as a float, used as the year-0 anchor in the baseline recursion.
    """
    return float(initial_debt_resolved)

def engine_initial_debt_shocked(initial_debt_resolved: float) -> float:
    """Pass through the resolved year-0 debt-to-GDP ratio as the shocked recursion anchor.

    Supply the initial debt stock used to seed the shocked debt path on the Engine sheet.

    Args:
        initial_debt_resolved: The resolved initial debt-to-GDP ratio (%) for the selected country, used as the year-0 anchor for the shocked recursion.

    Returns:
        The value of initial_debt_resolved as a float, representing the year-0 debt stock anchor for the shocked recursion.
    """
    return float(initial_debt_resolved)

def shock_magnitude_resolved(shock_type: int, shock_magnitudes: Sequence[float]) -> float:
    """Resolve the shock magnitude for the selected shock type from the shock table.

    Select the configured magnitude corresponding to the active shock type.

    Args:
        shock_type: Integer 1-3 identifying the parameter affected by the shock: 1 for real GDP growth, 2 for real interest rate, 3 for primary balance.
        shock_magnitudes: Sequence of three shock magnitudes, one per shock type, in the order growth, interest, primary balance.

    Returns:
        The shock magnitude for the selected shock type, as a float.
    """
    return float(xl_at(shock_magnitudes, ((shock_type - 1))))

def shock_active(engine_year_labels: Sequence[int], shock_year: int) -> tuple[int, ...]:
    """Compute the shock activation flag for each projection year.

    Indicate which projection years are subject to the configured shock.

    Args:
        engine_year_labels: Sequence of projection year labels (e.g., 1 through 5) used to determine when the shock begins.
        shock_year: First year in which the shock takes effect; years at or after this value are marked as active.

    Returns:
        A tuple of integers, one per projection year, containing 1 when the year label is greater than or equal to shock_year and 0 otherwise.
    """
    n = require_aligned(engine_year_labels)
    return tuple(int((1 if (engine_year_labels[i] >= shock_year) else 0)) for i in range(n))

def shocked_growth(growth_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Shocked real GDP growth path after applying the selected shock magnitude.

    Construct the year-by-year real GDP growth path under the configured shock, applying the resolved growth magnitude only for growth-type shocks.

    Args:
        growth_baseline: Real GDP growth rates in percent per annum for years 1 through 5 under the baseline scenario, before any shock is applied.
        shock_type: Integer 1, 2, or 3 selecting the parameter affected by the shock; 1 applies the resolved magnitude to real GDP growth, while other types leave growth unchanged.
        shock_magnitude_resolved: Resolved shock magnitude in percentage points for the selected shock type, looked up from the shock table; applied only when shock_type equals 1.
        shock_active: Sequence of per-year indicators (1 or 0) marking whether the shock is active in that year, i.e., 1 for years at or after the shock year.

    Returns:
        Tuple of real GDP growth rates for years 1 through 5 after applying the growth shock: baseline growth plus the resolved magnitude when shock_type is 1 and the shock is active in that year; otherwise baseline growth unchanged.
    """
    n = require_aligned(growth_baseline, shock_active)
    return tuple(float((growth_baseline[i] + (xl_choose(shock_type, shock_magnitude_resolved, 0, 0) * shock_active[i]))) for i in range(n))

def shocked_interest(interest_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Return the shocked real interest rate path for the five-year horizon.

    Computes the real interest rate path after applying the configured interest-rate shock, forming the shocked parameter row used in the debt-dynamics recursion.

    Args:
        interest_baseline: Baseline real interest rates for years 1 through 5, expressed in percent per annum, representing the effective real rate paid on outstanding general-government debt.
        shock_type: Integer between 1 and 3 indicating which parameter the shock affects. A value of 2 denotes the real interest rate; any other value leaves the interest rate unchanged.
        shock_magnitude_resolved: Resolved shock magnitude for the selected shock type, in percentage points. For an interest-rate shock, this is the additive change applied to the baseline real interest rate.
        shock_active: Sequence of 0/1 indicators for years 1 through 5, one per year, equal to 1 when the shock is active in that year (i.e., year is at or after the shock year) and 0 otherwise.

    Returns:
        A tuple of shocked real interest rates for years 1 through 5, in percent per annum, formed by adding the resolved shock magnitude to the baseline rate for each year in which the shock is active.
    """
    n = require_aligned(interest_baseline, shock_active)
    return tuple(float((interest_baseline[i] + (xl_choose(shock_type, 0, shock_magnitude_resolved, 0) * shock_active[i]))) for i in range(n))

def shocked_primary_balance(primary_balance_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float, shock_active: Sequence[int]) -> tuple[float, ...]:
    """Shocked primary balance path after applying the selected shock magnitude to the primary balance.

    Compute the primary balance path used in the shocked debt trajectory.

    Args:
        primary_balance_baseline: Baseline primary balance path, expressed as percent of GDP with positive values denoting a surplus.
        shock_type: Shock type selector as an integer: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance. Only type 3 adds the shock magnitude to the primary balance.
        shock_magnitude_resolved: Resolved shock magnitude, in percentage points, from the shock table for the selected shock type; applied to the primary balance only when shock_type equals 3.
        shock_active: Year-by-year indicator of whether the shock is in effect: 1 from the shock year onward, 0 before it.

    Returns:
        Tuple of shocked primary-balance values for each year, expressed as percent of GDP.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    return tuple(float((primary_balance_baseline[i] + (xl_choose(shock_type, 0, 0, shock_magnitude_resolved) * shock_active[i]))) for i in range(n))

def baseline_path_internal(engine_initial_debt_baseline: float, growth_baseline: Sequence[float], interest_baseline: Sequence[float], primary_balance_baseline: Sequence[float]) -> tuple[float, ...]:
    """Compute the internal baseline debt-to-GDP path from an initial ratio and baseline macroeconomic and fiscal series.

    Implement the real-terms debt-dynamics recursion for the baseline scenario as exposed by the Engine sheet's baseline_path named range.

    Args:
        engine_initial_debt_baseline: Initial general-government debt-to-GDP ratio at the end of the pre-projection year (year 0), expressed as a percentage of GDP.
        growth_baseline: Annual real GDP growth rates for the projection horizon, in percent per annum.
        interest_baseline: Annual effective real interest rates on outstanding general-government debt over the projection horizon, in percent per annum.
        primary_balance_baseline: Annual primary fiscal balances as a percentage of GDP over the projection horizon, with positive values denoting surpluses.

    Returns:
        Tuple of debt-to-GDP ratios, in percent of GDP, computed recursively year by year from the initial ratio using the debt-dynamics identity.
    """
    n = require_aligned(growth_baseline, interest_baseline, primary_balance_baseline)
    path: list[float] = []
    prior = engine_initial_debt_baseline
    for i in range(n):
        prior = float((xl_div((prior * (1 + xl_div(interest_baseline[i], 100))), (1 + xl_div(growth_baseline[i], 100))) - primary_balance_baseline[i]))
        path.append(prior)
    return tuple(path)

def shocked_path_internal(engine_initial_debt_shocked: float, shocked_growth: Sequence[float], shocked_interest: Sequence[float], shocked_primary_balance: Sequence[float]) -> tuple[float, ...]:
    """Recursively compute the shocked debt-to-GDP path from the shocked growth, interest, and primary balance series.

    Provides the internal Engine-sheet shocked path calculation used as a helper for the bound series.

    Args:
        engine_initial_debt_shocked: Debt-to-GDP ratio (percent of GDP) at the start of the projection horizon, i.e., end of year 0, from which the shocked path recurses.
        shocked_growth: Real GDP growth rates (percent per annum) under the shock for each year of the horizon, aligned elementwise with the other shocked series.
        shocked_interest: Real interest rates (percent per annum) under the shock for each year of the horizon.
        shocked_primary_balance: Primary balances (percent of GDP, positive surplus) under the shock for each year of the horizon.

    Returns:
        Tuple of shocked debt-to-GDP ratios (percent of GDP) for years 1 through n, computed by recursing the snowball identity with the shocked parameters.
    """
    n = require_aligned(shocked_growth, shocked_interest, shocked_primary_balance)
    path: list[float] = []
    prior = engine_initial_debt_shocked
    for i in range(n):
        prior = float((xl_div((prior * (1 + xl_div(shocked_interest[i], 100))), (1 + xl_div(shocked_growth[i], 100))) - shocked_primary_balance[i]))
        path.append(prior)
    return tuple(path)

def output_baseline(baseline_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Convert the internal baseline debt-to-GDP path to the output baseline tuple.

    Provides the externally consumed baseline debt-to-GDP trajectory for projection years 1 through 5.

    Args:
        baseline_path_internal: The internally computed baseline debt-to-GDP path (Engine range `baseline_path`) to be exposed as the output baseline series.

    Returns:
        A tuple of floats representing the baseline debt-to-GDP ratio for each projection year, aligned with the Outputs sheet range `output_baseline`.
    """
    n = require_aligned(baseline_path_internal)
    return tuple(float(baseline_path_internal[i]) for i in range(n))

def output_shocked(shocked_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Shocked debt-to-GDP path for projection years 1 through 5.

    Convert the internal shocked-path series to an immutable tuple for downstream output.

    Args:
        shocked_path_internal: Sequence of debt-to-GDP ratio values for the shocked scenario, one per projection year, aligned to the projection horizon.

    Returns:
        A tuple of floats representing the shocked debt-to-GDP path, one value per projection year.
    """
    n = require_aligned(shocked_path_internal)
    return tuple(float(shocked_path_internal[i]) for i in range(n))

def output_delta(output_baseline: Sequence[float], output_shocked: Sequence[float]) -> tuple[float, ...]:
    """Compute the shocked-minus-baseline debt-to-GDP path in percentage points.

    Return the year-by-year difference between the shocked and baseline debt-to-GDP trajectories, expressed in percentage points of GDP.

    Args:
        output_baseline: Baseline debt-to-GDP path as a sequence of percentages of GDP for years 1 through 5, from the Outputs sheet (`output_baseline`).
        output_shocked: Shocked debt-to-GDP path as a sequence of percentages of GDP for years 1 through 5, from the Outputs sheet (`output_shocked`).

    Returns:
        A tuple of length equal to the aligned series, each element the shocked value minus the baseline value for that year, in percentage points.
    """
    n = require_aligned(output_baseline, output_shocked)
    return tuple(float((output_shocked[i] - output_baseline[i])) for i in range(n))
