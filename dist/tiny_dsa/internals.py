from __future__ import annotations

from .runtime import (
    XlError,
    to_bool,
    to_int,
    xl_cell,
    xl_compare,
    xl_eval,
    xl_index_ref,
    xl_match,
    xl_number,
    xl_offset,
    xl_raise,
    xl_range,
)

# --- Formula cell functions ---

def debt_to_gdp_shock_impact_percent(ctx, time_period):
    """Return the shock impact on debt-to-GDP percent for a projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Shocked debt-to-GDP percent minus baseline debt-to-GDP percent.

    Note:
        Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6 through =Engine!G20-Engine!G6.
"""
    shocked_debt_to_gdp_percent_value = shocked_debt_to_gdp_percent(ctx, time_period=time_period)
    baseline_debt_to_gdp_percent_value = baseline_debt_to_gdp_percent(ctx, time_period=time_period)
    return xl_number(shocked_debt_to_gdp_percent_value) - xl_number(baseline_debt_to_gdp_percent_value)

def baseline_debt_to_gdp_percent(ctx, time_period):
    """Return the baseline debt-to-GDP percentage for the given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Baseline debt-to-GDP percentage for the projection year, or an XlError.

    Note:
        Covers Engine!C6, Engine!D6, Engine!E6, Engine!F6, and Engine!G6. Excel:
        Engine!C6 =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
        Engine!D6 =Engine!C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18.
        Engine!E6 =Engine!D6*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Inputs!E18.
        Engine!F6 =Engine!E6*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Inputs!F18.
        Engine!G6 =Engine!F6*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Inputs!G18.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    projection_column = column_by_time_period.get(time_period)
    if projection_column is None:
        return XlError.VALUE
    if time_period == 1:
        prior_debt_to_gdp_percent = selected_country_initial_debt_to_gdp_ratio(ctx)
    else:
        previous_time_period = time_period - 1
        prior_debt_to_gdp_percent = baseline_debt_to_gdp_percent(ctx, time_period=previous_time_period)
    interest_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}17')
    interest_rate_fraction = (lambda interest_rate_percent_value, percent_denominator: interest_rate_percent_value / percent_denominator if percent_denominator != 0 else xl_raise(XlError.DIV))(xl_number(interest_rate_percent), xl_number(100.0))
    interest_factor = xl_number(1.0) + xl_number(interest_rate_fraction)
    debt_before_growth_adjustment = xl_number(prior_debt_to_gdp_percent) * xl_number(interest_factor)
    gdp_growth_rate_percent = xl_cell(ctx, f'Inputs!{projection_column}16')
    gdp_growth_rate_fraction = (lambda gdp_growth_rate_percent_value, percent_denominator: gdp_growth_rate_percent_value / percent_denominator if percent_denominator != 0 else xl_raise(XlError.DIV))(xl_number(gdp_growth_rate_percent), xl_number(100.0))
    growth_factor = xl_number(1.0) + xl_number(gdp_growth_rate_fraction)
    debt_after_growth_adjustment = (lambda debt_before_growth_adjustment_value, growth_denominator: debt_before_growth_adjustment_value / growth_denominator if growth_denominator != 0 else xl_raise(XlError.DIV))(xl_number(debt_before_growth_adjustment), xl_number(growth_factor))
    primary_balance_percent = xl_cell(ctx, f'Inputs!{projection_column}18')
    return xl_number(debt_after_growth_adjustment) - xl_number(primary_balance_percent)

def shocked_debt_to_gdp_percent(ctx, time_period):
    """Return the shocked debt-to-GDP ratio for a projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Shocked debt-to-GDP ratio percentage for the requested projection year, or an Excel error value.

    Note:
        Covers Engine!C20, Engine!D20, Engine!E20, Engine!F20, and Engine!G20. Excel: Engine!C20 = Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16; Engine!D20:G20 = prior Engine row 20*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    workbook_column = column_by_time_period.get(time_period)
    if workbook_column is None:
        return XlError.VALUE
    if time_period == 1:
        prior_debt_to_gdp_percent = selected_country_initial_debt_to_gdp_ratio(ctx)
    else:
        prior_debt_to_gdp_percent = shocked_debt_to_gdp_percent(ctx, time_period=time_period - 1)
    if isinstance(prior_debt_to_gdp_percent, XlError):
        return xl_raise(prior_debt_to_gdp_percent)
    prior_debt_to_gdp_number = xl_number(prior_debt_to_gdp_percent)
    if isinstance(prior_debt_to_gdp_number, XlError):
        return xl_raise(prior_debt_to_gdp_number)
    baseline_interest_rate_percent = xl_cell(ctx, f'Inputs!{workbook_column}17')
    if isinstance(baseline_interest_rate_percent, XlError):
        return xl_raise(baseline_interest_rate_percent)
    baseline_interest_rate_number = xl_number(baseline_interest_rate_percent)
    if isinstance(baseline_interest_rate_number, XlError):
        return xl_raise(baseline_interest_rate_number)
    interest_rate_shock_parameter_cell = xl_cell(ctx, 'Inputs!B22')
    if isinstance(interest_rate_shock_parameter_cell, XlError):
        return xl_raise(interest_rate_shock_parameter_cell)
    interest_rate_shock_parameter_index = to_int(interest_rate_shock_parameter_cell)
    if isinstance(interest_rate_shock_parameter_index, XlError):
        return xl_raise(interest_rate_shock_parameter_index)
    interest_rate_shock_parameter_number = xl_number(interest_rate_shock_parameter_index)
    if isinstance(interest_rate_shock_parameter_number, XlError):
        return xl_raise(interest_rate_shock_parameter_number)
    interest_rate_shock_parameter_is_below_first_choice = to_bool(xl_compare('<', interest_rate_shock_parameter_number, xl_number(1.0)))
    if isinstance(interest_rate_shock_parameter_is_below_first_choice, XlError):
        return xl_raise(interest_rate_shock_parameter_is_below_first_choice)
    if interest_rate_shock_parameter_is_below_first_choice:
        return xl_raise(XlError.VALUE)
    interest_rate_shock_parameter_is_above_last_choice = to_bool(xl_compare('>', interest_rate_shock_parameter_number, xl_number(3.0)))
    if isinstance(interest_rate_shock_parameter_is_above_last_choice, XlError):
        return xl_raise(interest_rate_shock_parameter_is_above_last_choice)
    if interest_rate_shock_parameter_is_above_last_choice:
        return xl_raise(XlError.VALUE)
    interest_rate_uses_shock_magnitude = {1: False, 2: True, 3: False}.get(interest_rate_shock_parameter_index)
    if interest_rate_uses_shock_magnitude is None:
        return xl_raise(XlError.VALUE)
    if interest_rate_uses_shock_magnitude:
        interest_rate_shock_choice_pp = selected_shock_magnitude_pp(ctx)
    else:
        interest_rate_shock_choice_pp = xl_number(0.0)
    if isinstance(interest_rate_shock_choice_pp, XlError):
        return xl_raise(interest_rate_shock_choice_pp)
    interest_rate_shock_choice_number = xl_number(interest_rate_shock_choice_pp)
    if isinstance(interest_rate_shock_choice_number, XlError):
        return xl_raise(interest_rate_shock_choice_number)
    interest_rate_shock_activation = shock_active(ctx, time_period=time_period)
    if isinstance(interest_rate_shock_activation, XlError):
        return xl_raise(interest_rate_shock_activation)
    interest_rate_shock_activation_number = xl_number(interest_rate_shock_activation)
    if isinstance(interest_rate_shock_activation_number, XlError):
        return xl_raise(interest_rate_shock_activation_number)
    interest_rate_shock_adjustment_pp = xl_number(interest_rate_shock_choice_number * interest_rate_shock_activation_number)
    if isinstance(interest_rate_shock_adjustment_pp, XlError):
        return xl_raise(interest_rate_shock_adjustment_pp)
    interest_rate_percent_amount = xl_number(baseline_interest_rate_number + xl_number(interest_rate_shock_adjustment_pp))
    if isinstance(interest_rate_percent_amount, XlError):
        return xl_raise(interest_rate_percent_amount)
    interest_rate_percent_scale = xl_number(100.0)
    interest_rate_percent_scale_is_zero = to_bool(xl_compare('=', interest_rate_percent_scale, xl_number(0.0)))
    if isinstance(interest_rate_percent_scale_is_zero, XlError):
        return xl_raise(interest_rate_percent_scale_is_zero)
    if interest_rate_percent_scale_is_zero:
        return xl_raise(XlError.DIV)
    interest_rate_percent_component = xl_number(interest_rate_percent_amount / interest_rate_percent_scale)
    if isinstance(interest_rate_percent_component, XlError):
        return xl_raise(interest_rate_percent_component)
    debt_interest_factor = xl_number(xl_number(1.0) + xl_number(interest_rate_percent_component))
    if isinstance(debt_interest_factor, XlError):
        return xl_raise(debt_interest_factor)
    debt_interest_numerator = xl_number(prior_debt_to_gdp_number * xl_number(debt_interest_factor))
    if isinstance(debt_interest_numerator, XlError):
        return xl_raise(debt_interest_numerator)
    baseline_growth_rate_percent = xl_cell(ctx, f'Inputs!{workbook_column}16')
    if isinstance(baseline_growth_rate_percent, XlError):
        return xl_raise(baseline_growth_rate_percent)
    baseline_growth_rate_number = xl_number(baseline_growth_rate_percent)
    if isinstance(baseline_growth_rate_number, XlError):
        return xl_raise(baseline_growth_rate_number)
    growth_rate_shock_parameter_cell = xl_cell(ctx, 'Inputs!B22')
    if isinstance(growth_rate_shock_parameter_cell, XlError):
        return xl_raise(growth_rate_shock_parameter_cell)
    growth_rate_shock_parameter_index = to_int(growth_rate_shock_parameter_cell)
    if isinstance(growth_rate_shock_parameter_index, XlError):
        return xl_raise(growth_rate_shock_parameter_index)
    growth_rate_shock_parameter_number = xl_number(growth_rate_shock_parameter_index)
    if isinstance(growth_rate_shock_parameter_number, XlError):
        return xl_raise(growth_rate_shock_parameter_number)
    growth_rate_shock_parameter_is_below_first_choice = to_bool(xl_compare('<', growth_rate_shock_parameter_number, xl_number(1.0)))
    if isinstance(growth_rate_shock_parameter_is_below_first_choice, XlError):
        return xl_raise(growth_rate_shock_parameter_is_below_first_choice)
    if growth_rate_shock_parameter_is_below_first_choice:
        return xl_raise(XlError.VALUE)
    growth_rate_shock_parameter_is_above_last_choice = to_bool(xl_compare('>', growth_rate_shock_parameter_number, xl_number(3.0)))
    if isinstance(growth_rate_shock_parameter_is_above_last_choice, XlError):
        return xl_raise(growth_rate_shock_parameter_is_above_last_choice)
    if growth_rate_shock_parameter_is_above_last_choice:
        return xl_raise(XlError.VALUE)
    growth_rate_uses_shock_magnitude = {1: True, 2: False, 3: False}.get(growth_rate_shock_parameter_index)
    if growth_rate_uses_shock_magnitude is None:
        return xl_raise(XlError.VALUE)
    if growth_rate_uses_shock_magnitude:
        growth_rate_shock_choice_pp = selected_shock_magnitude_pp(ctx)
    else:
        growth_rate_shock_choice_pp = xl_number(0.0)
    if isinstance(growth_rate_shock_choice_pp, XlError):
        return xl_raise(growth_rate_shock_choice_pp)
    growth_rate_shock_choice_number = xl_number(growth_rate_shock_choice_pp)
    if isinstance(growth_rate_shock_choice_number, XlError):
        return xl_raise(growth_rate_shock_choice_number)
    growth_rate_shock_activation = shock_active(ctx, time_period=time_period)
    if isinstance(growth_rate_shock_activation, XlError):
        return xl_raise(growth_rate_shock_activation)
    growth_rate_shock_activation_number = xl_number(growth_rate_shock_activation)
    if isinstance(growth_rate_shock_activation_number, XlError):
        return xl_raise(growth_rate_shock_activation_number)
    growth_rate_shock_adjustment_pp = xl_number(growth_rate_shock_choice_number * growth_rate_shock_activation_number)
    if isinstance(growth_rate_shock_adjustment_pp, XlError):
        return xl_raise(growth_rate_shock_adjustment_pp)
    growth_rate_percent_amount = xl_number(baseline_growth_rate_number + xl_number(growth_rate_shock_adjustment_pp))
    if isinstance(growth_rate_percent_amount, XlError):
        return xl_raise(growth_rate_percent_amount)
    growth_rate_percent_scale = xl_number(100.0)
    growth_rate_percent_scale_is_zero = to_bool(xl_compare('=', growth_rate_percent_scale, xl_number(0.0)))
    if isinstance(growth_rate_percent_scale_is_zero, XlError):
        return xl_raise(growth_rate_percent_scale_is_zero)
    if growth_rate_percent_scale_is_zero:
        return xl_raise(XlError.DIV)
    growth_rate_percent_component = xl_number(growth_rate_percent_amount / growth_rate_percent_scale)
    if isinstance(growth_rate_percent_component, XlError):
        return xl_raise(growth_rate_percent_component)
    debt_growth_denominator = xl_number(xl_number(1.0) + xl_number(growth_rate_percent_component))
    if isinstance(debt_growth_denominator, XlError):
        return xl_raise(debt_growth_denominator)
    debt_growth_denominator_is_zero = to_bool(xl_compare('=', debt_growth_denominator, xl_number(0.0)))
    if isinstance(debt_growth_denominator_is_zero, XlError):
        return xl_raise(debt_growth_denominator_is_zero)
    if debt_growth_denominator_is_zero:
        return xl_raise(XlError.DIV)
    debt_before_primary_balance_adjustment = xl_number(debt_interest_numerator / debt_growth_denominator)
    if isinstance(debt_before_primary_balance_adjustment, XlError):
        return xl_raise(debt_before_primary_balance_adjustment)
    shocked_primary_balance_percent = shocked_primary_balance_percent_gdp(ctx, time_period=time_period)
    if isinstance(shocked_primary_balance_percent, XlError):
        return xl_raise(shocked_primary_balance_percent)
    shocked_primary_balance_number = xl_number(shocked_primary_balance_percent)
    if isinstance(shocked_primary_balance_number, XlError):
        return xl_raise(shocked_primary_balance_number)
    return xl_number(debt_before_primary_balance_adjustment) - xl_number(shocked_primary_balance_number)

