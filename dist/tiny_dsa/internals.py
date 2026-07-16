from __future__ import annotations

from ._readers import (
    read_country_name,
    read_growth_baseline,
    read_interest_baseline,
    read_primary_balance_baseline,
    read_shock_type,
    read_shock_year,
)
from .runtime import CellValue, EvalContext, XlError, xl_bool, xl_cell, xl_compare, xl_eval, xl_index_ref, xl_int, xl_match, xl_number, xl_offset, xl_raise, xl_range

# --- Formula cell functions ---

def output_delta(ctx: EvalContext, time_period: int) -> float:
    """Compute the difference between the shocked and baseline debt-to-GDP paths for a given projection period.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection period (1 through 5).

Returns:
    The shocked path value minus the baseline path value as a float.

Note:
    Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6.
"""
    return xl_number(shocked_path_internal(ctx, time_period=time_period)) - xl_number(baseline_path_internal(ctx, time_period=time_period))

def baseline_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Compute the baseline debt-to-GDP path for projection periods 1-5.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection period (1 through 5).

Returns:
    The debt-to-GDP ratio at the given period.

Note:
    Covers Engine!C6:G6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
"""
    prev = xl_number(initial_debt_resolved(ctx) if time_period == 1 else baseline_path_internal(ctx, time_period=time_period - 1))
    i = xl_number(read_interest_baseline(ctx, time_period=time_period))
    g = xl_number(read_growth_baseline(ctx, time_period=time_period))
    b = xl_number(read_primary_balance_baseline(ctx, time_period=time_period))
    return (prev * (1 + i / 100.0) / (1 + g / 100.0) if 1 + g / 100.0 != 0 else xl_raise(XlError.DIV)) - b

def shocked_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Recursively compute the shocked debt-to-GDP ratio for a given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year (1 through 5).

Returns:
    The shocked debt-to-GDP ratio for the given projection year as a float.

Note:
    Covers Engine!C20:G20. Excel: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16.
"""
    interest = xl_number(read_interest_baseline(ctx, time_period=time_period))
    growth = xl_number(read_growth_baseline(ctx, time_period=time_period))
    shock_type_val = xl_int(read_shock_type(ctx))
    if shock_type_val not in (1, 2, 3):
        xl_raise(XlError.VALUE)
    shock_mag = shock_magnitude_resolved(ctx)
    adj_num = {1: 0.0, 2: shock_mag, 3: 0.0}[shock_type_val]
    adj_den = {1: shock_mag, 2: 0.0, 3: 0.0}[shock_type_val]
    shock_act = shock_active(ctx, time_period=time_period)
    numerator = 1.0 + (interest + adj_num * shock_act) / 100.0
    denominator = 1.0 + (growth + adj_den * shock_act) / 100.0
    ratio = numerator / denominator if denominator != 0 else xl_raise(XlError.DIV)
    if time_period == 1:
        prev = xl_number(initial_debt_resolved(ctx))
    else:
        prev = shocked_path_internal(ctx, time_period=time_period - 1)
    result = prev * ratio - xl_number(shocked_primary_balance(ctx, time_period=time_period))
    return result

def shocked_primary_balance(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked primary balance by adding the shock impact to the baseline primary balance for the given time period.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection time period (1 through 5).

Returns:
    The shocked primary balance value as a percentage of GDP.

Note:
    Covers Engine!C16:G16. Excel: =Inputs!C18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!C10.
"""
    baseline = xl_number(read_primary_balance_baseline(ctx, time_period=time_period))
    shock_type_val = xl_int(read_shock_type(ctx))
    if shock_type_val < 1 or shock_type_val > 3:
        xl_raise(XlError.VALUE)
    if shock_type_val == 3:
        shock_term = shock_magnitude_resolved(ctx)
    else:
        shock_term = 0.0
    active = xl_number(shock_active(ctx, time_period=time_period))
    return baseline + shock_term * active

def shock_active(ctx: EvalContext, time_period: int) -> float:
    """Return 1.0 if the engine period value is greater than or equal to the shock year; otherwise 0.0.

Args:
    ctx: Workbook evaluation context.
    time_period: Time period index (1 through 5).

Returns:
    1.0 if the engine value meets or exceeds the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!C5>=Inputs!B21,1,0).
