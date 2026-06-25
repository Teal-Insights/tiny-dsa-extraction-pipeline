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

def output_delta(ctx, time_period: int):
    """Return the debt-to-GDP delta between shocked and baseline paths.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Shocked debt-to-GDP minus baseline debt-to-GDP for the year.

Note:
    Covers Outputs!B14:F14."""
    return xl_sub(
        debt_to_gdp(ctx, time_period=time_period),
        baseline_debt(ctx, time_period=time_period),
    )

def baseline_debt(ctx, time_period: int):
    """Return the baseline debt-to-GDP ratio for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Recursed baseline debt-to-GDP ratio for the year.

Note:
    Covers Engine!C6:G6."""
    if time_period == 1:
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        prior_debt = baseline_debt(ctx, time_period=time_period - 1)
    column = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}[time_period]
    growth_rate = xl_cell(ctx, f'Inputs!{column}17')
    interest_rate = xl_cell(ctx, f'Inputs!{column}16')
    primary_balance = xl_cell(ctx, f'Inputs!{column}18')
    growth_term = xl_add(1.0, xl_div(growth_rate, 100.0))
    interest_term = xl_add(1.0, xl_div(interest_rate, 100.0))
    scaled_debt = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(scaled_debt, primary_balance)

def debt_to_gdp(ctx, time_period: int):
    """Return the shocked debt-to-GDP ratio for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Recursed shocked debt-to-GDP ratio for the year.

Note:
    Covers Engine!C20:G20."""
    if time_period == 1:
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        prior_debt = debt_to_gdp(ctx, time_period=time_period - 1)
    column = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}[time_period]
    shock_type = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_type, XlError):
        growth_shock_factor = shock_type
        interest_shock_factor = shock_type
    else:
        shock_type_index = to_int(shock_type)
        if isinstance(shock_type_index, XlError):
            growth_shock_factor = shock_type_index
            interest_shock_factor = shock_type_index
        elif shock_type_index < 1 or shock_type_index > 3:
            growth_shock_factor = XlError.VALUE
            interest_shock_factor = XlError.VALUE
        else:
            shock_magnitude = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
            if shock_type_index == 1:
                growth_shock_factor = 0.0
                interest_shock_factor = shock_magnitude
            elif shock_type_index == 2:
                growth_shock_factor = shock_magnitude
                interest_shock_factor = 0.0
            else:
                growth_shock_factor = 0.0
                interest_shock_factor = 0.0
    growth_rate = xl_cell(ctx, f'Inputs!{column}17')
    interest_rate = xl_cell(ctx, f'Inputs!{column}16')
    shock_activation = shock_active(ctx, time_period=time_period)
    shocked_primary_balance = primary_balance_shocked(ctx, time_period=time_period)
    growth_numerator = xl_add(growth_rate, xl_mul(growth_shock_factor, shock_activation))
    growth_term = xl_add(1.0, xl_div(growth_numerator, 100.0))
    interest_denominator = xl_add(interest_rate, xl_mul(interest_shock_factor, shock_activation))
    interest_term = xl_add(1.0, xl_div(interest_denominator, 100.0))
    debt_ratio = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(debt_ratio, shocked_primary_balance)

def primary_balance_shocked(ctx, time_period: int):
    """Return the shocked primary balance for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Baseline primary balance plus the shock adjustment for the year.

Note:
    Covers Engine!C16:G16."""
    column = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}[time_period]
    shock_type = xl_cell(ctx, 'Inputs!B22')
    if isinstance(shock_type, XlError):
        shock_multiplier = shock_type
    else:
        shock_type_index = to_int(shock_type)
        if isinstance(shock_type_index, XlError):
            shock_multiplier = shock_type_index
        elif shock_type_index < 1 or shock_type_index > 3:
            shock_multiplier = XlError.VALUE
        elif shock_type_index == 1 or shock_type_index == 2:
            shock_multiplier = 0.0
        else:
            shock_multiplier = xl_eval(ctx, 'Engine!B9', shock_magnitude_resolved)
    baseline_primary_balance = xl_cell(ctx, f'Inputs!{column}18')
    shock_adjustment = xl_mul(shock_multiplier, shock_active(ctx, time_period=time_period))
    return xl_add(baseline_primary_balance, shock_adjustment)

def shock_active(ctx, time_period: int):
    """Return 1.0 when the shock is active for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    1.0 if the projection year is at or after the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0)."""
    column = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}[time_period]
    projection_year = xl_cell(ctx, f'Engine!{column}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    year_at_or_after_shock = xl_ge(projection_year, shock_year)
    is_active = to_bool(year_at_or_after_shock)
    if isinstance(is_active, XlError):
        return is_active
    return 1.0 if is_active else 0.0

def initial_debt_to_gdp(ctx):
    """Look up the initial debt-to-GDP ratio for the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio from the country profile table.

Note:
    Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2)."""
    country_name = xl_cell(ctx, 'Inputs!B5')
    country_codes = np.array(
        [
            [xl_cell(ctx, 'Inputs!A10')],
            [xl_cell(ctx, 'Inputs!A11')],
            [xl_cell(ctx, 'Inputs!A12')],
        ],
        dtype=object,
    )
    match_index = xl_match(
        country_name,
        np.array(country_codes, dtype=object),
        0.0,
    )
    profile_table = ('Inputs', 10, 1, 12, 3)
    return xl_offset(
        ctx,
        xl_index_ref(profile_table, match_index, 2.0),
        0.0,
        0.0,
    )

def shock_magnitude_resolved(ctx):
    """Resolve the shock magnitude for the selected shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude from the shock table for Inputs!B22.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1)."""
    shock_type = xl_cell(ctx, 'Inputs!B22')
    type_offset = xl_sub(shock_type, 1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, type_offset, None, None)


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
