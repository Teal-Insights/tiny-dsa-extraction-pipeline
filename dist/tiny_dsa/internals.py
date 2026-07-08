from __future__ import annotations

from .runtime import EvalContext, XlError, xl_bool, xl_cell, xl_compare, xl_eval, xl_index_ref, xl_int, xl_match, xl_number, xl_offset, xl_raise, xl_range

# --- Formula cell functions ---

def debt_to_gdp_shock_deviation(ctx: EvalContext, time_period: int) -> float:
    """Compute the difference between shocked and baseline/projected debt-to-GDP for a given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    Difference between shocked and baseline/projected debt-to-GDP as a float.

Note:
    Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6.
"""
    if time_period == 1:
        shocked = xl_number(shocked_debt_to_gdp_period1(ctx))
        baseline = xl_number(baseline_debt_to_gdp(ctx))
    else:
        shocked = xl_number(shocked_debt_to_gdp(ctx, time_period=time_period))
        baseline = xl_number(projected_debt_to_gdp(ctx, time_period=time_period))
    return shocked - baseline

def projected_debt_to_gdp(ctx: EvalContext, time_period: int) -> float:
    """Compute projected debt-to-GDP ratio for a given time period in the baseline path.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (2 through 5).

Returns:
    Debt-to-GDP ratio as a percentage.

Note:
    Covers Engine!D6:G6. Excel: =Engine!C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18.
"""
    if time_period == 2:
        prior = baseline_debt_to_gdp(ctx)
    else:
        prior = projected_debt_to_gdp(ctx, time_period - 1)
    column_by_period = {2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = column_by_period[time_period]
    rate_17 = xl_number(xl_cell(ctx, f'Inputs!{col}17')) / 100.0
    rate_16 = xl_number(xl_cell(ctx, f'Inputs!{col}16')) / 100.0
    deduction = xl_number(xl_cell(ctx, f'Inputs!{col}18'))
    denominator = 1.0 + rate_16
    if denominator == 0.0:
        xl_raise(XlError.DIV)
    return prior * (1.0 + rate_17) / denominator - deduction

def shocked_debt_to_gdp(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked debt-to-GDP percentage for a given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: The time period index (1–5).

Returns:
    Shocked debt-to-GDP percentage as a float.

Note:
    Covers Engine!D20:G20. Excel: =Engine!C20*(1+(Inputs!D17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!D10)/100)/(1+(Inputs!D16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!D10)/100)-Engine!D16.
"""
    if time_period == 1:
        return shocked_debt_to_gdp_period1(ctx)
    prior_period_debt = shocked_debt_to_gdp(ctx, time_period - 1)
    shock_type = xl_int(xl_cell(ctx, 'Inputs!B22'))
    magnitude = shock_magnitude_pp(ctx)
    active = xl_number(shock_active(ctx, time_period=time_period))
    adj_numerator = magnitude if shock_type == 2 else 0.0
    adj_denominator = magnitude if shock_type == 1 else 0.0
    column_by_period = {2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = column_by_period[time_period]
    rate_n = xl_number(xl_cell(ctx, f'Inputs!{col}17'))
    rate_d = xl_number(xl_cell(ctx, f'Inputs!{col}16'))
    numerator_factor = 1.0 + (rate_n + adj_numerator * active) / 100.0
    denominator_factor = 1.0 + (rate_d + adj_denominator * active) / 100.0
    if denominator_factor == 0.0:
        return xl_raise(XlError.DIV)
    result = xl_number(prior_period_debt) * numerator_factor / denominator_factor - xl_number(primary_balance_shocked(ctx, time_period=time_period))
    return xl_number(result)

def primary_balance_shocked(ctx: EvalContext, time_period: int) -> float:
    """Calculate primary balance as a percentage of GDP after applying shocks.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    Shocked primary balance (% of GDP) as a float.

Note:
    Covers Engine!C16:G16. Excel: =Inputs!C18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!C10.
"""
    column_by_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_period[time_period]
    base_value = xl_number(xl_cell(ctx, f'Inputs!{column}18'))
    shock_type = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if shock_type < 1 or shock_type > 3:
        xl_raise(XlError.VALUE)
    elif shock_type == 1:
        shock_magnitude = 0.0
    elif shock_type == 2:
        shock_magnitude = 0.0
    else:
        shock_magnitude = shock_magnitude_pp(ctx)
    shock_effect = shock_magnitude * shock_active(ctx, time_period=time_period)
    return base_value + xl_number(shock_effect)

def shock_active(ctx: EvalContext, time_period: int) -> float:
    """Return 1.0 if the shock is active for the given time period, else 0.0.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    1.0 if the base value is at or above the shock threshold, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!C5>=Inputs!B21,1,0).
"""
    period_column = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = period_column[time_period]
    base_for_period = xl_cell(ctx, f'Engine!{col}5')
    shock_threshold = xl_cell(ctx, 'Inputs!B21')
    meets_threshold = xl_compare('>=', base_for_period, shock_threshold)
    return 1.0 if meets_threshold else 0.0

def borvelia_initial_debt_to_gdp(ctx: EvalContext) -> float:
    """Borvelia's initial debt-to-GDP percentage, looked up from the COUNTRY SELECTOR table based on the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP for Borvelia.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:Inputs!C12,MATCH(Inputs!B5,Inputs!A10:Inputs!A12,0),2).
"""
    selected_country = xl_cell(ctx, 'Inputs!B5')
    country_list = xl_range(ctx, 'Inputs!A10:Inputs!A12')
    row_num = xl_match(selected_country, country_list, 0.0)
    table_ref = ('Inputs', 10, 1, 12, 3)
    index_ref = xl_index_ref(table_ref, row_num, 2.0)
    return xl_offset(ctx, index_ref, 0.0, 0.0)

def baseline_debt_to_gdp(ctx: EvalContext) -> float:
    """Baseline debt-to-GDP ratio for period 1.

Args:
    ctx: Workbook evaluation context.

Returns:
    Debt-to-GDP ratio as a percentage.

Note:
    Covers Engine!C6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
"""
    initial_debt = xl_number(borvelia_initial_debt_to_gdp(ctx))
    rate_num = xl_number(xl_cell(ctx, 'Inputs!C17'))
    hundred = 100.0
    if hundred == 0:
        xl_raise(XlError.DIV)
    rate_num_adjusted = rate_num / hundred
    rate_den = xl_number(xl_cell(ctx, 'Inputs!C16'))
    if hundred == 0:
        xl_raise(XlError.DIV)
    rate_den_adjusted = rate_den / hundred
    factor_num = 1.0 + rate_num_adjusted
    factor_den = 1.0 + rate_den_adjusted
    numerator = initial_debt * factor_num
    if factor_den == 0:
        xl_raise(XlError.DIV)
    ratio = numerator / factor_den
    constant = xl_number(xl_cell(ctx, 'Inputs!C18'))
    return ratio - constant

def shock_magnitude_pp(ctx: EvalContext) -> float:
    """Shock magnitude in percentage points, retrieved dynamically using OFFSET based on the shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude in percentage points.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type = xl_number(xl_cell(ctx, 'Inputs!B22'))
    col_offset = shock_type - 1.0
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, col_offset, None, None)

