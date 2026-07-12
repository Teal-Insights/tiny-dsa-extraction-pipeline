from __future__ import annotations

from .runtime import EvalContext, XlError, xl_bool, xl_cell, xl_compare, xl_eval, xl_index_ref, xl_int, xl_match, xl_number, xl_offset, xl_raise, xl_range

# --- Formula cell functions ---

def shock_impact_on_debt_to_gdp(ctx: EvalContext, time_period: int) -> float:
    """Compute the difference between the shocked and baseline debt-to-GDP ratios for a given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    The shocked ratio minus the baseline ratio, as a percentage of GDP.

Note:
    Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6.
"""
    if time_period == 1:
        shocked = xl_number(shocked_debt_to_gdp_period_1(ctx))
        baseline = xl_number(projected_debt_to_gdp(ctx))
    else:
        shocked = xl_number(shocked_debt_to_gdp(ctx, time_period=time_period))
        baseline = xl_number(compute_debt_to_gdp_ratio(ctx, time_period=time_period))
    return shocked - baseline

def compute_debt_to_gdp_ratio(ctx: EvalContext, time_period: int) -> float:
    """Compute the projected debt-to-GDP ratio for the given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Reporting period index (2 through 5). Period 1 is the initial value
        obtained from projected_debt_to_gdp.

Returns:
    Debt-to-GDP ratio as a percentage of GDP.

Note:
    Covers Engine!D6:G6. Excel: =Engine!C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18.
"""
    column_by_period = {2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    value = projected_debt_to_gdp(ctx)
    for period in range(2, time_period + 1):
        col = column_by_period[period]
        interest_rate = xl_number(xl_cell(ctx, f'Inputs!{col}17')) / 100.0
        nominal_growth = xl_number(xl_cell(ctx, f'Inputs!{col}16')) / 100.0
        primary_balance = xl_number(xl_cell(ctx, f'Inputs!{col}18'))
        growth_factor = 1.0 + nominal_growth
        value = (value * (1.0 + interest_rate) / growth_factor if growth_factor != 0.0 else xl_raise(XlError.DIV)) - primary_balance
    return value

def shocked_debt_to_gdp(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked debt-to-GDP ratio for a given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (2 through 5).

Returns:
    The debt-to-GDP ratio as a percentage of GDP.

Note:
    Covers Engine!D20:G20. Excel: =Engine!C20*(1+(Inputs!D17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!D10)/100)/(1+(Inputs!D16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!D10)/100)-Engine!D16.
"""
    if time_period == 2:
        prior = shocked_debt_to_gdp_period_1(ctx)
    else:
        prior = shocked_debt_to_gdp(ctx, time_period - 1)
    col_by_period = {2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = col_by_period[time_period]
    rate_interest = xl_number(xl_cell(ctx, f'Inputs!{col}17'))
    rate_growth = xl_number(xl_cell(ctx, f'Inputs!{col}16'))
    scenario = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if scenario == 1:
        shock_interest = 0.0
        shock_growth = resolve_shock_magnitude(ctx)
    elif scenario == 2:
        shock_interest = resolve_shock_magnitude(ctx)
        shock_growth = 0.0
    elif scenario == 3:
        shock_interest = 0.0
        shock_growth = 0.0
    else:
        xl_raise(XlError.VALUE)
    shock_active_flag = shock_active(ctx, time_period=time_period)
    shock_interest_term = shock_interest * shock_active_flag
    shock_growth_term = shock_growth * shock_active_flag
    numerator = prior * (1.0 + (rate_interest + shock_interest_term) / 100.0)
    denominator = 1.0 + (rate_growth + shock_growth_term) / 100.0
    if denominator == 0.0:
        xl_raise(XlError.DIV)
    ratio = numerator / denominator
    primary_balance = shocked_primary_balance(ctx, time_period=time_period)
    return ratio - primary_balance

def shocked_primary_balance(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked primary balance for a given time period, combining a base value with a conditional shock adjustment.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    The shocked primary balance as a float.

Note:
    Covers Engine!C16:G16. Excel: =Inputs!C18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!C10.
"""
    column_map = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    col = column_map[time_period]
    base = xl_number(xl_cell(ctx, f'Inputs!{col}18'))
    choice_idx = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if choice_idx < 1 or choice_idx > 3:
        xl_raise(XlError.VALUE)
    if choice_idx == 1 or choice_idx == 2:
        magnitude = 0.0
    elif choice_idx == 3:
        magnitude = xl_number(resolve_shock_magnitude(ctx))
    else:
        xl_raise(XlError.VALUE)
    shock_term = xl_number(magnitude * xl_number(shock_active(ctx, time_period=time_period)))
    return base + shock_term

def shock_active(ctx: EvalContext, time_period: int) -> float:
    """Return 1.0 when the shock is active for the given time period, 0.0 otherwise.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    1.0 if the observed value meets or exceeds the threshold, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!C5>=Inputs!B21,1,0).
"""
    column_by_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_period[time_period]
    observed_value = xl_cell(ctx, f'Engine!{column}5')
    threshold = xl_cell(ctx, 'Inputs!B21')
    meets_threshold = xl_compare('>=', observed_value, threshold)
    return 1.0 if meets_threshold else 0.0

def initial_debt_to_gdp(ctx: EvalContext) -> float:
    """Retrieve the initial debt-to-GDP ratio for the selected scenario.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio as a percentage of GDP.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:Inputs!C12,MATCH(Inputs!B5,Inputs!A10:Inputs!A12,0),2).
"""
    selected_scenario = xl_cell(ctx, 'Inputs!B5')
    scenario_list = xl_range(ctx, 'Inputs!A10:Inputs!A12')
    match_row = xl_match(selected_scenario, scenario_list, 0.0)
    debt_ratio_table = ('Inputs', 10, 1, 12, 3)
    debt_ratio_ref = xl_index_ref(debt_ratio_table, match_row, 2.0)
    initial_debt_to_gdp_value = xl_offset(ctx, debt_ratio_ref, 0.0, 0.0)
    return initial_debt_to_gdp_value

def projected_debt_to_gdp(ctx: EvalContext) -> float:
    """Projected debt-to-GDP ratio for the baseline scenario.

Args:
    ctx: Workbook evaluation context.

Returns:
    Projected debt-to-GDP ratio as a percentage of GDP.

Note:
    Covers Engine!C6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
"""
    initial_debt = xl_number(initial_debt_to_gdp(ctx))
    c16 = xl_cell(ctx, 'Inputs!C16')
    c17 = xl_cell(ctx, 'Inputs!C17')
    c18 = xl_cell(ctx, 'Inputs!C18')
    interest_adjustment = 1.0 + xl_number(c17) / 100.0
    growth_adjustment = 1.0 + xl_number(c16) / 100.0
    adjusted_debt = initial_debt * interest_adjustment
    ratio = adjusted_debt / growth_adjustment if growth_adjustment != 0.0 else xl_raise(XlError.DIV)
    return ratio - xl_number(c18)

def resolve_shock_magnitude(ctx: EvalContext) -> float:
    """Resolve the shock magnitude for the current time period.

Args:
    ctx: Workbook evaluation context.

Returns:
    The resolved shock magnitude as a float.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    time_period = xl_number(xl_cell(ctx, 'Inputs!B22'))
    column_offset = time_period - 1.0
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, column_offset, None, None)

def shocked_debt_to_gdp_period_1(ctx: EvalContext) -> float:
    """Compute the debt-to-GDP ratio for time period 1 under a shocked scenario.

Args:
    ctx: Workbook evaluation context.

Returns:
    Debt-to-GDP ratio as a percentage of GDP.

Note:
    Covers Engine!C20. Excel: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16.
"""
    base_interest_rate = xl_number(xl_cell(ctx, 'Inputs!C17'))
    base_growth_rate = xl_number(xl_cell(ctx, 'Inputs!C16'))
    scenario = xl_int(xl_cell(ctx, 'Inputs!B22'))
    if scenario < 1 or scenario > 3:
        xl_raise(XlError.VALUE)
    shock_active_flag = xl_number(shock_active(ctx, time_period=1))
    if scenario == 1:
        interest_shock = 0.0
        growth_shock = xl_number(resolve_shock_magnitude(ctx))
    elif scenario == 2:
        interest_shock = xl_number(resolve_shock_magnitude(ctx))
        growth_shock = 0.0
    else:
        interest_shock = 0.0
        growth_shock = 0.0
    interest_adjustment = (base_interest_rate + interest_shock * shock_active_flag) / 100.0
    growth_adjustment = (base_growth_rate + growth_shock * shock_active_flag) / 100.0
    denominator_factor = 1.0 + growth_adjustment
    if denominator_factor == 0.0:
        xl_raise(XlError.DIV)
    numerator_factor = 1.0 + interest_adjustment
    initial_debt = xl_number(initial_debt_to_gdp(ctx))
    debt_before_primary = initial_debt * numerator_factor / denominator_factor
    primary_balance = xl_number(shocked_primary_balance(ctx, time_period=1))
    return debt_before_primary - primary_balance

# --- Projection public address aliases ---

def cell_engine_b20(ctx):
    return initial_debt_to_gdp(ctx)

def cell_engine_b6(ctx):
    return initial_debt_to_gdp(ctx)

def cell_outputs_b12(ctx):
    return projected_debt_to_gdp(ctx)

def cell_outputs_b13(ctx):
    return shocked_debt_to_gdp_period_1(ctx)

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B14': ('shock_impact_on_debt_to_gdp', {'time_period': 1}),
    'Outputs!C12': ('compute_debt_to_gdp_ratio', {'time_period': 2}),
    'Outputs!C13': ('shocked_debt_to_gdp', {'time_period': 2}),
    'Outputs!C14': ('shock_impact_on_debt_to_gdp', {'time_period': 2}),
    'Outputs!D12': ('compute_debt_to_gdp_ratio', {'time_period': 3}),
    'Outputs!D13': ('shocked_debt_to_gdp', {'time_period': 3}),
    'Outputs!D14': ('shock_impact_on_debt_to_gdp', {'time_period': 3}),
    'Outputs!E12': ('compute_debt_to_gdp_ratio', {'time_period': 4}),
    'Outputs!E13': ('shocked_debt_to_gdp', {'time_period': 4}),
    'Outputs!E14': ('shock_impact_on_debt_to_gdp', {'time_period': 4}),
    'Outputs!F12': ('compute_debt_to_gdp_ratio', {'time_period': 5}),
    'Outputs!F13': ('shocked_debt_to_gdp', {'time_period': 5}),
    'Outputs!F14': ('shock_impact_on_debt_to_gdp', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'resolve_shock_magnitude',
    'Engine!C20': 'shocked_debt_to_gdp_period_1',
    'Engine!C6': 'projected_debt_to_gdp',
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
