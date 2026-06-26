from __future__ import annotations

from .runtime import (
    XlError,
    np,
    to_bool,
    to_int,
    xl_add,
    xl_cell,
    xl_div,
    xl_eval,
    xl_ge,
    xl_index_ref,
    xl_match,
    xl_mul,
    xl_offset,
    xl_sub,
)

# --- Formula cell functions ---

def debt_to_gdp_percent_shock_impact(ctx, time_period):
    """Return the shocked debt-to-GDP percentage less the baseline percentage.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection time period, where 1 maps to Engine column C through 5 maps to Engine column G.

    Returns:
        Difference between shocked and baseline debt-to-GDP percentages for the time period.

    Note:
        Covers Outputs!B14:F14. Excel: =Engine!{col}20-Engine!{col}6.
"""
    shocked_debt_to_gdp = shocked_debt_to_gdp_percent(ctx, time_period=time_period)
    baseline_debt_to_gdp = baseline_debt_to_gdp_percent(ctx, time_period=time_period)
    return xl_sub(shocked_debt_to_gdp, baseline_debt_to_gdp)

def baseline_debt_to_gdp_percent(ctx, time_period):
    """Return the baseline debt-to-GDP percentage for a projection period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection period from 1 through 5.

    Returns:
        Baseline debt-to-GDP percentage for the requested projection period.

    Note:
        Covers Engine!C6:G6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18 for Engine!C6; =Engine!{previous_col}6*(1+Inputs!{col}17/100)/(1+Inputs!{col}16/100)-Inputs!{col}18 for Engine!D6:G6.
"""
    if time_period == 1:
        projection_column = 'C'
        prior_debt_to_gdp_percent = initial_debt_to_gdp(ctx)
    elif time_period == 2:
        projection_column = 'D'
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=1)
    elif time_period == 3:
        projection_column = 'E'
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=2)
    elif time_period == 4:
        projection_column = 'F'
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=3)
    elif time_period == 5:
        projection_column = 'G'
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=4)
    else:
        return XlError('#VALUE!')
    interest_rate_percent = xl_cell(ctx, 'Inputs!' + projection_column + '17')
    growth_rate_percent = xl_cell(ctx, 'Inputs!' + projection_column + '16')
    primary_balance_percent = xl_cell(ctx, 'Inputs!' + projection_column + '18')
    return xl_sub(xl_div(xl_mul(prior_debt_to_gdp_percent, xl_add(1.0, xl_div(interest_rate_percent, 100.0))), xl_add(1.0, xl_div(growth_rate_percent, 100.0))), primary_balance_percent)

def shocked_debt_to_gdp_percent(ctx, time_period):
    """Return shocked debt-to-GDP for the selected projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index, where 1 maps to Engine column C and 5 maps to Engine column G.

    Returns:
        Shocked debt-to-GDP percentage for the projection year, or an Excel error value.

    Note:
        Covers Engine!C20:G20. Excel: for time period 1, =Inputs!B6*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16; for later periods, =Engine!{previous_col}20*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16.
"""
    if time_period == 1:
        projection_column = 'C'
    elif time_period == 2:
        projection_column = 'D'
    elif time_period == 3:
        projection_column = 'E'
    elif time_period == 4:
        projection_column = 'F'
    elif time_period == 5:
        projection_column = 'G'
    else:
        return XlError.VALUE
    if time_period == 1:
        prior_debt_to_gdp_percent = initial_debt_to_gdp(ctx)
    else:
        prior_debt_to_gdp_percent = shocked_debt_to_gdp_percent(ctx, time_period=time_period - 1)
    baseline_interest_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}17')
    interest_shock_parameter_selection = xl_cell(ctx, 'Inputs!B22')
    if isinstance(interest_shock_parameter_selection, XlError):
        interest_rate_shock_pp = interest_shock_parameter_selection
    else:
        interest_shock_parameter_index = to_int(interest_shock_parameter_selection)
        if isinstance(interest_shock_parameter_index, XlError):
            interest_rate_shock_pp = interest_shock_parameter_index
        elif interest_shock_parameter_index < 1 or interest_shock_parameter_index > 3:
            interest_rate_shock_pp = XlError.VALUE
        elif interest_shock_parameter_index == 1:
            interest_rate_shock_pp = 0.0
        elif interest_shock_parameter_index == 2:
            interest_rate_shock_pp = selected_shock_magnitude_pp(ctx)
        elif interest_shock_parameter_index == 3:
            interest_rate_shock_pp = 0.0
        else:
            interest_rate_shock_pp = XlError.VALUE
    interest_shock_active_indicator = shock_active(ctx, time_period=time_period)
    shocked_interest_rate_percent = xl_add(baseline_interest_rate_percent, xl_mul(interest_rate_shock_pp, interest_shock_active_indicator))
    interest_rate_debt_factor = xl_add(1.0, xl_div(shocked_interest_rate_percent, 100.0))
    debt_after_interest_factor = xl_mul(prior_debt_to_gdp_percent, interest_rate_debt_factor)
    baseline_growth_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}16')
    growth_shock_parameter_selection = xl_cell(ctx, 'Inputs!B22')
    if isinstance(growth_shock_parameter_selection, XlError):
        growth_rate_shock_pp = growth_shock_parameter_selection
    else:
        growth_shock_parameter_index = to_int(growth_shock_parameter_selection)
        if isinstance(growth_shock_parameter_index, XlError):
            growth_rate_shock_pp = growth_shock_parameter_index
        elif growth_shock_parameter_index < 1 or growth_shock_parameter_index > 3:
            growth_rate_shock_pp = XlError.VALUE
        elif growth_shock_parameter_index == 1:
            growth_rate_shock_pp = selected_shock_magnitude_pp(ctx)
        elif growth_shock_parameter_index == 2:
            growth_rate_shock_pp = 0.0
        elif growth_shock_parameter_index == 3:
            growth_rate_shock_pp = 0.0
        else:
            growth_rate_shock_pp = XlError.VALUE
    growth_shock_active_indicator = shock_active(ctx, time_period=time_period)
    shocked_growth_rate_percent = xl_add(baseline_growth_rate_percent, xl_mul(growth_rate_shock_pp, growth_shock_active_indicator))
    growth_rate_debt_factor = xl_add(1.0, xl_div(shocked_growth_rate_percent, 100.0))
    debt_after_growth_factor = xl_div(debt_after_interest_factor, growth_rate_debt_factor)
    shocked_primary_balance_percent = shocked_primary_balance_percent_gdp(ctx, time_period=time_period)
    return xl_sub(debt_after_growth_factor, shocked_primary_balance_percent)