def shocked_debt_to_gdp_period1(ctx: EvalContext) -> float:
    """Calculate the shocked debt-to-GDP percentage for period 1.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shocked debt-to-GDP percentage for period 1 as a float.

Note:
    Covers Engine!C20. Excel: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16.
"""
    scenario = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if scenario == 1:
        shock_term_numerator = 0.0
        shock_term_denominator = shock_magnitude_pp(ctx)
    elif scenario == 2:
        shock_term_numerator = shock_magnitude_pp(ctx)
        shock_term_denominator = 0.0
    elif scenario == 3:
        shock_term_numerator = 0.0
        shock_term_denominator = 0.0
    else:
        xl_raise(XlError.VALUE)
    shock_flag = shock_active(ctx, time_period=1)
    numerator_base = xl_number(xl_cell(ctx, 'Inputs!C17'))
    denominator_base = xl_number(xl_cell(ctx, 'Inputs!C16'))
    numerator = numerator_base + shock_flag * shock_term_numerator
    denominator = denominator_base + shock_flag * shock_term_denominator
    initial_debt = borvelia_initial_debt_to_gdp(ctx)
    growth_factor = 1 + numerator / 100.0
    discount_factor = 1 + denominator / 100.0
    if discount_factor == 0.0:
        xl_raise(XlError.DIV)
    shocked_debt = initial_debt * growth_factor / discount_factor
    primary_balance = primary_balance_shocked(ctx, time_period=1)
    return shocked_debt - primary_balance

# --- Projection public address aliases ---

def cell_outputs_b12(ctx):
    return baseline_debt_to_gdp(ctx)

def cell_outputs_b13(ctx):
    return shocked_debt_to_gdp_period1(ctx)

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B14': ('debt_to_gdp_shock_deviation', {'time_period': 1}),
    'Outputs!C12': ('projected_debt_to_gdp', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp', {'time_period': 2}),
    'Outputs!C14': ('debt_to_gdp_shock_deviation', {'time_period': 2}),
    'Outputs!D12': ('projected_debt_to_gdp', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp', {'time_period': 3}),
    'Outputs!D14': ('debt_to_gdp_shock_deviation', {'time_period': 3}),
    'Outputs!E12': ('projected_debt_to_gdp', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp', {'time_period': 4}),
    'Outputs!E14': ('debt_to_gdp_shock_deviation', {'time_period': 4}),
    'Outputs!F12': ('projected_debt_to_gdp', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp', {'time_period': 5}),
    'Outputs!F14': ('debt_to_gdp_shock_deviation', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'shock_magnitude_pp',
    'Engine!C20': 'shocked_debt_to_gdp_period1',
    'Engine!C6': 'baseline_debt_to_gdp',
    'Inputs!B6': 'borvelia_initial_debt_to_gdp',
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