"""
    column_by_period = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    column = column_by_period[time_period]
    engine_value = xl_cell(ctx, f'Engine!{column}5')
    shock_year = read_shock_year(ctx)
    condition_met = xl_bool(xl_compare('>=', engine_value, shock_year))
    return 1.0 if condition_met else 0.0

def initial_debt_resolved(ctx: EvalContext) -> CellValue:
    """Lookup the resolved initial debt-to-GDP ratio for the selected country from the country profile table.

Args:
    ctx: Workbook evaluation context.

Returns:
    The initial debt-to-GDP ratio as a CellValue.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:C12,MATCH(Inputs!B5,Inputs!A10:A12,0),2).
"""
    country_name = read_country_name(ctx)
    country_column = xl_range(ctx, 'Inputs!A10:A12')
    row_number = xl_match(country_name, country_column, 0.0)
    lookup_table_ref = ('Inputs', 10, 1, 12, 3)
    index_ref = xl_index_ref(lookup_table_ref, row_number, 2.0)
    return xl_offset(ctx, index_ref, 0.0, 0.0)

def shock_magnitude_resolved(ctx: EvalContext) -> CellValue:
    """Resolve the shock magnitude (in percentage points) based on the selected shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude resolved from the Inputs table.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type = xl_number(read_shock_type(ctx))
    offset_cols = shock_type - 1.0
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, offset_cols, None, None)

# --- Projection public address aliases ---

def cell_engine_b20(ctx):
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), xl_match(read_country_name(ctx), xl_range(ctx, 'Inputs!A10:A12'), 0.0), 2.0), 0.0, 0.0)

def cell_engine_b6(ctx):
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), xl_match(read_country_name(ctx), xl_range(ctx, 'Inputs!A10:A12'), 0.0), 2.0), 0.0, 0.0)

def cell_outputs_b12(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(initial_debt_resolved(ctx)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_interest_baseline(ctx, time_period=1)), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_growth_baseline(ctx, time_period=1)), xl_number(100.0))))))))) - xl_number(read_primary_balance_baseline(ctx, time_period=1)))

def cell_outputs_b13(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(initial_debt_resolved(ctx)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_interest_baseline(ctx, time_period=1)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t1 := xl_int(read_shock_type(ctx))) < 1 or _t1 > 3 else ((0.0) if _t1 == 1 else (((shock_magnitude_resolved(ctx)) if _t1 == 2 else (((0.0) if _t1 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=1)))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_growth_baseline(ctx, time_period=1)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t2 := xl_int(read_shock_type(ctx))) < 1 or _t2 > 3 else ((shock_magnitude_resolved(ctx)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=1)))))), xl_number(100.0))))))))) - xl_number(shocked_primary_balance(ctx, time_period=1)))

def cell_outputs_c12(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(baseline_path_internal(ctx, time_period=1)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_interest_baseline(ctx, time_period=2)), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_growth_baseline(ctx, time_period=2)), xl_number(100.0))))))))) - xl_number(read_primary_balance_baseline(ctx, time_period=2)))

def cell_outputs_c13(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(shocked_path_internal(ctx, time_period=1)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_interest_baseline(ctx, time_period=2)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t3 := xl_int(read_shock_type(ctx))) < 1 or _t3 > 3 else ((0.0) if _t3 == 1 else (((shock_magnitude_resolved(ctx)) if _t3 == 2 else (((0.0) if _t3 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=2)))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_growth_baseline(ctx, time_period=2)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t4 := xl_int(read_shock_type(ctx))) < 1 or _t4 > 3 else ((shock_magnitude_resolved(ctx)) if _t4 == 1 else (((0.0) if _t4 == 2 else (((0.0) if _t4 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=2)))))), xl_number(100.0))))))))) - xl_number(shocked_primary_balance(ctx, time_period=2)))

