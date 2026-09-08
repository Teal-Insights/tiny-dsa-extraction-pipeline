"""First-level-dependency internals for the inverted graph."""

from __future__ import annotations

from collections.abc import Sequence

from . import data

from .runtime import XlError, as_measure, live_measure, publish, require_aligned, require_length, xl_add, xl_at, xl_choose, xl_div, xl_ge, xl_match, xl_mul, xl_sub


@publish(
    key=(),
    domain=((),),
)
def initial_debt_resolved(country_profile_names: Sequence[str], country_name: str, country_initial_debt: Sequence[float | str]) -> float | str:
    """Resolve the initial debt-to-GDP ratio for a selected country from the country profile table.

    Look up the initial debt-to-GDP ratio to use as the first-year baseline input.

    Args:
        country_profile_names: Sequence of country names in the profile lookup table, used as the INDEX/MATCH search vector.
        country_name: Name of the country to resolve, matched against the profile names.
        country_initial_debt: Sequence of initial debt-to-GDP ratios aligned with each profile country, from which the matched value is returned.

    Returns:
        The matched initial debt-to-GDP ratio as a numeric measure. If the country is not found or the lookup fails, an Excel error code string is returned.
    """
    try:
        return as_measure(xl_at(country_initial_debt, (xl_match(country_name, country_profile_names, 0)) - 1))
    except XlError as err:
        return err.code

@publish(
    key=(),
    domain=((),),
)
def engine_initial_debt_baseline(initial_debt_resolved: float | str) -> float | str:
    """Return the Year-0 debt stock anchor for the baseline recursion on the Engine sheet.

    Normalizes the resolved initial debt-to-GDP ratio so the Engine baseline path starts from a valid Year-0 anchor, while preserving spreadsheet errors for downstream propagation.

    Args:
        initial_debt_resolved: Resolved general-government debt-to-GDP ratio at the end of Year 0, expressed as a percent of GDP and sourced through the Inputs-sheet country lookup. Serves as the Year-0 debt stock anchor for the baseline recursion on the Engine sheet; it may also carry a spreadsheet error-code string when resolution fails.

    Returns:
        The normalized Year-0 debt-to-GDP ratio as a float when resolution succeeds; otherwise, the originating Excel error code as a string.
    """
    try:
        return as_measure(initial_debt_resolved)
    except XlError as err:
        return err.code

@publish(
    key=(),
    domain=((),),
)
def engine_initial_debt_shocked(initial_debt_resolved: float | str) -> float | str:
    """Resolve the Year-0 debt stock anchor that seeds the shocked Engine recursion.

    Return the initial debt-to-GDP ratio at end of year 0 as a measure for use in the shocked recursion on the Engine sheet.

    Args:
        initial_debt_resolved: Resolved Year-0 debt stock anchor for the shocked recursion on the Engine sheet, expressed as a percentage of GDP.

    Returns:
        The Year-0 debt stock anchor as a numeric percentage-of-GDP measure when resolution succeeds, or the corresponding error code string when resolution fails.
    """
    try:
        return as_measure(initial_debt_resolved)
    except XlError as err:
        return err.code

