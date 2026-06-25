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

def output_delta(ctx, time_period):
    """Return the difference between shocked and baseline debt-to-GDP for a given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 to 5).

Returns:
    Shocked debt-to-GDP minus baseline debt-to-GDP.

Note:
    Covers Outputs!B14:F14. Excel formula: =Engine!{col}20-Engine!{col}6 where column letter corresponds to time_period (C=1, D=2, E=3, F=4, G=5).
"""
    return xl_sub(debt_to_gdp(ctx, time_period=time_period), baseline_debt(ctx, time_period=time_period))

def baseline_debt(ctx, time_period):
    """Compute the baseline debt-to-GDP ratio for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period: Projection year index (1-based, 1..5).

    Returns:
        The baseline debt-to-GDP ratio.

    Note:
        Covers Engine!C6:G6. Excel formulas: Engine!C6: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18;
        Engine!D6: =C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18;
        Engine!E6: =D6*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Inputs!E18;
        Engine!F6: =E6*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Inputs!F18;
        Engine!G6: =F6*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Inputs!G18.
"""
    columns = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = columns[time_period]
    if time_period == 1:
        prior = initial_debt_to_gdp(ctx)
    else:
        prior = baseline_debt(ctx, time_period - 1)
    interest_rate = xl_cell(ctx, 'Inputs!' + col + '17')
    shocked_primary = primary_balance_shocked(ctx, time_period=time_period)
    primary_balance = xl_cell(ctx, 'Inputs!' + col + '18')
    return xl_sub(xl_div(xl_mul(prior, xl_add(1.0, xl_div(interest_rate, 100.0))), xl_add(1.0, xl_div(shocked_primary, 100.0))), primary_balance)

def debt_to_gdp(ctx, time_period):
    """Compute the shocked debt-to-GDP ratio for a given projection year.

    Args:
        ctx: Workbook evaluation context.
        time_period (int): The projection year index (1-based, 1 through 5).

    Returns:
        The shocked debt-to-GDP value for the column, or an XlError on error.

    Note:
        Covers Engine!C20:G20. Excel: ={PRIOR_DEBT}*(1+(Inputs!{COL}17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!{COL}10)/100)/(1+(Inputs!{COL}16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!{COL}10)/100)-Engine!{COL}16
"""
    col_letter = chr(ord('C') + time_period - 1)
    if time_period == 1:
        prior_debt = xl_cell(ctx, 'Inputs!B6')
    else:
        prior_debt = debt_to_gdp(ctx, time_period - 1)
    shock_type_raw = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_type_raw, XlError):
        return shock_type_raw
    shock_type_int = to_int(shock_type_raw)
    if isinstance(shock_type_int, XlError):
        return shock_type_int
    if shock_type_int < 1 or shock_type_int > 3:
        return XlError.VALUE
    shock_magnitude = shock_magnitude_resolved(ctx)
    if isinstance(shock_magnitude, XlError):
        return shock_magnitude
    shock_active_val = shock_active(ctx, time_period=time_period)
    if isinstance(shock_active_val, XlError):
        return shock_active_val
    if shock_type_int == 1:
        adjust_growth = shock_magnitude
        adjust_interest = 0.0
    elif shock_type_int == 2:
        adjust_growth = 0.0
        adjust_interest = shock_magnitude
    else:
        adjust_growth = 0.0
        adjust_interest = 0.0
    growth_baseline = xl_cell(ctx, f'Inputs!{col_letter}16')
    if isinstance(growth_baseline, XlError):
        return growth_baseline
    interest_baseline = xl_cell(ctx, f'Inputs!{col_letter}17')
    if isinstance(interest_baseline, XlError):
        return interest_baseline
    adjusted_interest = xl_add(interest_baseline, xl_mul(adjust_interest, shock_active_val))
    adjusted_growth = xl_add(growth_baseline, xl_mul(adjust_growth, shock_active_val))
    numerator_factor = xl_add(1.0, xl_div(adjusted_interest, 100.0))
    denominator_factor = xl_add(1.0, xl_div(adjusted_growth, 100.0))
    primary_balance = primary_balance_shocked(ctx, time_period=time_period)
    if isinstance(primary_balance, XlError):
        return primary_balance
    debt_before_subtraction = xl_div(xl_mul(prior_debt, numerator_factor), denominator_factor)
    return xl_sub(debt_before_subtraction, primary_balance)