def cell_outputs_d12(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(baseline_path_internal(ctx, time_period=2)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_interest_baseline(ctx, time_period=3)), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_growth_baseline(ctx, time_period=3)), xl_number(100.0))))))))) - xl_number(read_primary_balance_baseline(ctx, time_period=3)))

def cell_outputs_d13(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(shocked_path_internal(ctx, time_period=2)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_interest_baseline(ctx, time_period=3)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t5 := xl_int(read_shock_type(ctx))) < 1 or _t5 > 3 else ((0.0) if _t5 == 1 else (((shock_magnitude_resolved(ctx)) if _t5 == 2 else (((0.0) if _t5 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=3)))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_growth_baseline(ctx, time_period=3)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t6 := xl_int(read_shock_type(ctx))) < 1 or _t6 > 3 else ((shock_magnitude_resolved(ctx)) if _t6 == 1 else (((0.0) if _t6 == 2 else (((0.0) if _t6 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=3)))))), xl_number(100.0))))))))) - xl_number(shocked_primary_balance(ctx, time_period=3)))

def cell_outputs_e12(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(baseline_path_internal(ctx, time_period=3)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_interest_baseline(ctx, time_period=4)), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_growth_baseline(ctx, time_period=4)), xl_number(100.0))))))))) - xl_number(read_primary_balance_baseline(ctx, time_period=4)))

def cell_outputs_e13(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(shocked_path_internal(ctx, time_period=3)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_interest_baseline(ctx, time_period=4)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t7 := xl_int(read_shock_type(ctx))) < 1 or _t7 > 3 else ((0.0) if _t7 == 1 else (((shock_magnitude_resolved(ctx)) if _t7 == 2 else (((0.0) if _t7 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=4)))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_growth_baseline(ctx, time_period=4)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t8 := xl_int(read_shock_type(ctx))) < 1 or _t8 > 3 else ((shock_magnitude_resolved(ctx)) if _t8 == 1 else (((0.0) if _t8 == 2 else (((0.0) if _t8 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=4)))))), xl_number(100.0))))))))) - xl_number(shocked_primary_balance(ctx, time_period=4)))

def cell_outputs_f12(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(baseline_path_internal(ctx, time_period=4)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_interest_baseline(ctx, time_period=5)), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(read_growth_baseline(ctx, time_period=5)), xl_number(100.0))))))))) - xl_number(read_primary_balance_baseline(ctx, time_period=5)))

def cell_outputs_f13(ctx):
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(shocked_path_internal(ctx, time_period=4)) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_interest_baseline(ctx, time_period=5)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t9 := xl_int(read_shock_type(ctx))) < 1 or _t9 > 3 else ((0.0) if _t9 == 1 else (((shock_magnitude_resolved(ctx)) if _t9 == 2 else (((0.0) if _t9 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=5)))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(read_growth_baseline(ctx, time_period=5)) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t10 := xl_int(read_shock_type(ctx))) < 1 or _t10 > 3 else ((shock_magnitude_resolved(ctx)) if _t10 == 1 else (((0.0) if _t10 == 2 else (((0.0) if _t10 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(shock_active(ctx, time_period=5)))))), xl_number(100.0))))))))) - xl_number(shocked_primary_balance(ctx, time_period=5)))

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {
    'Outputs!B14': ('output_delta', {'time_period': 1}),
    'Outputs!C14': ('output_delta', {'time_period': 2}),
    'Outputs!D14': ('output_delta', {'time_period': 3}),
    'Outputs!E14': ('output_delta', {'time_period': 4}),
    'Outputs!F14': ('output_delta', {'time_period': 5}),
}
_SYMBOL_DISPATCH = {
    'Engine!B9': 'shock_magnitude_resolved',
    'Inputs!B6': 'initial_debt_resolved',
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
