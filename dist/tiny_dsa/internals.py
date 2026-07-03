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
    """Return the deviation between shocked and baseline debt-to-GDP for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        The difference between the shocked debt-to-GDP ratio and the baseline debt-to-GDP ratio.

    Note:
        Covers Outputs!B14:F14. Excel: =Engine!{col}20-Engine!{col}6.
"""
    shocked = shocked_debt_to_gdp(ctx, time_period=time_period)
    baseline = baseline_debt_to_gdp(ctx, time_period=time_period)
    return xl_number(shocked) - xl_number(baseline)

def baseline_debt_to_gdp(ctx, time_period):
    """Return the baseline path debt-to-GDP percentage for the given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Baseline debt-to-GDP ratio as a percentage.

    Note:
        Covers Engine!C6:G6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18 for year 1;
        =prior year value*(1+Inputs!<col>17/100)/(1+Inputs!<col>16/100)-Inputs!<col>18 for subsequent years.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    if time_period == 1:
        prior_debt_to_gdp = xl_number(initial_debt_to_gdp(ctx))
    else:
        prior_debt_to_gdp = xl_number(baseline_debt_to_gdp(ctx, time_period=time_period - 1))
    primary_surplus_rate = xl_number(xl_cell(ctx, f'Inputs!{column}17'))
    interest_rate = xl_number(xl_cell(ctx, f'Inputs!{column}16'))
    other_adjustment = xl_number(xl_cell(ctx, f'Inputs!{column}18'))
    growth_factor = xl_number((xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(primary_surplus_rate, xl_number(100.0)))) / (xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(interest_rate, xl_number(100.0)))))
    return xl_number(prior_debt_to_gdp * growth_factor - other_adjustment)

def shocked_debt_to_gdp(ctx, time_period):
    """Return the shocked debt-to-GDP ratio for the given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Shocked debt-to-GDP percentage for the projection year.

    Note:
        Covers Engine!C20:G20. Excel: =IF time_period=1, Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16, else prior_debt*(1+(Inputs!{col}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{col}10)/100)-Engine!{col}16.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    if time_period == 1:
        prior_debt = initial_debt_to_gdp(ctx)
    else:
        prior_debt = shocked_debt_to_gdp(ctx, time_period=time_period - 1)
    nominal_growth_rate_input = xl_cell(ctx, f'Inputs!{column}17')
    nominal_growth_rate_shock = xl_number(xl_raise(XlError.VALUE) if (shock_parameter_index := xl_int(xl_cell(ctx, 'Inputs!B22'))) < 1 or shock_parameter_index > 3 else 0.0 if shock_parameter_index == 1 else shock_activation_offset(ctx) if shock_parameter_index == 2 else 0.0 if shock_parameter_index == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active(ctx, time_period=time_period))
    nominal_growth_rate = xl_number((xl_number(nominal_growth_rate_input) + nominal_growth_rate_shock) / xl_number(100.0))
    numerator = xl_number(prior_debt) * xl_number(xl_number(1.0) + nominal_growth_rate)
    real_growth_rate_input = xl_cell(ctx, f'Inputs!{column}16')
    real_growth_rate_shock = xl_number(xl_raise(XlError.VALUE) if (shock_parameter_index := xl_int(xl_cell(ctx, 'Inputs!B22'))) < 1 or shock_parameter_index > 3 else shock_activation_offset(ctx) if shock_parameter_index == 1 else 0.0 if shock_parameter_index == 2 else 0.0 if shock_parameter_index == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active(ctx, time_period=time_period))
    real_growth_rate = xl_number((xl_number(real_growth_rate_input) + real_growth_rate_shock) / xl_number(100.0))
    denominator = xl_number(xl_number(1.0) + real_growth_rate)
    ratio = (lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(numerator, denominator)
    return xl_number(ratio - xl_number(shocked_primary_balance(ctx, time_period=time_period)))

def shocked_primary_balance(ctx, time_period):
    """Return the shocked primary balance as a percentage of GDP for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        Shocked primary balance to GDP ratio.

    Note:
        Covers Engine!C16:G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!$B$22,0,0,$B$9)*{col}10.
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    baseline_balance = xl_number(xl_cell(ctx, f'Inputs!{column}18'))
    shock_index = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if shock_index < 1 or shock_index > 3:
        xl_raise(XlError.VALUE)
    shock_offset = 0.0
    if shock_index == 3:
        shock_offset = xl_eval(ctx, 'Engine!B9', shock_activation_offset)
    shock_adjustment = xl_number(shock_offset) * xl_number(shock_active(ctx, time_period=time_period))
    return xl_number(baseline_balance) + shock_adjustment

def shock_active(ctx, time_period):
    """Return 1.0 when the shock is active for the given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1 through 5).

    Returns:
        1.0 if the projection year is at or after the shock year, else 0.0.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0).
"""
    column_by_time_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_time_period.get(time_period)
    if column is None:
        xl_raise(XlError.VALUE)
    projection_year = xl_cell(ctx, f'Engine!{column}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    is_active = xl_bool(xl_compare('>=', projection_year, shock_year))
    return 1.0 if is_active else 0.0

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Initial debt-to-GDP ratio from the country profile table.

    Note:
        Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).
"""
    country_profile_table = ('Inputs', 10, 1, 12, 3)
    selected_country = xl_cell(ctx, 'Inputs!B5')
    matched_row = xl_match(selected_country, xl_range(ctx, 'Inputs!A10:Inputs!A12'), 0.0)
    debt_to_gdp_ref = xl_index_ref(country_profile_table, matched_row, 2.0)
    return xl_offset(ctx, debt_to_gdp_ref, 0.0, 0.0)

def shock_activation_offset(ctx):
    """Returns the shock activation value from the Inputs sheet using an OFFSET from cell B26.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        The value at Inputs!B26 offset by 0 rows and (Inputs!B22 - 1) columns.

    Note:
        Covers Engine!B9. Excel: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1)
"""
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_number(xl_cell(ctx, 'Inputs!B22')) - xl_number(1.0), None, None)

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
    'Engine!B9': 'shock_activation_offset',
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
