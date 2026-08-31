from __future__ import annotations

from ._readers import (
    read_country_name,
    read_country_profile_names_range,
    read_engine_year_labels,
    read_growth_baseline,
    read_interest_baseline,
    read_primary_balance_baseline,
    read_shock_type,
    read_shock_year,
)
from .runtime import CellValue, EvalContext, XlError, xl_bool, xl_compare, xl_eval, xl_index_ref, xl_int, xl_match, xl_memoize, xl_number, xl_offset, xl_raise, xl_range

# --- Formula cell functions ---

@xl_memoize
def output_delta(ctx: EvalContext, time_period: int) -> float:
    """Computes the difference between shocked and baseline output for a given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection time period (1-based index into projection columns; observed range 1..5).

Returns:
    Numeric difference (shocked minus baseline) for the specified time period.

Note:
    Covers Outputs!B14:F14. Excel: =Outputs!B13-Outputs!B12.
"""
    shocked_output = output_shocked(ctx, time_period=time_period)
    baseline_output = output_baseline(ctx, time_period=time_period)
    return xl_number(shocked_output) - xl_number(baseline_output)


@xl_memoize
def output_baseline(ctx: EvalContext, time_period: int) -> float:
    """Computes the output baseline value for a projection year.

Args:
    ctx (EvalContext): The workbook evaluation context.
    time_period (int): The projection year. Observed values range from 1 through 5.

Returns:
    float: The output baseline value for the requested projection year.

Note:
    Covers Outputs!B12:F12. Excel: =Engine!C6.
"""
    if time_period in {1, 2, 3, 4}:
        return baseline_path_internal(ctx, time_period=time_period)
    prior_year_baseline_debt = baseline_path_internal(ctx, time_period=4)
    baseline_interest_rate = read_interest_baseline(ctx, time_period=5)
    baseline_real_gdp_growth_rate = read_growth_baseline(ctx, time_period=5)
    baseline_primary_balance = read_primary_balance_baseline(ctx, time_period=5)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(prior_year_baseline_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(baseline_interest_rate), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(baseline_real_gdp_growth_rate), xl_number(100.0)))))) - xl_number(baseline_primary_balance)


@xl_memoize
def output_shocked(ctx: EvalContext, time_period: int) -> float:
    """Return the shocked output for a given projection year.

Args:
    ctx: Evaluation context.
    time_period: Projection year; valid values are 1 through 5.

Returns:
    The shocked output value for the requested projection year.

Note:
    Covers Outputs!B13:F13. Excel: =Engine!C20.
"""
    if time_period in {1, 2, 3, 4}:
        return shocked_path_internal(ctx, time_period=time_period)
    shocked_path_prior = shocked_path_internal(ctx, time_period=4)
    shocked_interest_rate = shocked_interest(ctx, time_period=5)
    baseline_growth_rate = read_growth_baseline(ctx, time_period=5)
    shock_type_code = read_shock_type(ctx)
    shock_active_flag = shock_active(ctx, time_period=5)
    shocked_primary_balance_value = shocked_primary_balance(ctx, time_period=5)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(shocked_path_prior) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(shocked_interest_rate), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(baseline_growth_rate) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_index := xl_int(shock_type_code)) < 1 or shock_type_index > 3 else shock_magnitude_resolved(ctx) if shock_type_index == 1 else 0.0 if shock_type_index == 2 else 0.0 if shock_type_index == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_flag))), xl_number(100.0)))))) - xl_number(shocked_primary_balance_value)


@xl_memoize
def shocked_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Compute the internal shocked debt-to-GDP path for a projection time period.

Args:
    ctx (EvalContext): Evaluation context providing workbook state and dependency helpers.
    time_period (int): Projection period (1-based). In this cluster the observed range is 1 through 4.

Returns:
    float: The shocked debt-to-GDP ratio for the requested period, expressed as a percentage of GDP.

