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

def output_delta(ctx, col):
    """Return the difference between shocked and baseline debt-to-GDP for a given column.

    Args:
        ctx: Workbook evaluation context.
        col: Engine column letter (C through G).

    Returns:
        The result of subtracting baseline debt from shocked debt.

    Note:
        Covers Outputs!B14:F14. Excel formula: =Engine!{col}20-Engine!{col}6.
"""
    return xl_sub(debt_to_gdp(ctx, col), baseline_debt(ctx, col))

def debt_to_gdp(ctx, col):
    """Return the shocked debt-to-GDP ratio for the given projection column.

    Args:
        ctx: Workbook evaluation context.
        col: Engine column letter (C through G).

    Returns:
        The computed debt-to-GDP ratio as a float or XlError.

    Note:
        Covers Engine!C20:G20. Excel: ={PRIOR_DEBT}*(1+(Inputs!{col}17+CHOOSE(Inputs!$B$22,0,$B$9,0)*{col}10)/100)/(1+(Inputs!{col}16+CHOOSE(Inputs!$B$22,$B$9,0,0)*{col}10)/100)-{col}16,
        where {PRIOR_DEBT} is Inputs!B6 for column C and Engine!{prev_col}20 otherwise.
"""
    if col == 'C':
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    elif col == 'D':
        prior_debt = debt_to_gdp(ctx, 'C')
    elif col == 'E':
        prior_debt = debt_to_gdp(ctx, 'D')
    elif col == 'F':
        prior_debt = debt_to_gdp(ctx, 'E')
    elif col == 'G':
        prior_debt = debt_to_gdp(ctx, 'F')
    else:
        raise ValueError(f'Unexpected column {col}')
    interest_baseline = xl_cell(ctx, f'Inputs!{col}17')
    growth_baseline = xl_cell(ctx, f'Inputs!{col}16')
    shock_type_raw = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_type_raw, XlError):
        return shock_type_raw
    shock_type_int = to_int(shock_type_raw)
    if isinstance(shock_type_int, XlError):
        return shock_type_int
    if shock_type_int < 1 or shock_type_int > 3:
        return XlError.VALUE
    if shock_type_int == 1:
        numerator_factor = 0.0
    elif shock_type_int == 2:
        numerator_factor = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
    else:
        numerator_factor = 0.0
    if shock_type_int == 1:
        denominator_factor = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
    elif shock_type_int == 2:
        denominator_factor = 0.0
    else:
        denominator_factor = 0.0
    shock_active_val = shock_active(ctx, col)
    primary_balance_shocked_val = primary_balance_shocked(ctx, col)
    numerator_part = xl_add(interest_baseline, xl_mul(numerator_factor, shock_active_val))
    numerator_div = xl_div(numerator_part, 100.0)
    numerator_term = xl_add(1.0, numerator_div)
    denominator_part = xl_add(growth_baseline, xl_mul(denominator_factor, shock_active_val))
    denominator_div = xl_div(denominator_part, 100.0)
    denominator_term = xl_add(1.0, denominator_div)
    ratio = xl_div(xl_mul(prior_debt, numerator_term), denominator_term)
    return xl_sub(ratio, primary_balance_shocked_val)

def baseline_debt(ctx, col):
    """Return the baseline debt-to-GDP ratio for the given projection column.

    Args:
        ctx: Workbook evaluation context.
        col: Engine column letter (C through G).

    Returns:
        Baseline debt-to-GDP ratio for the column.

    Note:
        Covers Engine!C6:G6. Excel: ={PRIOR_DEBT}*(1+Inputs!{col}17/100)/(1+Inputs!{col}16/100)-Inputs!{col}18,
        where {PRIOR_DEBT} is Inputs!B6 for col C and the previous column's Engine!{prev_col}6 otherwise.
"""
    if col == 'C':
        prior_debt = xl_cell(ctx, 'Inputs!B6')
    else:
        previous_col = chr(ord(col) - 1)
        prior_debt = baseline_debt(ctx, previous_col)
    growth_baseline = xl_cell(ctx, f'Inputs!{col}16')
    interest_baseline = xl_cell(ctx, f'Inputs!{col}17')
    primary_balance_baseline = xl_cell(ctx, f'Inputs!{col}18')
    growth_factor = xl_add(1.0, xl_div(growth_baseline, 100.0))
    interest_factor = xl_add(1.0, xl_div(interest_baseline, 100.0))
    debt_after_interest_and_growth = xl_div(xl_mul(prior_debt, interest_factor), growth_factor)
    new_debt = xl_sub(debt_after_interest_and_growth, primary_balance_baseline)
    return new_debt