@publish(
    key=(),
    domain=((),),
)
def shock_magnitude_resolved(shock_type: int | str, shock_magnitudes: Sequence[float | str]) -> float | str:
    """Resolve the configured shock magnitude from the shock table.

    Select the magnitude corresponding to the selected shock type, mirroring the OFFSET lookup on the shock table.

    Args:
        shock_type: Selected shock type as a one-based index: 1 for real GDP growth, 2 for the real interest rate, 3 for the primary balance. An Excel reference or string representing one of these values is also accepted.
        shock_magnitudes: The three shock magnitudes from the shock table in type order: growth, interest, and primary balance. The magnitude at position shock_type - 1 is resolved.

    Returns:
        The resolved shock magnitude for the selected shock type, in percentage points. If the shock type is out of range or an Excel error occurs, returns the corresponding error code.
    """
    try:
        return as_measure(xl_at(shock_magnitudes, (xl_sub(shock_type, 1))))
    except XlError as err:
        return err.code

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def shock_active(engine_year_labels: Sequence[int | str], shock_year: int | str) -> tuple[int | str, ...]:
    """Return shock activation flags (1 when a projection year is at or after shock_year, else 0) for each engine year.

    Provide the year-by-year shock-active indicator used to apply the configured shock from the shock year onward.

    Args:
        engine_year_labels: Projection year labels for the Engine sheet, one per year in the five-year horizon. Each label may be an integer or a string and must be dense over the producer's domain.
        shock_year: User-selected first projection year in which the shock takes effect, corresponding to Inputs!B21.

    Returns:
        Tuple of activation flags aligned with engine_year_labels: 1 when the year label is greater than or equal to shock_year, otherwise 0. If a comparison raises an Excel error, the corresponding element is an error-code string instead.
    """
    n = require_aligned(engine_year_labels)
    out: list[int | str] = []
    for i in range(n):
        try:
            out.append(as_measure((1 if xl_ge(engine_year_labels[i], shock_year) else 0), 'int'))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def shocked_growth(growth_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Shocked real GDP growth path after applying the selected shock magnitude.

    Computes the real GDP growth rates used in the shocked debt path when a growth shock is active.

    Args:
        growth_baseline: Baseline real GDP growth rates, percent per annum, for each year of the horizon.
        shock_type: Shock-type selector: 1 for real GDP growth, 2 for the real interest rate, or 3 for the primary balance; only a value of 1 applies the resolved shock to growth.
        shock_magnitude_resolved: Shock magnitude in percentage points for the selected shock type, from the resolved shock-magnitude cell.
        shock_active: Annual activation indicators (1 if the shock applies that year, 0 otherwise), marking the years from the shock year onward.

    Returns:
        Tuple of shocked real GDP growth rates, percent per annum, for the same years as the inputs; each element equals baseline growth plus the resolved growth shock when active, or an XLS error code string when the underlying evaluation fails.
    """
    n = require_aligned(growth_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(growth_baseline[i], xl_mul(xl_choose(shock_type, shock_magnitude_resolved, 0, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def shocked_interest(interest_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Compute the shocked real interest rate path by overlaying the resolved interest-rate shock magnitude on the baseline interest path when the shock is active.

    Produce the shocked real interest rate row used in the debt-dynamics trajectory.

    Args:
        interest_baseline: Baseline real interest rates, in percent per annum, for the years of the scenario.
        shock_type: Selected shock type (1=growth, 2=real interest rate, 3=primary balance); only type 2 alters the interest path.
        shock_magnitude_resolved: Resolved shock magnitude, in percentage points, applied when the interest-rate shock is selected.
        shock_active: Per-year indicators (1 if the shock is active in that year, 0 otherwise).

    Returns:
        Tuple of shocked real interest rates, in percent per annum, aligned with the input series; Excel-style error codes are preserved when conversion fails.
    """
    n = require_aligned(interest_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(interest_baseline[i], xl_mul(xl_choose(shock_type, 0, shock_magnitude_resolved, 0), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def shocked_primary_balance(primary_balance_baseline: Sequence[float | str], shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: Sequence[int | str]) -> tuple[float | str, ...]:
    """Compute the shocked primary-balance path by applying the configured primary-balance shock to the baseline primary balance.

    Return the annual primary-balance series under the active shock, preserving baseline values when the shock type is not primary-balance or the shock is not yet active.

    Args:
        primary_balance_baseline: Annual baseline primary balance, expressed as a percent of GDP with positive values denoting a surplus.
        shock_type: Shock-type selector (1 = real GDP growth, 2 = real interest rate, 3 = primary balance). This function applies the magnitude only when shock_type is 3.
        shock_magnitude_resolved: Resolved magnitude for the selected shock type, in percentage points; for a primary-balance shock, negative values reduce the primary balance. Ignored unless shock_type is 3.
        shock_active: Per-year 0/1 indicator marking years from the shock year onward in which the shock is active.

    Returns:
        A tuple of primary-balance values for each year in the aligned domain, as percent of GDP (positive = surplus). Each value equals baseline plus the resolved magnitude when shock_active is 1 and shock_type is 3; otherwise it equals baseline. Underlying spreadsheet formula error codes are represented as string entries.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_add(primary_balance_baseline[i], xl_mul(xl_choose(shock_type, 0, 0, shock_magnitude_resolved), shock_active[i]))))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def baseline_path_internal(engine_initial_debt_baseline: float | str, growth_baseline: Sequence[float | str], interest_baseline: Sequence[float | str], primary_balance_baseline: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the internal baseline debt-to-GDP path over the projection horizon.

    Generate the recursive baseline path used for the Engine sheet's baseline_path range from the initial debt ratio and the baseline growth, interest, and primary-balance vectors.

    Args:
        engine_initial_debt_baseline: Debt-to-GDP ratio at the end of the year preceding the projection horizon, in percent of GDP. It seeds the recursion as the debt stock carried into the first projected year.
        growth_baseline: Sequence of annual real GDP growth rates, in percent per annum, spanning the projection horizon. The first element is applied in the first projected year; the sequence must be dense over the producer's domain.
        interest_baseline: Sequence of annual real interest rates, in percent per annum, spanning the projection horizon. The first element is applied in the first projected year; the sequence must be dense over the producer's domain.
        primary_balance_baseline: Sequence of annual primary balances as a percent of GDP, with positive values denoting a surplus, spanning the projection horizon. The first element is applied in the first projected year; the sequence must be dense over the producer's domain.

    Returns:
        Tuple with one entry per projected year. Each entry is the baseline debt-to-GDP ratio in percent of GDP after applying the standard real-term snowball recursion: next debt equals previous debt multiplied by (1 + interest/100) / (1 + growth/100) minus the primary balance for that year.
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

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def shocked_path_internal(engine_initial_debt_shocked: float | str, shocked_growth: Sequence[float | str], shocked_interest: Sequence[float | str], shocked_primary_balance: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the internal shocked debt-to-GDP path.

    First-level helper that recurses the debt-dynamics identity using the shocked growth, interest, and primary-balance series to produce the Engine sheet's shocked_path values.

    Args:
        engine_initial_debt_shocked: Initial debt-to-GDP ratio for the shocked run, expressed as a percentage of GDP. This is the starting debt stock from which the shocked path recursion is calculated.
        shocked_growth: Real GDP growth rates after applying the configured shock, in percent per annum. The series must be dense over the producer's domain and represents the growth input for each year of the shocked path.
        shocked_interest: Real interest rates after applying the configured shock, in percent per annum. The series must be dense over the producer's domain and represents the effective rate on outstanding government debt for each year of the shocked path.
        shocked_primary_balance: Primary balances after applying the configured shock, expressed as a percentage of GDP with positive values denoting a surplus. The series must be dense over the producer's domain and provides the fiscal-stance input for each year of the shocked path.

    Returns:
        A tuple with one entry per year in the aligned series, giving the shocked debt-to-GDP ratio for that year as a percentage of GDP. This mirrors the Engine sheet's shocked_path named range. If an evaluation error occurs for a year, the corresponding entry is the spreadsheet error-code string.
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

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def output_baseline(baseline_path_internal: Sequence[float | str]) -> tuple[float | str, ...]:
    """First-level helper for bound series `output_baseline`.

    Resolve the internal engine-sheet baseline debt path into the public Outputs-sheet `output_baseline` series.

    Args:
        baseline_path_internal: The internal baseline debt-to-GDP path for projection years 1 through 5. Must be dense over the producer's domain; holed series shorter than the public domain are not accepted.

    Returns:
        A tuple of resolved debt-to-GDP values for projection years 1 through 5, with error codes preserved for any year whose value cannot be evaluated.
    """
    n = require_aligned(baseline_path_internal)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(baseline_path_internal[i]))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def output_shocked(shocked_path_internal: Sequence[float | str]) -> tuple[float | str, ...]:
    """Format the Engine-sheet shocked debt-to-GDP path for the `output_shocked` bound series.

    Converts the internal shocked-path calculation into the stable Outputs-sheet series for projection years 1 through 5.

    Args:
        shocked_path_internal: Sequence of shocked debt-to-GDP values (percent of GDP) for projection years 1 through 5 from the Engine-sheet `shocked_path` range. Must be dense over the producer's `__domain__`; holed series are shorter than the public domain.

    Returns:
        Tuple of shocked debt-to-GDP values (percent of GDP) aligned to the public `output_shocked` domain, one entry per projection year; cells that cannot be evaluated are represented by their Excel error-code strings.
    """
    n = require_aligned(shocked_path_internal)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(shocked_path_internal[i]))
        except XlError as err:
            out.append(err.code)
    return tuple(out)

@publish(
    key=('TIME_PERIOD',),
    domain=data.TIME_PERIOD_DOMAIN,
)
def output_delta(output_baseline: Sequence[float | str], output_shocked: Sequence[float | str]) -> tuple[float | str, ...]:
    """Compute the shocked-minus-baseline debt-to-GDP difference in percentage points.

    Return the output_delta bound series: the difference between the shocked and baseline paths, expressed in percentage points and aligned by output year.

    Args:
        output_baseline: Baseline debt-to-GDP path over the output horizon, in percent of GDP, as a dense sequence of floats or spreadsheet error-code strings.
        output_shocked: Shocked debt-to-GDP path over the output horizon, in percent of GDP, as a dense sequence of floats or spreadsheet error-code strings.

    Returns:
        A tuple of the same length as the input sequences, with each element equal to the shocked value minus the baseline value for that year in percentage points. Elements are floats when both inputs are numeric; otherwise an error-code string is propagated for that year.
    """
    n = require_aligned(output_baseline, output_shocked)
    out: list[float | str] = []
    for i in range(n):
        try:
            out.append(as_measure(xl_sub(output_shocked[i], output_baseline[i])))
        except XlError as err:
            out.append(err.code)
    return tuple(out)