Note:
    Covers Engine!C20:F20. Excel: =Inputs!B6*(1+Engine!C15/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16.
"""
    if time_period == 1:
        initial_debt = initial_debt_resolved(ctx)
        initial_shocked_interest_pct = shocked_interest(ctx, time_period=1)
        initial_growth_baseline_pct = read_growth_baseline(ctx, time_period=1)
        initial_shock_type_code = read_shock_type(ctx)
        initial_shock_active_flag = shock_active(ctx, time_period=1)
        initial_primary_balance = shocked_primary_balance(ctx, time_period=1)
        return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(initial_shocked_interest_pct), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_growth_baseline_pct) + xl_number(xl_number(xl_raise(XlError.VALUE) if (initial_shock_type_index := xl_int(initial_shock_type_code)) < 1 or initial_shock_type_index > 3 else shock_magnitude_resolved(ctx) if initial_shock_type_index == 1 else 0.0 if initial_shock_type_index == 2 else 0.0 if initial_shock_type_index == 3 else xl_raise(XlError.VALUE)) * xl_number(initial_shock_active_flag))), xl_number(100.0)))))) - xl_number(initial_primary_balance)
    previous_period_debt = shocked_path_internal(ctx, time_period=time_period - 1)
    shocked_interest_pct = shocked_interest(ctx, time_period=time_period)
    growth_baseline_pct = read_growth_baseline(ctx, time_period=time_period)
    shock_type_code = read_shock_type(ctx)
    shock_active_flag = shock_active(ctx, time_period=time_period)
    primary_balance = shocked_primary_balance(ctx, time_period=time_period)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(previous_period_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(shocked_interest_pct), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(growth_baseline_pct) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_index := xl_int(shock_type_code)) < 1 or shock_type_index > 3 else shock_magnitude_resolved(ctx) if shock_type_index == 1 else 0.0 if shock_type_index == 2 else 0.0 if shock_type_index == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_flag))), xl_number(100.0)))))) - xl_number(primary_balance)


@xl_memoize
def baseline_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Compute the baseline debt-to-GDP path for a given projection year.

Args:
    ctx (EvalContext): Workbook evaluation context used to resolve cell reads.
    time_period (int): Projection year (1-based). Observed range in this cluster is 1 through 4; the recursion reads time_period - 1 when time_period > 1.

Returns:
    float: Baseline debt-to-GDP value (in percent of GDP) for the requested time_period.

Note:
    Covers Engine!C6:F6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
"""
    if time_period == 1:
        initial_debt = initial_debt_resolved(ctx)
        initial_interest_rate_baseline = read_interest_baseline(ctx, time_period=1)
        initial_growth_rate_baseline = read_growth_baseline(ctx, time_period=1)
        initial_primary_balance_baseline = read_primary_balance_baseline(ctx, time_period=1)
        return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(initial_interest_rate_baseline), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(initial_growth_rate_baseline), xl_number(100.0)))))) - xl_number(initial_primary_balance_baseline)
    previous_period_debt = baseline_path_internal(ctx, time_period=time_period - 1)
    current_interest_rate_baseline = read_interest_baseline(ctx, time_period=time_period)
    current_growth_rate_baseline = read_growth_baseline(ctx, time_period=time_period)
    current_primary_balance_baseline = read_primary_balance_baseline(ctx, time_period=time_period)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(previous_period_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(current_interest_rate_baseline), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(current_growth_rate_baseline), xl_number(100.0)))))) - xl_number(current_primary_balance_baseline)


@xl_memoize
def shocked_interest(ctx: EvalContext, time_period: int) -> float:
    """Calculate the shocked real interest rate for a projection year.

Args:
    ctx (EvalContext): Workbook evaluation context used to read inputs and dependency cells.
    time_period (int): Projection year, observed in the range 1 through 5.

Returns:
    float: The shocked real interest rate for the given projection year, in percent per annum.

Note:
    Covers Engine!C15:G15. Excel: =Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10.
"""
    baseline_interest_rate = read_interest_baseline(ctx, time_period=time_period)
    selected_shock_type = read_shock_type(ctx)
    shock_active_for_period = shock_active(ctx, time_period=time_period)
    return xl_number(baseline_interest_rate) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_code := xl_int(selected_shock_type)) < 1 or shock_type_code > 3 else 0.0 if shock_type_code == 1 else shock_magnitude_resolved(ctx) if shock_type_code == 2 else 0.0 if shock_type_code == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_for_period))


@xl_memoize
def shocked_primary_balance(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked primary balance for a given projection year.

Args:
    ctx: Evaluation context providing access to workbook cells and helpers.
    time_period: Projection year (1-5) for which to compute the balance.

Returns:
    float: The shocked primary balance as a percentage of GDP.

Note:
    Covers Engine!C16:G16. Excel: =Inputs!C18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!C10.
"""
    baseline_primary_balance = read_primary_balance_baseline(ctx, time_period=time_period)
    shock_type = read_shock_type(ctx)
    shock_active_flag = shock_active(ctx, time_period=time_period)
    return xl_number(baseline_primary_balance) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_index := xl_int(shock_type)) < 1 or shock_type_index > 3 else 0.0 if shock_type_index == 1 else 0.0 if shock_type_index == 2 else shock_magnitude_resolved(ctx) if shock_type_index == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_flag))


@xl_memoize
def shock_active(ctx: EvalContext, time_period: int) -> float:
    """Determine whether the shock is active for a given time period.

Args:
    ctx (EvalContext): Evaluation context.
    time_period (int): One-based column index of the time period. Observed
        values in the cluster are 1 through 5, mapping to engine columns
        C through G.

Returns:
    float: 1.0 if the shock is active, 0.0 otherwise.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!C5>=Inputs!B21,1,0).
"""
    engine_projection_year = read_engine_year_labels(ctx, time_period=time_period)
    shock_year = read_shock_year(ctx)
    return 1.0 if (is_shock_active := xl_bool(xl_compare('>=', engine_projection_year, shock_year))) else 0.0


@xl_memoize
def initial_debt_resolved(ctx: EvalContext) -> CellValue:
    """Resolve the initial debt-to-GDP ratio for the currently selected country.

