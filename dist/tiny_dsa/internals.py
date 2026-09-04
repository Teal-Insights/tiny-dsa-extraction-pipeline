"""First-level-dependency internals for the inverted graph."""

from __future__ import annotations

from collections.abc import Sequence

from . import data

from .runtime import XlError, as_measure, live_measure, require_aligned, xl_add, xl_at, xl_choose, xl_div, xl_ge, xl_match, xl_mul, xl_sub


def initial_debt_resolved(country_profile_names: Sequence[str], country_name: str, country_initial_debt: Sequence[float | str]) -> float | str:
    """Resolve the initial debt-to-GDP ratio from the country profile via an INDEX/MATCH lookup.

    Provides the initial debt-to-GDP ratio for a selected country by matching its name against the country profile lookup table.

    Args:
        country_profile_names: Sequence of country names in the country profile lookup table, used as the lookup vector for the match.
        country_name: Name of the selected country, to be matched exactly against country_profile_names.
        country_initial_debt: Sequence of initial debt-to-GDP values aligned positionally with country_profile_names; may include numeric values or error codes.

    Returns:
        The resolved initial debt-to-GDP ratio as a number, or an Excel error code as a string if the lookup fails.
    """
    try:
        return as_measure(xl_at(country_initial_debt, (xl_match(country_name, country_profile_names, 0)) - 1))
    except XlError as err:
        return err.code
setattr(initial_debt_resolved, '__key__', ())
setattr(initial_debt_resolved, '__domain__', ((),))

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
setattr(engine_initial_debt_baseline, '__key__', ())
setattr(engine_initial_debt_baseline, '__domain__', ((),))

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
setattr(engine_initial_debt_shocked, '__key__', ())
setattr(engine_initial_debt_shocked, '__domain__', ((),))

def shock_magnitude_resolved(shock_type: int | str, shock_magnitudes: Sequence[float | str]) -> float | str:
    """Resolve the shock magnitude for the selected shock type from the shock table.

    Return the configured magnitude for the active shock type, equivalent to an OFFSET lookup on the shock table.

    Args:
        shock_type: Integer or Excel-compatible identifier of the shock type (1 for growth, 2 for interest rate, 3 for primary balance); selects the magnitude by its 1-based position in the shock table.
        shock_magnitudes: Sequence of three shock magnitudes in the standard shock table order (growth, interest rate, primary balance).

    Returns:
        The resolved shock magnitude for the selected shock type; if the lookup is invalid, an Excel error code is returned.
    """
    try:
        return as_measure(xl_at(shock_magnitudes, (xl_sub(shock_type, 1))))
    except XlError as err:
        return err.code
setattr(shock_magnitude_resolved, '__key__', ())
setattr(shock_magnitude_resolved, '__domain__', ((),))

def shock_active(engine_year_labels: Sequence[int | str], shock_year: int | str) -> tuple[int | str, ...]:
    """Return a tuple of shock-activation flags (1 or 0) for the supplied projection year labels versus the shock start year.

    Indicate, for each projection year, whether the shock is active (year >= shock_year).

    Args:
        engine_year_labels: Projection-year labels covering the forecast horizon (typically years 1 through 5). Each label is compared with shock_year to decide whether the shock applies in that year.
        shock_year: First projection year in which the shock begins. The returned flag is 1 for every projection year greater than or equal to shock_year, and 0 for earlier years.

    Returns:
        A tuple with one element per projection year label, in the same order. Each element is 1 when the corresponding year is on or after shock_year, and 0 otherwise. If a year label is not comparable to shock_year as an Excel-compatible value, the element is an error-code string.
    """
    n = require_aligned(engine_year_labels)
    out: list[int | str] = []
    for i in range(n):
        try:
            out.append(as_measure((1 if xl_ge(engine_year_labels[i], shock_year) else 0), 'int'))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
setattr(shock_active, '__key__', ('TIME_PERIOD',))
setattr(shock_active, '__domain__', data.TIME_PERIOD_DOMAIN)