def primary_balance_shocked(ctx, col):
    """Compute the primary balance including the shock effect for a given projection column.

    Args:
        ctx: Workbook evaluation context.
        col: Engine column letter (C through G).

    Returns:
        Primary balance after applying the shock if applicable.

    Note:
        Covers Engine!C16:G16. Excel: =Inputs!{col}18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!{col}10.
"""
    raw = xl_cell(ctx, 'Inputs!B22')
    if isinstance(raw, XlError):
        shock_factor = raw
    else:
        index = to_int(raw)
        if isinstance(index, XlError):
            shock_factor = index
        elif index < 1 or index > 3:
            shock_factor = XlError.VALUE
        elif index == 1:
            shock_factor = 0.0
        elif index == 2:
            shock_factor = 0.0
        else:
            shock_factor = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
    active = shock_active(ctx, col)
    shock_term = xl_mul(shock_factor, active)
    baseline = xl_cell(ctx, f'Inputs!{col}18')
    return xl_add(baseline, shock_term)

def shock_active(ctx, col):
    """Return 1.0 when the shock is active for the given projection column.

    Args:
        ctx: Workbook evaluation context.
        col: Engine column letter (C through G).

    Returns:
        1.0 if the projection year is at or after the shock year, else 0.0.

    Note:
        Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0).
"""
    comparison_result = xl_ge(xl_cell(ctx, f'Engine!{col}5'), xl_cell(ctx, 'Inputs!B21'))
    active_flag = to_bool(comparison_result)
    if isinstance(active_flag, XlError):
        return active_flag
    return 1.0 if active_flag else 0.0

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Initial debt-to-GDP ratio from the country profile table.

    Note:
        Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).
"""
    country_code = xl_cell(ctx, 'Inputs!B5')
    country_list = [xl_cell(ctx, 'Inputs!A10'), xl_cell(ctx, 'Inputs!A11'), xl_cell(ctx, 'Inputs!A12')]
    match_row = xl_match(country_code, country_list, 0.0)
    table_ref = ('Inputs', 10, 1, 12, 3)
    return xl_offset(ctx, xl_index_ref(table_ref, match_row, 2.0), 0.0, 0.0)

def shock_magnitude_resolved(ctx):
    """Retrieve the shock magnitude from the Inputs sheet based on the selected shock type.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        The shock magnitude value from row 26, column determined by shock_type.

    Note:
        Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type_raw = xl_cell(ctx, 'Inputs!B22')
    column_offset = xl_sub(shock_type_raw, 1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, column_offset, None, None)


# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B12': ('baseline_debt', 'C'),
    'Outputs!B13': ('debt_to_gdp', 'C'),
    'Outputs!B14': ('output_delta', 'C'),
    'Outputs!C12': ('baseline_debt', 'D'),
    'Outputs!C13': ('debt_to_gdp', 'D'),
    'Outputs!C14': ('output_delta', 'D'),
    'Outputs!D12': ('baseline_debt', 'E'),
    'Outputs!D13': ('debt_to_gdp', 'E'),
    'Outputs!D14': ('output_delta', 'E'),
    'Outputs!E12': ('baseline_debt', 'F'),
    'Outputs!E13': ('debt_to_gdp', 'F'),
    'Outputs!E14': ('output_delta', 'F'),
    'Outputs!F12': ('baseline_debt', 'G'),
    'Outputs!F13': ('debt_to_gdp', 'G'),
    'Outputs!F14': ('output_delta', 'G'),
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
        helper_name, column = dispatch
        helper = globals()[helper_name]

        def _bound(ctx, _helper=helper, _column=column):
            return _helper(ctx, _column)

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