def shocked_primary_balance_percent_gdp(ctx, time_period):
    """Return the primary balance, shocked as a percent of GDP, for a projection period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection period number, from 1 through 5.

    Returns:
        The Excel-equivalent shocked primary balance percentage of GDP for the requested period.

    Note:
        Covers Engine!C16, Engine!D16, Engine!E16, Engine!F16, and Engine!G16.
        Excel: =Inputs!{period_column}18+CHOOSE(Inputs!$B$22,0,0,Engine!$B$9)*Engine!{period_column}10.
"""
    projection_workbook_columns_by_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    if time_period not in projection_workbook_columns_by_period:
        return XlError.VALUE
    projection_workbook_column = projection_workbook_columns_by_period[time_period]
    baseline_primary_balance_percent_gdp = xl_cell(ctx, f'Inputs!{projection_workbook_column}18')
    shock_selector = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_selector, XlError):
        primary_balance_shock_pp = shock_selector
    else:
        shock_selector_index = to_int(shock_selector)
        if isinstance(shock_selector_index, XlError):
            primary_balance_shock_pp = shock_selector_index
        elif shock_selector_index < 1 or shock_selector_index > 3:
            primary_balance_shock_pp = XlError.VALUE
        elif shock_selector_index == 1:
            primary_balance_shock_pp = 0.0
        elif shock_selector_index == 2:
            primary_balance_shock_pp = 0.0
        elif shock_selector_index == 3:
            primary_balance_shock_pp = selected_shock_magnitude_pp(ctx)
        else:
            primary_balance_shock_pp = XlError.VALUE
    active_shock_indicator = shock_active(ctx, time_period=time_period)
    return xl_add(baseline_primary_balance_percent_gdp, xl_mul(primary_balance_shock_pp, active_shock_indicator))

def shock_active(ctx, time_period):
    """Return 1.0 when the projection year is at or after the configured shock year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection period number, where 1 through 5 map to Engine columns C through G.

    Returns:
        1.0 if the projection year is at or after the shock year, 0.0 if it is before the shock year, or an Excel error propagated from the comparison.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{projection_column}5>=Inputs!B21,1,0).
"""
    projection_columns_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    projection_column = projection_columns_by_time_period[time_period]
    projection_year = xl_cell(ctx, f'Engine!{projection_column}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    projection_year_at_or_after_shock_year = xl_ge(projection_year, shock_year)
    shock_is_active = to_bool(projection_year_at_or_after_shock_year)
    if isinstance(shock_is_active, XlError):
        return shock_is_active
    if shock_is_active:
        return 1.0
    return 0.0

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio from the country selector table.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:Inputs!C12,MATCH(Inputs!B5,Inputs!A10:Inputs!A12,0),2).
"""
    selected_country = xl_cell(ctx, 'Inputs!B5')
    country_selector_table = ('Inputs', 10, 1, 12, 3)
    country_selector_country_names = np.array(np.array([[xl_cell(ctx, 'Inputs!A10')], [xl_cell(ctx, 'Inputs!A11')], [xl_cell(ctx, 'Inputs!A12')]], dtype=object), dtype=object)
    selected_country_row = xl_match(selected_country, country_selector_country_names, 0.0)
    initial_debt_to_gdp_column = 2.0
    initial_debt_to_gdp_reference = xl_index_ref(country_selector_table, selected_country_row, initial_debt_to_gdp_column)
    return xl_offset(ctx, initial_debt_to_gdp_reference, 0.0, 0.0)

def selected_shock_magnitude_pp(ctx):
    """Return the shock magnitude in percentage points for the selected shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude in percentage points selected from the shock activation row.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    selected_shock_type = xl_cell(ctx, 'Inputs!B22')
    shock_type_column_offset = xl_sub(selected_shock_type, 1.0)
    shock_magnitude_pp_anchor = ('Inputs', 26, 2)
    return xl_offset(ctx, shock_magnitude_pp_anchor, 0.0, shock_type_column_offset, None, None)

# --- Projection public address aliases ---

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B13': ('shocked_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B14': ('debt_to_gdp_percent_shock_impact', {'time_period': 1}),
    'Outputs!C12': ('baseline_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C14': ('debt_to_gdp_percent_shock_impact', {'time_period': 2}),
    'Outputs!D12': ('baseline_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D14': ('debt_to_gdp_percent_shock_impact', {'time_period': 3}),
    'Outputs!E12': ('baseline_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E14': ('debt_to_gdp_percent_shock_impact', {'time_period': 4}),
    'Outputs!F12': ('baseline_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F14': ('debt_to_gdp_percent_shock_impact', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'selected_shock_magnitude_pp',
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