def shocked_primary_balance_percent_gdp(ctx, time_period):
    """Return the shocked primary balance as a percent of GDP for a projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Primary balance, shocked, as a percent of GDP.

    Note:
        Covers Engine!C16:G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!$B$22,0,0,Engine!$B$9)*Engine!{col}10.
"""
    workbook_column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    workbook_column = workbook_column_by_time_period.get(time_period)
    if workbook_column is None:
        return XlError.VALUE
    baseline_primary_balance_percent_gdp = xl_cell(ctx, f'Inputs!{workbook_column}18')
    selected_shock_parameter_raw = xl_cell(ctx, 'Inputs!B22')
    if isinstance(selected_shock_parameter_raw, XlError):
        primary_balance_shock_magnitude_pp = xl_raise(selected_shock_parameter_raw)
    else:
        selected_shock_parameter_index = to_int(selected_shock_parameter_raw)
        if isinstance(selected_shock_parameter_index, XlError):
            primary_balance_shock_magnitude_pp = xl_raise(selected_shock_parameter_index)
        elif selected_shock_parameter_index in (1, 2):
            primary_balance_shock_magnitude_pp = 0.0
        elif selected_shock_parameter_index in (3,):
            primary_balance_shock_magnitude_pp = selected_shock_magnitude_pp(ctx)
        else:
            primary_balance_shock_magnitude_pp = xl_raise(XlError.VALUE)
    primary_balance_shock_activation = shock_active(ctx, time_period=time_period)
    return xl_number(baseline_primary_balance_percent_gdp) + xl_number(xl_number(primary_balance_shock_magnitude_pp) * xl_number(primary_balance_shock_activation))

