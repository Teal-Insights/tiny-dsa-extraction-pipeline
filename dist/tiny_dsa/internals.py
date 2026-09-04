"""First-level-dependency internals for the inverted graph."""

from __future__ import annotations

from collections.abc import Sequence

from .runtime import XlError, as_measure, live_measure, require_aligned, xl_add, xl_at, xl_choose, xl_div, xl_ge, xl_match, xl_mul, xl_sub


def initial_debt_resolved(country_profile_names: Sequence[str], country_name: str, country_initial_debt: Sequence[float]) -> float | str:
    """Resolve the initial debt-to-GDP ratio for a country from the profile table by exact name match.

    Look up the initial debt-to-GDP ratio from the country profile using INDEX/MATCH semantics for the debt-dynamics recursion.

    Args:
        country_profile_names: Sequence of country names from the country profile table; used as the lookup column for matching.
        country_name: The selected country name to match against the profile names; must exactly match one entry to resolve a debt ratio.
        country_initial_debt: Sequence of initial debt-to-GDP ratios aligned by position with country_profile_names; the value corresponding to the matched country is returned.

    Returns:
        The matched initial debt-to-GDP ratio as a numeric measure (float), or the Excel error code string when the country name is not found or the lookup operation fails.
    """
    try:
        return as_measure(xl_at(country_initial_debt, (xl_match(country_name, country_profile_names, 0)) - 1))
    except XlError as err:
        return err.code

def engine_initial_debt_baseline(initial_debt_resolved: float | str) -> float | str:
    """Return the year-0 debt stock anchor for the Engine-sheet baseline recursion as a measure, preserving Excel error codes.

    Supply the initial debt-to-GDP ratio from which the baseline debt path is recursed on the Engine sheet.

    Args:
        initial_debt_resolved: Resolved year-0 initial debt-to-GDP value, as a numeric measure or an Excel error code.

    Returns:
        The initial debt stock for year 0 as a measure, or the original Excel error code when the input cannot be converted.
    """
    try:
        return as_measure(initial_debt_resolved)
    except XlError as err:
        return err.code

def engine_initial_debt_shocked(initial_debt_resolved: float | str) -> float | str:
    """Return the year-0 debt stock anchor for the shocked recursion on the Engine sheet.

    Provide the initial debt level that seeds the shocked debt-path calculation.

    Args:
        initial_debt_resolved: The resolved year-0 debt stock for the selected country, expressed as a numeric value or an Excel error string.

    Returns:
        The initial debt stock as a float measure used as the starting point for the shocked recursion, or an Excel error code string if the resolved input cannot be interpreted as a measure.
    """
    try:
        return as_measure(initial_debt_resolved)
    except XlError as err:
        return err.code

def shock_magnitude_resolved(shock_type: int, shock_magnitudes: Sequence[float]) -> float | str:
    """Return the shock magnitude for the selected shock type.

    Selects the applicable magnitude from the shock table for the active shock type, mirroring the worksheet's OFFSET lookup.

    Args:
        shock_type: Integer shock type, 1 for real GDP growth, 2 for real interest rate, or 3 for primary balance.
        shock_magnitudes: Sequence of three shock magnitudes, one per shock type, expressed in percentage points.

    Returns:
        The shock magnitude corresponding to the selected shock type, or an Excel error code string if the lookup fails.
    """
    try:
        return as_measure(xl_at(shock_magnitudes, (xl_sub(shock_type, 1))))
    except XlError as err:
        return err.code