def primary_balance_shocked(ctx, time_period):
    """Return the primary balance including the shock for a given time period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Integer time period from 1 (column C) to 5 (column G).

    Returns:
        The shocked primary balance as a float, or XlError if any input is missing or invalid.

    Note:
        Covers Engine!C16:G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!$B$22,0,0,$B$9)*{col}10
"""
    col_letter = chr(ord('C') + time_period - 1)
    primary_baseline = xl_cell(ctx, f'Inputs!{col_letter}18')
    shock_type_raw = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_type_raw, XlError):
        shock_multiplier = shock_type_raw
    else:
        shock_type_int = to_int(shock_type_raw)
        if isinstance(shock_type_int, XlError):
            shock_multiplier = shock_type_int
        elif shock_type_int < 1 or shock_type_int > 3:
            shock_multiplier = XlError.VALUE
        elif shock_type_int == 1 or shock_type_int == 2:
            shock_multiplier = 0.0
        else:
            shock_multiplier = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
    shock_active_val = shock_active(ctx, time_period=time_period)
    product = xl_mul(shock_multiplier, shock_active_val)
    return xl_add(primary_baseline, product)

def shock_active(ctx, time_period):
    """Return 1.0 when the shock is active for the given time period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Integer time period (1-based, 1..5) mapping to Engine column (C..G).

    Returns:
        1.0 if the projection year is at or after the shock year, else 0.0.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{COL}5>=Inputs!$B$21,1,0).
"""
    col = chr(ord('C') + time_period - 1)
    projection_year = xl_cell(ctx, f'Engine!{col}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    active_bool = to_bool(xl_ge(projection_year, shock_year))
    if isinstance(active_bool, XlError):
        return active_bool
    return 1.0 if active_bool else 0.0

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio from the country profile table.

Note:
    Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).
"""
    selected_country = xl_cell(ctx, 'Inputs!B5')
    country_array = np.array(np.array([[xl_cell(ctx, 'Inputs!A10')], [xl_cell(ctx, 'Inputs!A11')], [xl_cell(ctx, 'Inputs!A12')]], dtype=object), dtype=object)
    match_row = xl_match(selected_country, country_array, 0.0)
    table_range = ('Inputs', 10, 1, 12, 3)
    target_cell = xl_index_ref(table_range, match_row, 2.0)
    return xl_offset(ctx, target_cell, 0.0, 0.0)

def shock_magnitude_resolved(ctx):
    """Resolve the shock magnitude for the selected shock type.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        The shock magnitude from Inputs!B26:D26 corresponding to the shock type in Inputs!B22 (1..3).

    Note:
        Covers Engine!B9. Excel: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1).
"""
    shock_type_value = xl_cell(ctx, 'Inputs!B22')
    offset = xl_sub(shock_type_value, 1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, offset, None, None)


# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt', {'time_period': 1}),
    'Outputs!B13': ('debt_to_gdp', {'time_period': 1}),
    'Outputs!B14': ('output_delta', {'time_period': 1}),
    'Outputs!C12': ('baseline_debt', {'time_period': 2}),
    'Outputs!C13': ('debt_to_gdp', {'time_period': 2}),
    'Outputs!C14': ('output_delta', {'time_period': 2}),
    'Outputs!D12': ('baseline_debt', {'time_period': 3}),
    'Outputs!D13': ('debt_to_gdp', {'time_period': 3}),
    'Outputs!D14': ('output_delta', {'time_period': 3}),
    'Outputs!E12': ('baseline_debt', {'time_period': 4}),
    'Outputs!E13': ('debt_to_gdp', {'time_period': 4}),
    'Outputs!E14': ('output_delta', {'time_period': 4}),
    'Outputs!F12': ('baseline_debt', {'time_period': 5}),
    'Outputs!F13': ('debt_to_gdp', {'time_period': 5}),
    'Outputs!F14': ('output_delta', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'shock_magnitude_resolved',
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
