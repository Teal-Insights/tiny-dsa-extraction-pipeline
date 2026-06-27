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

def debt_to_gdp_percent_shock_delta(ctx, time_period):
    """Return the shocked debt-to-GDP percentage less the baseline debt-to-GDP percentage.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection time period corresponding to the workbook output column.

    Returns:
        Difference between shocked and baseline debt-to-GDP percentages.

    Note:
        Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6 through =Engine!G20-Engine!G6.
"""
    shocked_debt_to_gdp_percentage = shocked_debt_to_gdp_percent(ctx, time_period=time_period)
    baseline_debt_to_gdp_percentage = baseline_debt_to_gdp_percent(ctx, time_period=time_period)
    return xl_sub(shocked_debt_to_gdp_percentage, baseline_debt_to_gdp_percentage)

def baseline_debt_to_gdp_percent(ctx, time_period):
    """Return baseline debt-to-GDP percent for the requested projection period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection period number, 1 through 5, corresponding to Engine columns C through G.

    Returns:
        Baseline debt-to-GDP percentage for the requested projection period.

    Note:
        Covers Engine!C6:G6. Excel: Engine!C6: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18; Engine!D6: =C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18; Engine!E6: =D6*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Inputs!E18; Engine!F6: =E6*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Inputs!F18; Engine!G6: =F6*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Inputs!G18.
"""
    input_columns_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    inputs_projection_column = input_columns_by_time_period[time_period]
    if time_period == 1:
        prior_debt_to_gdp_percent = selected_country_initial_debt_to_gdp(ctx)
    else:
        previous_time_period = time_period - 1
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=previous_time_period)
    debt_accumulation_rate_percent = xl_cell(ctx, f'Inputs!{inputs_projection_column}17')
    shocked_debt_to_gdp_denominator_percent = shocked_debt_to_gdp_percent(ctx, time_period=time_period)
    primary_balance_percent = xl_cell(ctx, f'Inputs!{inputs_projection_column}18')
    return xl_sub(xl_div(xl_mul(prior_debt_to_gdp_percent, xl_add(1.0, xl_div(debt_accumulation_rate_percent, 100.0))), xl_add(1.0, xl_div(shocked_debt_to_gdp_denominator_percent, 100.0))), primary_balance_percent)

def shocked_debt_to_gdp_percent(ctx, time_period):
    """Return the shocked-path debt-to-GDP percentage for a projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year number, where 1 maps to Engine column C and 5 maps to Engine column G.

    Returns:
        Shocked-path debt-to-GDP percentage for the requested projection year.

    Note:
        Covers Engine!C20:G20. Excel: time_period 1 uses =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16; time_periods 2-5 use =Engine!{prior_col}20*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16.
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
        prior_debt_to_gdp_percent = selected_country_initial_debt_to_gdp(ctx)
    else:
        previous_time_period = time_period - 1
        prior_debt_to_gdp_percent = shocked_debt_to_gdp_percent(ctx, time_period=previous_time_period)
    interest_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}17')
    shock_parameter_choice_for_interest = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_parameter_choice_for_interest, XlError):
        interest_rate_shock_magnitude_pp = shock_parameter_choice_for_interest
    else:
        interest_shock_parameter_index = to_int(shock_parameter_choice_for_interest)
        if isinstance(interest_shock_parameter_index, XlError):
            interest_rate_shock_magnitude_pp = interest_shock_parameter_index
        elif interest_shock_parameter_index < 1 or interest_shock_parameter_index > 3:
            interest_rate_shock_magnitude_pp = XlError.VALUE
        elif interest_shock_parameter_index == 1:
            interest_rate_shock_magnitude_pp = 0.0
        elif interest_shock_parameter_index == 2:
            interest_rate_shock_magnitude_pp = selected_shock_magnitude_pp(ctx)
        elif interest_shock_parameter_index == 3:
            interest_rate_shock_magnitude_pp = 0.0
        else:
            interest_rate_shock_magnitude_pp = XlError.VALUE
    active_interest_rate_shock = shock_active(ctx, time_period=time_period)
    shocked_interest_rate_factor = xl_add(1.0, xl_div(xl_add(interest_rate_percent, xl_mul(interest_rate_shock_magnitude_pp, active_interest_rate_shock)), 100.0))
    debt_after_interest_effect = xl_mul(prior_debt_to_gdp_percent, shocked_interest_rate_factor)
    growth_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}16')
    shock_parameter_choice_for_growth = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_parameter_choice_for_growth, XlError):
        growth_rate_shock_magnitude_pp = shock_parameter_choice_for_growth
    else:
        growth_shock_parameter_index = to_int(shock_parameter_choice_for_growth)
        if isinstance(growth_shock_parameter_index, XlError):
            growth_rate_shock_magnitude_pp = growth_shock_parameter_index
        elif growth_shock_parameter_index < 1 or growth_shock_parameter_index > 3:
            growth_rate_shock_magnitude_pp = XlError.VALUE
        elif growth_shock_parameter_index == 1:
            growth_rate_shock_magnitude_pp = selected_shock_magnitude_pp(ctx)
        elif growth_shock_parameter_index == 2:
            growth_rate_shock_magnitude_pp = 0.0
        elif growth_shock_parameter_index == 3:
            growth_rate_shock_magnitude_pp = 0.0
        else:
            growth_rate_shock_magnitude_pp = XlError.VALUE
    active_growth_rate_shock = shock_active(ctx, time_period=time_period)
    shocked_growth_rate_factor = xl_add(1.0, xl_div(xl_add(growth_rate_percent, xl_mul(growth_rate_shock_magnitude_pp, active_growth_rate_shock)), 100.0))
    debt_before_primary_balance = xl_div(debt_after_interest_effect, shocked_growth_rate_factor)
    primary_balance_percent_gdp = shocked_primary_balance_percent_gdp(ctx, time_period=time_period)
    return xl_sub(debt_before_primary_balance, primary_balance_percent_gdp)

def shocked_primary_balance_percent_gdp(ctx, time_period):
    """Return the shocked primary balance as a percent of GDP for a projection period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection time period from 1 through 5.

    Returns:
        Excel-compatible shocked primary balance value for the requested projection period.

    Note:
        Covers Engine!C16, Engine!D16, Engine!E16, Engine!F16, and Engine!G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!$B$22,0,0,Engine!$B$9)*Engine!{col}10.