def shock_active(engine_year_labels: Sequence[int], shock_year: int) -> tuple[int | str, ...]:
    """Compute the shock activation indicator for each projection year.

    Return 1 for years at or after the shock year and 0 otherwise, matching the Engine sheet's shock-active flag.

    Args:
        engine_year_labels: Sequence of projection-year labels (e.g., 1 through 5) against which the shock year is compared.
        shock_year: First year in which the shock takes effect. Years at or after this value produce a flag of 1.

    Returns:
        Tuple with one element per projection year: 1 if that year is at or after shock_year, otherwise 0. If a comparison with an individual year raises an Excel error, the corresponding element is that error's code string.
    """
    n = require_aligned(engine_year_labels)
    out: list[int | str] = []
    for i in range(n):
        try:
            out.append(as_measure((1 if xl_ge(engine_year_labels[i], shock_year) else 0), 'int'))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def shocked_growth(growth_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Shocked real GDP growth path after applying the selected shock magnitude.

    Overlay the resolved growth-shock magnitude on the baseline real GDP growth series for each year in which the shock is active.

    Args:
        growth_baseline: Baseline real GDP growth rates, in percent per annum, for years 1 through 5.
        shock_type: Integer selector identifying the parameter affected by the shock; values are 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance. Only a value of 1 applies the growth magnitude in this helper.
        shock_magnitude_resolved: Resolved shock magnitude for the selected shock type, expressed in percentage points. When shock_type is 1, this magnitude is added to baseline growth; otherwise it is not applied to the growth series.
        shock_active: Sequence of per-year indicators, aligned with growth_baseline, marking whether the shock is in force in each year. Non-zero values denote active shock years.

    Returns:
        Tuple of shocked real GDP growth rates in percent per annum, one for each year in the horizon. Years before the shock, or years when the shock does not target growth, retain the baseline growth rate. Excel error codes encountered during the calculation are propagated as strings.
    """
    n = require_aligned(growth_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(growth_baseline[i], xl_mul(xl_choose(shock_type, shock_magnitude_resolved, 0, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def shocked_interest(interest_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Shocked real interest rate path after applying the selected shock magnitude to the baseline real interest rate wherever the shock is active.

    Constructs the shocked interest-rate input row used by the debt-dynamics recursion.

    Args:
        interest_baseline: Baseline real interest rates, in percent per annum, for the relevant years of the horizon.
        shock_type: Shock type selector: 1 for real GDP growth, 2 for the real interest rate, or 3 for the primary balance. The shock magnitude is applied to the interest baseline only when shock_type is 2.
        shock_magnitude_resolved: Resolved magnitude of the selected shock, in percentage points, as looked up from the shock table. May also carry an Excel error-code string if the lookup fails.
        shock_active: Per-year indicator equal to 1 from the configured shock year onward and 0 before it, with one entry per modeled year. May also carry Excel error-code strings.

    Returns:
        A tuple with one shocked real interest rate per year, in percent per annum, or an Excel error-code string when any underlying Excel operation returns an XlError.
    """
    n = require_aligned(interest_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(interest_baseline[i], xl_mul(xl_choose(shock_type, 0, shock_magnitude_resolved, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def shocked_primary_balance(primary_balance_baseline: Sequence[float], shock_type: int, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Shocked primary balance path after applying the selected shock magnitude.

    Compute the primary-balance series with the configured shock applied from the designated shock year onward.

    Args:
        primary_balance_baseline: Baseline primary balance series, in percent of GDP per year, with positive values representing a surplus.
        shock_type: Integer code selecting the parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance. Only code 3 contributes a nonzero shock to this helper.
        shock_magnitude_resolved: Resolved shock magnitude for the active shock type, in percentage points; applied only when shock_type is 3 and otherwise effectively zero. Sign follows the shock convention, e.g., negative for a primary-balance reduction.
        shock_active: Indicator series with 0 before the shock year and 1 from the shock year onward, controlling when the shock is added to the baseline path.

    Returns:
        Tuple of shocked primary balance values in percent of GDP (positive for surplus), one per year; entries are floats or Excel error-code strings when a formula error occurs.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(primary_balance_baseline[i], xl_mul(xl_choose(shock_type, 0, 0, shock_magnitude_resolved), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def baseline_path_internal(engine_initial_debt_baseline: float | str, growth_baseline: Sequence[float], interest_baseline: Sequence[float], primary_balance_baseline: Sequence[float]) -> tuple[float | str, ...]:
    """Computes the internal baseline debt-to-GDP path on the Engine sheet, applying the real-terms debt-dynamics identity year by year.

    Provides the calculation behind the Engine-sheet `baseline_path` named range, used internally for the workbook's baseline trajectory before it is mirrored to the Outputs sheet.

    Args:
        engine_initial_debt_baseline: Debt-to-GDP ratio at the end of year 0, expressed as a percent of GDP. This is the starting value for the recursion and is usually looked up from the selected country profile.
        growth_baseline: Series of real GDP growth rates for years 1 through 5, in percent per annum. Its length must match interest_baseline and primary_balance_baseline.
        interest_baseline: Series of effective real interest rates paid on outstanding general-government debt in years 1 through 5, in percent per annum. Its length must match growth_baseline and primary_balance_baseline.
        primary_balance_baseline: Series of primary fiscal balances for years 1 through 5, expressed as a percent of GDP with positive values denoting surpluses. Its length must match growth_baseline and interest_baseline.

    Returns:
        A tuple with one baseline debt-to-GDP value per year over the aligned horizon, expressed as a percent of GDP. Each year t is computed recursively as baseline[t] = baseline[t-1] * (1 + interest_baseline[t]/100) / (1 + growth_baseline[t]/100) - primary_balance_baseline[t], with baseline[0] replacing baseline[t-1] by engine_initial_debt_baseline. When a period's formula raises an Excel error, that element is the corresponding error code string rather than a numeric measure.
    """
    baseline_path_internal: list[float | str] = []
    n = require_aligned(growth_baseline, interest_baseline, primary_balance_baseline)
    for t in range(n):
        if t == 0:
            try:
                baseline_path_internal_t = as_measure(xl_sub(xl_div(xl_mul(live_measure(engine_initial_debt_baseline), xl_add(1, xl_div(live_measure(interest_baseline[t]), 100))), xl_add(1, xl_div(live_measure(growth_baseline[t]), 100))), live_measure(primary_balance_baseline[t])))
            except XlError as err:
                baseline_path_internal_t = err.code
            baseline_path_internal.append(baseline_path_internal_t)
        else:
            try:
                baseline_path_internal_t = as_measure(xl_sub(xl_div(xl_mul(live_measure(baseline_path_internal[t - 1]), xl_add(1, xl_div(live_measure(interest_baseline[t]), 100))), xl_add(1, xl_div(live_measure(growth_baseline[t]), 100))), live_measure(primary_balance_baseline[t])))
            except XlError as err:
                baseline_path_internal_t = err.code
            baseline_path_internal.append(baseline_path_internal_t)
    return tuple(baseline_path_internal)

def shocked_path_internal(engine_initial_debt_shocked: float | str, shocked_growth: Sequence[float | str], shocked_interest: Sequence[float | str], shocked_primary_balance: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the shocked debt-to-GDP path for the Engine sheet.

    Project the internal shocked debt-to-GDP series used as the Engine-sheet `shocked_path` named range.

    Args:
        engine_initial_debt_shocked: Initial debt-to-GDP ratio in percent of GDP at the end of the year preceding the first projection year (year 0), used to seed the shocked path.
        shocked_growth: Annual real GDP growth rates in percent per annum over the projection horizon, reflecting any permanent growth shock from the configured shock year onward.
        shocked_interest: Annual real interest rates in percent per annum over the projection horizon, reflecting any permanent interest-rate shock from the configured shock year onward.
        shocked_primary_balance: Annual primary balances in percent of GDP over the projection horizon, with positive values denoting surpluses, reflecting any permanent primary-balance shock from the configured shock year onward.

    Returns:
        Tuple of projected shocked debt-to-GDP ratios for each year of the horizon, in percent of GDP. Entries are numeric where the recursion succeeds, and may be Excel error codes if input values are not numeric.
    """
    shocked_path_internal: list[float | str] = []
    n = require_aligned(shocked_growth, shocked_interest, shocked_primary_balance)
    for t in range(n):
        if t == 0:
            try:
                shocked_path_internal_t = as_measure(xl_sub(xl_div(xl_mul(live_measure(engine_initial_debt_shocked), xl_add(1, xl_div(live_measure(shocked_interest[t]), 100))), xl_add(1, xl_div(live_measure(shocked_growth[t]), 100))), live_measure(shocked_primary_balance[t])))
            except XlError as err:
                shocked_path_internal_t = err.code
            shocked_path_internal.append(shocked_path_internal_t)
        else:
            try:
                shocked_path_internal_t = as_measure(xl_sub(xl_div(xl_mul(live_measure(shocked_path_internal[t - 1]), xl_add(1, xl_div(live_measure(shocked_interest[t]), 100))), xl_add(1, xl_div(live_measure(shocked_growth[t]), 100))), live_measure(shocked_primary_balance[t])))
            except XlError as err:
                shocked_path_internal_t = err.code
            shocked_path_internal.append(shocked_path_internal_t)
    return tuple(shocked_path_internal)

def output_baseline(baseline_path_internal: Sequence[float | str]) -> tuple[float | str, ...]:
    """Converts the internal Engine baseline path to the five-year Outputs baseline series.

    Provides the baseline debt-to-GDP trajectory for projection years 1 through 5 as a bound output series.

    Args:
        baseline_path_internal: Sequence of yearly baseline debt-to-GDP values (Engine!C6:G6) for projection years 1 through 5, where each entry is either a numeric ratio or an Excel error code.

    Returns:
        A tuple of length 5 containing the baseline debt-to-GDP values for each projection year. Each numeric value is returned as a formatted measure; if a source cell contains an Excel error, the corresponding Excel error code is returned in its place.
    """
    n = require_aligned(baseline_path_internal)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(baseline_path_internal[i]))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def output_shocked(shocked_path_internal: Sequence[float | str]) -> tuple[float | str, ...]:
    """Return the shocked debt-to-GDP path from the Engine sheet as an aligned output tuple.

    First-level helper for the `output_shocked` bound series on the Outputs sheet.

    Args:
        shocked_path_internal: Sequence of shocked debt-to-GDP values from the Engine sheet's `shocked_path` range, for projection years 1 through 5; entries may be numeric or Excel error sentinels.

    Returns:
        A tuple of the same length as the input, where each value has been normalized to a measure for downstream use, with Excel error codes preserved when conversion fails.
    """
    n = require_aligned(shocked_path_internal)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(shocked_path_internal[i]))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

def output_delta(output_baseline: Sequence[float | str], output_shocked: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the percentage-point difference between the shocked and baseline debt-to-GDP trajectories.

    Provide the output_delta series for the shocked-minus-baseline comparison.

    Args:
        output_baseline: Baseline debt-to-GDP path as a percent of GDP for years 1 through 5.
        output_shocked: Shocked debt-to-GDP path as a percent of GDP for years 1 through 5.

    Returns:
        Tuple of year-by-year differences, shocked minus baseline, expressed in percentage points; Excel errors propagate as error-code strings.
    """
    n = require_aligned(output_baseline, output_shocked)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_sub(output_shocked[i], output_baseline[i])))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