Args:
    ctx (EvalContext): The workbook evaluation context providing access to
        bound cells and ranges.

Returns:
    CellValue: The initial debt-to-GDP ratio (as a percentage of GDP) for the
        selected country, or an Excel error value if the lookup fails.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:C12,MATCH(Inputs!B5,Inputs!A10:A12,0),2).
"""
    selected_country_name = read_country_name(ctx)
    country_profile_names = read_country_profile_names_range(ctx)
    country_row_index = xl_match(selected_country_name, country_profile_names, 0.0)
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), country_row_index, 2.0), 0.0, 0.0)


@xl_memoize
def shock_magnitude_resolved(ctx: EvalContext) -> CellValue:
    """Resolve the configured shock magnitude for the selected shock type.

Args:
    ctx (EvalContext): The workbook evaluation context used to read cell values.

Returns:
    CellValue: The shock magnitude value from the Inputs sheet, resolved for the current shock type.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type = read_shock_type(ctx)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_number(shock_type) - xl_number(1.0), None, None)


# --- Unrefactored formula cells ---

def cell_engine_b20(ctx):
    return initial_debt_resolved(ctx)


def cell_engine_b6(ctx):
    return initial_debt_resolved(ctx)


# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Engine!C10': ('shock_active', {'time_period': 1}),
    'Engine!C15': ('shocked_interest', {'time_period': 1}),
    'Engine!C16': ('shocked_primary_balance', {'time_period': 1}),
    'Engine!C20': ('shocked_path_internal', {'time_period': 1}),
    'Engine!C6': ('baseline_path_internal', {'time_period': 1}),
    'Engine!D10': ('shock_active', {'time_period': 2}),
    'Engine!D15': ('shocked_interest', {'time_period': 2}),
    'Engine!D16': ('shocked_primary_balance', {'time_period': 2}),
    'Engine!D20': ('shocked_path_internal', {'time_period': 2}),
    'Engine!D6': ('baseline_path_internal', {'time_period': 2}),
    'Engine!E10': ('shock_active', {'time_period': 3}),
    'Engine!E15': ('shocked_interest', {'time_period': 3}),
    'Engine!E16': ('shocked_primary_balance', {'time_period': 3}),
    'Engine!E20': ('shocked_path_internal', {'time_period': 3}),
    'Engine!E6': ('baseline_path_internal', {'time_period': 3}),
    'Engine!F10': ('shock_active', {'time_period': 4}),
    'Engine!F15': ('shocked_interest', {'time_period': 4}),
    'Engine!F16': ('shocked_primary_balance', {'time_period': 4}),
    'Engine!F20': ('shocked_path_internal', {'time_period': 4}),
    'Engine!F6': ('baseline_path_internal', {'time_period': 4}),
    'Engine!G10': ('shock_active', {'time_period': 5}),
    'Engine!G15': ('shocked_interest', {'time_period': 5}),
    'Engine!G16': ('shocked_primary_balance', {'time_period': 5}),
    'Outputs!B12': ('output_baseline', {'time_period': 1}),
    'Outputs!B13': ('output_shocked', {'time_period': 1}),
    'Outputs!B14': ('output_delta', {'time_period': 1}),
    'Outputs!C12': ('output_baseline', {'time_period': 2}),
    'Outputs!C13': ('output_shocked', {'time_period': 2}),
    'Outputs!C14': ('output_delta', {'time_period': 2}),
    'Outputs!D12': ('output_baseline', {'time_period': 3}),
    'Outputs!D13': ('output_shocked', {'time_period': 3}),
    'Outputs!D14': ('output_delta', {'time_period': 3}),
    'Outputs!E12': ('output_baseline', {'time_period': 4}),
    'Outputs!E13': ('output_shocked', {'time_period': 4}),
    'Outputs!E14': ('output_delta', {'time_period': 4}),
    'Outputs!F12': ('output_baseline', {'time_period': 5}),
    'Outputs!F13': ('output_shocked', {'time_period': 5}),
    'Outputs!F14': ('output_delta', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'shock_magnitude_resolved',
    'Inputs!B6': 'initial_debt_resolved',
}

def _address_to_func_name(address):
    name = []
    prev_underscore = False
    for ch in address.lower():
        if ch == "'":
            continue
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            name.append(ch)
            prev_underscore = False
        else:
            if not prev_underscore:
                name.append("_")
                prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{base}"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    dispatch = _ADDRESS_DISPATCH.get(address)
    if dispatch is not None:
        helper_name, key_kwargs = dispatch
        helper = globals()[helper_name]

        def _bound(ctx, _helper=helper, _key_kwargs=key_kwargs):
            return _helper(ctx, **_key_kwargs)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    symbol_name = _SYMBOL_DISPATCH.get(address)
    if symbol_name is not None:
        helper = globals()[symbol_name]

        def _bound(ctx, _helper=helper):
            return _helper(ctx)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
