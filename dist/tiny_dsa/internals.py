from __future__ import annotations

from .runtime import (
    XlError,
    xl_bool,
    xl_cell,
    xl_compare,
    xl_eval,
    xl_index_ref,
    xl_int,
    xl_match,
    xl_number,
    xl_offset,
    xl_raise,
    xl_range,
)

# --- Formula cell functions ---

def debt_to_gdp_shock_deviation(ctx, time_period):
    """Return the deviation of shocked debt-to-GDP from the baseline.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        The difference between the shocked and baseline debt-to-GDP ratios.

    Note:
        Covers Outputs!B14:F14. Excel: =Engine!{col}20-Engine!{col}6.
"""
    shocked = shocked_debt_to_gdp(ctx, time_period=time_period)
    baseline = baseline_debt_to_gdp(ctx, time_period=time_period)
    return xl_number(shocked) - xl_number(baseline)

def baseline_debt_to_gdp(ctx, time_period):
    """Calculate baseline Debt-to-GDP (%) for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Baseline Debt-to-GDP (%) for the projection year.

    Note:
        Covers Engine!C6:G6. Excel: =prior_debt_to_gdp*(1+r_debt/100)/(1+r_gdp/100)-ps.
        where prior_debt_to_gdp is Inputs!B6 for year 1, previous year's cell for later years,
        r_debt is Inputs!{col}17, r_gdp is Inputs!{col}16, and ps is Inputs!{col}18.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    if time_period == 1:
        prior_debt = xl_number(initial_debt_to_gdp(ctx))
    else:
        prior_debt = xl_number(baseline_debt_to_gdp(ctx, time_period - 1))
    debt_growth_rate = xl_number(xl_cell(ctx, f'Inputs!{column}17'))
    gdp_growth_rate = xl_number(xl_cell(ctx, f'Inputs!{column}16'))
    primary_surplus = xl_number(xl_cell(ctx, f'Inputs!{column}18'))
    one = xl_number(1.0)
    hundred = xl_number(100.0)
    debt_factor_term = xl_number(debt_growth_rate) / hundred
    if debt_factor_term == 0:
        xl_raise(XlError.DIV)
    debt_factor = xl_number(one + debt_factor_term)
    gdp_factor_term = xl_number(gdp_growth_rate) / hundred
    if gdp_factor_term == 0:
        xl_raise(XlError.DIV)
    gdp_factor = xl_number(one + gdp_factor_term)
    numerator = xl_number(prior_debt * debt_factor)
    denominator = xl_number(gdp_factor)
    if denominator == 0:
        xl_raise(XlError.DIV)
    ratio = xl_number(numerator / denominator)
    result = xl_number(ratio - xl_number(primary_surplus))
    return result

def shocked_debt_to_gdp(ctx, time_period):
    """Return the debt-to-GDP ratio under the shock scenario for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        The shocked debt-to-GDP ratio as a float.

    Note:
        Covers Engine!C20:G20. Excel: for time_period=1: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16;
        for time_period>1: =prev_debt*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    if time_period == 1:
        prior_debt = xl_number(initial_debt_to_gdp(ctx))
    else:
        previous_column = column_by_time_period.get(time_period - 1)
        if previous_column is None:
            xl_raise(XlError.VALUE)
        prior_debt = xl_number(shocked_debt_to_gdp(ctx, time_period - 1))
    base_shock_choice = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if base_shock_choice < 1 or base_shock_choice > 3:
        xl_raise(XlError.VALUE)
    if base_shock_choice == 1:
        base_shock_for_numerator = 0.0
        base_shock_for_denominator = xl_number(shock_magnitude_pp(ctx))
    elif base_shock_choice == 2:
        base_shock_for_numerator = xl_number(shock_magnitude_pp(ctx))
        base_shock_for_denominator = 0.0
    else:
        base_shock_for_numerator = 0.0
        base_shock_for_denominator = 0.0
    shock_active_flag = xl_number(shock_active(ctx, time_period=time_period))
    interest_rate_change_numerator = base_shock_for_numerator * shock_active_flag
    interest_rate_change_denominator = base_shock_for_denominator * shock_active_flag
    numerator_input_rate = xl_number(xl_cell(ctx, f'Inputs!{column}17'))
    numerator_growth = xl_number(1.0) + (numerator_input_rate + interest_rate_change_numerator) / 100.0
    denominator_input_rate = xl_number(xl_cell(ctx, f'Inputs!{column}16'))
    denominator_growth = xl_number(1.0) + (denominator_input_rate + interest_rate_change_denominator) / 100.0
    if denominator_growth == 0.0:
        xl_raise(XlError.DIV)
    primary_balance = xl_number(shocked_primary_balance(ctx, time_period=time_period))
    result = prior_debt * numerator_growth / denominator_growth - primary_balance
    return xl_number(result)