def shocked_growth(growth_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Build the shocked real GDP growth path by adding the resolved growth-shock magnitude to baseline growth in active shock years when the selected shock type is growth.

    Produces the shock-adjusted real GDP growth series used to compute the shocked debt trajectory.

    Args:
        growth_baseline: Baseline real GDP growth rates in percent per annum for each year of the five-year horizon.
        shock_type: Numeric code identifying the shocked parameter: 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance.
        shock_magnitude_resolved: Shock magnitude in percentage points associated with the selected shock type; this value is applied only when shock_type is 1.
        shock_active: Sequence of per-year indicators, 1 when the shock applies in that year and 0 otherwise, typically 1 from the configured shock year through the end of the horizon.

    Returns:
        Tuple of shocked real GDP growth rates in percent per annum. When the shock type selects growth, each baseline growth rate in an active year is adjusted by shock_magnitude_resolved; otherwise the baseline growth rates are returned unchanged.
    """
    n = require_aligned(growth_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(growth_baseline[i], xl_mul(xl_choose(shock_type, shock_magnitude_resolved, 0, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
setattr(shocked_growth, '__key__', ('TIME_PERIOD',))
setattr(shocked_growth, '__domain__', data.TIME_PERIOD_DOMAIN)

def shocked_interest(interest_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Compute the shocked real interest rate path after applying the selected shock magnitude.

    Produces the real interest rate series used for the shocked debt-to-GDP recursion when the configured shock targets the interest rate.

    Args:
        interest_baseline: Baseline effective real interest rate on outstanding general-government debt for each year of the five-year horizon, in percent per annum.
        shock_type: Integer selecting the parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, or 3 for the primary balance. Only shock_type 2 applies the interest-rate shock; other values leave the interest rate at its baseline path.
        shock_magnitude_resolved: Shock magnitude in percentage points for the selected shock type, resolved from the shock table. It is applied to the interest rate only when shock_type is 2.
        shock_active: Year-by-year indicator equal to 1 from the configured shock year through the end of the horizon and 0 before the shock year, determining when the shock is active.

    Returns:
        Tuple of shocked real interest rates in percent per annum for each year, aligned with the baseline interest path. Values from the shock year onward include the shock adjustment; earlier years match the baseline. When the selected shock is not an interest-rate shock, the baseline interest rates are returned unchanged.
    """
    n = require_aligned(interest_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(interest_baseline[i], xl_mul(xl_choose(shock_type, 0, shock_magnitude_resolved, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
setattr(shocked_interest, '__key__', ('TIME_PERIOD',))
setattr(shocked_interest, '__domain__', data.TIME_PERIOD_DOMAIN)

def shocked_primary_balance(primary_balance_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Shocked primary balance path after applying the selected shock magnitude.

    Determine the year-by-year primary balance values used in the shocked debt trajectory.

    Args:
        primary_balance_baseline: Baseline primary balance path, in percent of GDP; positive values denote a surplus. Length must align with shock_active.
        shock_type: Selected shock-type code: 1 for growth, 2 for interest, 3 for primary balance. The resolved magnitude is applied to the primary balance only when shock_type equals 3.
        shock_magnitude_resolved: Shock magnitude in percentage points, looked up from the shock table for the active shock type. It is used as the additive increment to the baseline primary balance in the primary-balance shock case.
        shock_active: Per-year indicator sequence (0 or 1) marking the years during which the shock is active, i.e. from the shock year through the horizon; controls when the magnitude is applied.

    Returns:
        Shocked primary balance path as a tuple of numeric values or Excel error-code strings, one entry per year, matching the length of the baseline sequence.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(primary_balance_baseline[i], xl_mul(xl_choose(shock_type, 0, 0, shock_magnitude_resolved), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
setattr(shocked_primary_balance, '__key__', ('TIME_PERIOD',))
setattr(shocked_primary_balance, '__domain__', data.TIME_PERIOD_DOMAIN)

def baseline_path_internal(engine_initial_debt_baseline: float | str, growth_baseline: Sequence[float | str], interest_baseline: Sequence[float | str], primary_balance_baseline: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the annual baseline debt-to-GDP path projected on the Engine sheet.

    First-level helper that implements the real-terms debt-dynamics recursion for the internal baseline_path range.

    Args:
        engine_initial_debt_baseline: Initial general-government debt-to-GDP ratio at end of year 0, expressed as a percentage of GDP.
        growth_baseline: Real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Real interest rates on outstanding general-government debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Primary balances for years 1 through 5, expressed as a percentage of GDP; positive values denote surpluses.

    Returns:
        A tuple of debt-to-GDP ratios for years 1 through 5. If a year's formula evaluates to an Excel error, that element is the corresponding error code string.
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
setattr(baseline_path_internal, '__key__', ('TIME_PERIOD',))
setattr(baseline_path_internal, '__domain__', data.TIME_PERIOD_DOMAIN)

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
setattr(shocked_path_internal, '__key__', ('TIME_PERIOD',))
setattr(shocked_path_internal, '__domain__', data.TIME_PERIOD_DOMAIN)

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
setattr(output_baseline, '__key__', ('TIME_PERIOD',))
setattr(output_baseline, '__domain__', data.TIME_PERIOD_DOMAIN)

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
setattr(output_shocked, '__key__', ('TIME_PERIOD',))
setattr(output_shocked, '__domain__', data.TIME_PERIOD_DOMAIN)

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
setattr(output_delta, '__key__', ('TIME_PERIOD',))
setattr(output_delta, '__domain__', data.TIME_PERIOD_DOMAIN)