"""
    workbook_column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    workbook_column = workbook_column_by_time_period[time_period]
    unshocked_primary_balance = xl_cell(ctx, f'Inputs!{workbook_column}18')
    shock_selector = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_selector, XlError):
        selected_primary_balance_shock = shock_selector
    else:
        shock_selector_index = to_int(shock_selector)
        if isinstance(shock_selector_index, XlError):
            selected_primary_balance_shock = shock_selector_index
        elif shock_selector_index < 1 or shock_selector_index > 3:
            selected_primary_balance_shock = XlError.VALUE
        elif shock_selector_index == 1:
            selected_primary_balance_shock = 0.0
        elif shock_selector_index == 2:
            selected_primary_balance_shock = 0.0
        elif shock_selector_index == 3:
            selected_primary_balance_shock = selected_shock_magnitude_pp(ctx)
        else:
            selected_primary_balance_shock = XlError.VALUE
    active_shock_indicator = shock_active(ctx, time_period=time_period)
    return xl_add(unshocked_primary_balance, xl_mul(selected_primary_balance_shock, active_shock_indicator))

def shock_active(ctx, time_period):
    """Return 1.0 when the shock is active for the projection time period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection time period, where 1 through 5 map to Engine!C through Engine!G.

    Returns:
        1.0 if the projection year is greater than or equal to the shock year, else 0.0.
        Returns an Excel error if the comparison cannot be evaluated.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{period_column}5>=Inputs!B21,1,0).
"""
    if time_period == 1:
        projection_year = xl_cell(ctx, 'Engine!C5')
    elif time_period == 2:
        projection_year = xl_cell(ctx, 'Engine!D5')
    elif time_period == 3:
        projection_year = xl_cell(ctx, 'Engine!E5')
    elif time_period == 4:
        projection_year = xl_cell(ctx, 'Engine!F5')
    elif time_period == 5:
        projection_year = xl_cell(ctx, 'Engine!G5')
    else:
        return XlError.VALUE
    shock_year = xl_cell(ctx, 'Inputs!B21')
    shock_started_comparison = xl_ge(projection_year, shock_year)
    shock_is_active = to_bool(shock_started_comparison)
    if isinstance(shock_is_active, XlError):
        return shock_is_active
    if shock_is_active:
        return 1.0
    else:
        return 0.0

def selected_country_initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Initial debt-to-GDP ratio from the country selector table.

    Note:
        Covers Inputs!B6. Excel: =INDEX(Inputs!A10:Inputs!C12,MATCH(Inputs!B5,Inputs!A10:Inputs!A12,0),2).
"""
    selected_country_name = xl_cell(ctx, 'Inputs!B5')
    country_selector_names = np.array(np.array([[xl_cell(ctx, 'Inputs!A10')], [xl_cell(ctx, 'Inputs!A11')], [xl_cell(ctx, 'Inputs!A12')]], dtype=object), dtype=object)
    selected_country_table_row = xl_match(selected_country_name, country_selector_names, 0.0)
    initial_debt_to_gdp_reference = xl_index_ref(('Inputs', 10, 1, 12, 3), selected_country_table_row, 2.0)
    return xl_offset(ctx, initial_debt_to_gdp_reference, 0.0, 0.0)

def selected_shock_magnitude_pp(ctx):
    """Look up the shock magnitude in percentage points for the selected shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude in percentage points selected from the shock activation inputs.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    selected_shock_type = xl_cell(ctx, 'Inputs!B22')
    selected_shock_column_offset = xl_sub(selected_shock_type, 1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, selected_shock_column_offset, None, None)

# --- Projection public address aliases ---

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B13': ('shocked_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B14': ('debt_to_gdp_percent_shock_delta', {'time_period': 1}),
    'Outputs!C12': ('baseline_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C14': ('debt_to_gdp_percent_shock_delta', {'time_period': 2}),
    'Outputs!D12': ('baseline_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D14': ('debt_to_gdp_percent_shock_delta', {'time_period': 3}),
    'Outputs!E12': ('baseline_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E14': ('debt_to_gdp_percent_shock_delta', {'time_period': 4}),
    'Outputs!F12': ('baseline_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F14': ('debt_to_gdp_percent_shock_delta', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'selected_shock_magnitude_pp',
    'Inputs!B6': 'selected_country_initial_debt_to_gdp',
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