def shocked_primary_balance(ctx, time_period):
    """Calculate shocked primary balance as a percent of GDP for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Shocked primary balance as a percent of GDP.

    Note:
        Covers Engine!C16:G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!$B$22,0,0,$B$9)*Engine!{col}10.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    input_value = xl_cell(ctx, f'Inputs!{column}18')
    choose_index = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if xl_compare('<', choose_index, 1) or xl_compare('>', choose_index, 3):
        xl_raise(XlError.VALUE)
    if xl_compare('=', choose_index, 1):
        shock_term = 0.0
    elif xl_compare('=', choose_index, 2):
        shock_term = 0.0
    elif xl_compare('=', choose_index, 3):
        shock_term = shock_magnitude_pp(ctx)
    else:
        xl_raise(XlError.VALUE)
    return xl_number(input_value) + xl_number(shock_term) * xl_number(shock_active(ctx, time_period=time_period))

def shock_active(ctx, time_period):
    """Return 1.0 when the shock is active for the given projection year, 0.0 otherwise.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        1.0 if the shock year is at or before the projection year, else 0.0.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0).
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    engine_column = column_by_time_period.get(time_period)
    if engine_column is None:
        xl_raise(XlError.VALUE)
    projection_year = xl_cell(ctx, f'Engine!{engine_column}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    is_active = xl_compare('>=', projection_year, shock_year)
    return xl_number(1.0 if is_active else 0.0)

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Initial debt-to-GDP ratio from the country profile table.

    Note:
        Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).
"""
    country_table = ('Inputs', 10, 1, 12, 3)
    selected_country = xl_cell(ctx, 'Inputs!B5')
    matched_row = xl_match(selected_country, xl_range(ctx, 'Inputs!A10:Inputs!A12'), 0.0)
    debt_ratio_ref = xl_index_ref(country_table, matched_row, 2.0)
    return xl_offset(ctx, debt_ratio_ref, 0.0, 0.0)

def shock_magnitude_pp(ctx):
    """Look up the shock magnitude in percentage points for the selected shock type.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Shock magnitude from Inputs row 26, offset by the selected shock type index.

    Note:
        Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type_index = xl_number(xl_cell(ctx, 'Inputs!B22'))
    offset_cols = xl_number(shock_type_index) - xl_number(1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, offset_cols, None, None)

# --- Projection public address aliases ---

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt_to_gdp', {'time_period': 1}),
    'Outputs!B13': ('shocked_debt_to_gdp', {'time_period': 1}),
    'Outputs!B14': ('debt_to_gdp_shock_deviation', {'time_period': 1}),
    'Outputs!C12': ('baseline_debt_to_gdp', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp', {'time_period': 2}),
    'Outputs!C14': ('debt_to_gdp_shock_deviation', {'time_period': 2}),
    'Outputs!D12': ('baseline_debt_to_gdp', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp', {'time_period': 3}),
    'Outputs!D14': ('debt_to_gdp_shock_deviation', {'time_period': 3}),
    'Outputs!E12': ('baseline_debt_to_gdp', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp', {'time_period': 4}),
    'Outputs!E14': ('debt_to_gdp_shock_deviation', {'time_period': 4}),
    'Outputs!F12': ('baseline_debt_to_gdp', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp', {'time_period': 5}),
    'Outputs!F14': ('debt_to_gdp_shock_deviation', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'shock_magnitude_pp',
    'Inputs!B6': 'initial_debt_to_gdp',
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