def shock_active(ctx, time_period):
    """Return 1.0 when the shock is active for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    1.0 if the projection year is greater than or equal to the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!B21,1,0).
"""
    projection_year_column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    projection_year_column = projection_year_column_by_time_period.get(time_period)
    if projection_year_column is None:
        return XlError.VALUE
    projection_year = xl_cell(ctx, f'Engine!{projection_year_column}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    shock_activation_comparison = xl_compare('>=', projection_year, shock_year)
    shock_is_active = to_bool(shock_activation_comparison)
    if isinstance(shock_is_active, XlError):
        return xl_raise(shock_is_active)
    return xl_number(1.0) if shock_is_active else xl_number(0.0)

def selected_country_initial_debt_to_gdp_ratio(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Initial debt-to-GDP ratio from the country selector table.

    Note:
        Covers Inputs!B6. Excel: =INDEX(Inputs!A10:Inputs!C12,MATCH(Inputs!B5,Inputs!A10:Inputs!A12,0),2).
"""
    selected_country_name = xl_cell(ctx, 'Inputs!B5')
    country_selector_names = xl_range(ctx, 'Inputs!A10:Inputs!A12')
    selected_country_position = xl_match(selected_country_name, country_selector_names, xl_number(0.0))
    initial_debt_to_gdp_ratio_reference = xl_index_ref(('Inputs', 10, 1, 12, 3), selected_country_position, xl_number(2.0))
    return xl_offset(ctx, initial_debt_to_gdp_ratio_reference, xl_number(0.0), xl_number(0.0))

def selected_shock_magnitude_pp(ctx):
    """Return the selected shock magnitude in percentage points.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Shock magnitude in percentage points selected by the shock type input.

    Note:
        Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    selected_shock_type = xl_cell(ctx, 'Inputs!B22')
    shock_type_column_offset = xl_number(selected_shock_type) - xl_number(1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, shock_type_column_offset, None, None)

# --- Projection public address aliases ---

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B13': ('shocked_debt_to_gdp_percent', {'time_period': 1}),
    'Outputs!B14': ('debt_to_gdp_shock_impact_percent', {'time_period': 1}),
    'Outputs!C12': ('baseline_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp_percent', {'time_period': 2}),
    'Outputs!C14': ('debt_to_gdp_shock_impact_percent', {'time_period': 2}),
    'Outputs!D12': ('baseline_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp_percent', {'time_period': 3}),
    'Outputs!D14': ('debt_to_gdp_shock_impact_percent', {'time_period': 3}),
    'Outputs!E12': ('baseline_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp_percent', {'time_period': 4}),
    'Outputs!E14': ('debt_to_gdp_shock_impact_percent', {'time_period': 4}),
    'Outputs!F12': ('baseline_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp_percent', {'time_period': 5}),
    'Outputs!F14': ('debt_to_gdp_shock_impact_percent', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'selected_shock_magnitude_pp',
    'Inputs!B6': 'selected_country_initial_debt_to_gdp_ratio',
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
