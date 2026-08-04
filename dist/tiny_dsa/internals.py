from __future__ import annotations

from ._readers import (
    read_country_name,
    read_growth_baseline,
    read_interest_baseline,
    read_primary_balance_baseline,
    read_shock_type,
    read_shock_year,
)
from .runtime import CellValue, EvalContext, XlError, xl_bool, xl_cell, xl_compare, xl_eval, xl_index_ref, xl_int, xl_match, xl_memoize, xl_number, xl_offset, xl_raise, xl_range

# --- Formula cell functions ---

@xl_memoize
def output_delta(ctx: EvalContext, time_period: int) -> float:
    """Compute the delta between the shocked and baseline internal paths at a given time period.

Args:
    time_period: The projection time period (int, observed range 1..5).

Returns:
    float: The difference between the shocked path value and the baseline path value.

Note:
    Covers Outputs!B14:F14. Excel: =Engine!C20-Engine!C6.
"""
    shocked_value = shocked_path_internal(ctx, time_period=time_period)
    baseline_value = baseline_path_internal(ctx, time_period=time_period)
    return xl_number(shocked_value) - xl_number(baseline_value)


@xl_memoize
def shocked_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Compute the internally shocked debt path for projection periods 1–5.

Args:
    ctx (EvalContext): Workbook evaluation context.
    time_period (int): Projection year (1..5).

Returns:
    float: Shocked debt stock for the given projection year, in the same units as the initial debt.

Note:
    Covers Engine!C20:G20. Excel: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!B22,0,Engine!B9,0)*Engine!C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!B22,Engine!B9,0,0)*Engine!C10)/100)-Engine!C16.
"""
    if time_period == 1:
        initial_debt = initial_debt_resolved(ctx)
        initial_interest_baseline = read_interest_baseline(ctx, time_period=1)
        initial_shock_type_interest = read_shock_type(ctx)
        initial_shock_active_interest = shock_active(ctx, time_period=1)
        initial_growth_baseline = read_growth_baseline(ctx, time_period=1)
        initial_shock_type_growth = read_shock_type(ctx)
        initial_shock_active_growth = shock_active(ctx, time_period=1)
        initial_primary_balance = shocked_primary_balance(ctx, time_period=1)
        return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_interest_baseline) + xl_number(xl_number(xl_raise(XlError.VALUE) if (initial_shock_type_int_interest := xl_int(initial_shock_type_interest)) < 1 or initial_shock_type_int_interest > 3 else 0.0 if initial_shock_type_int_interest == 1 else shock_magnitude_resolved(ctx) if initial_shock_type_int_interest == 2 else 0.0 if initial_shock_type_int_interest == 3 else xl_raise(XlError.VALUE)) * xl_number(initial_shock_active_interest))), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_growth_baseline) + xl_number(xl_number(xl_raise(XlError.VALUE) if (initial_shock_type_int_growth := xl_int(initial_shock_type_growth)) < 1 or initial_shock_type_int_growth > 3 else shock_magnitude_resolved(ctx) if initial_shock_type_int_growth == 1 else 0.0 if initial_shock_type_int_growth == 2 else 0.0 if initial_shock_type_int_growth == 3 else xl_raise(XlError.VALUE)) * xl_number(initial_shock_active_growth))), xl_number(100.0)))))) - xl_number(initial_primary_balance)
    previous_debt = shocked_path_internal(ctx, time_period=time_period - 1)
    interest_baseline = read_interest_baseline(ctx, time_period=time_period)
    shock_type_interest = read_shock_type(ctx)
    shock_active_interest = shock_active(ctx, time_period=time_period)
    growth_baseline = read_growth_baseline(ctx, time_period=time_period)
    shock_type_growth = read_shock_type(ctx)
    shock_active_growth = shock_active(ctx, time_period=time_period)
    primary_balance = shocked_primary_balance(ctx, time_period=time_period)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(previous_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(interest_baseline) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_int_interest := xl_int(shock_type_interest)) < 1 or shock_type_int_interest > 3 else 0.0 if shock_type_int_interest == 1 else shock_magnitude_resolved(ctx) if shock_type_int_interest == 2 else 0.0 if shock_type_int_interest == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_interest))), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(growth_baseline) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_int_growth := xl_int(shock_type_growth)) < 1 or shock_type_int_growth > 3 else shock_magnitude_resolved(ctx) if shock_type_int_growth == 1 else 0.0 if shock_type_int_growth == 2 else 0.0 if shock_type_int_growth == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_growth))), xl_number(100.0)))))) - xl_number(primary_balance)


@xl_memoize
def baseline_path_internal(ctx: EvalContext, time_period: int) -> float:
    """Compute one year's debt-to-GDP ratio along the baseline path.

Args:
    ctx (EvalContext): Workbook evaluation context.
    time_period (int): Index of the projection year (1..5).

Returns:
    float: The computed debt-to-GDP ratio for the given projection year.

Note:
    Covers Engine!C6:G6. Excel: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.
"""
    if time_period == 1:
        initial_debt = initial_debt_resolved(ctx)
        initial_interest_rate = read_interest_baseline(ctx, time_period=1)
        initial_growth_rate = read_growth_baseline(ctx, time_period=1)
        initial_primary_balance = read_primary_balance_baseline(ctx, time_period=1)
        return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(initial_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(initial_interest_rate), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(initial_growth_rate), xl_number(100.0)))))) - xl_number(initial_primary_balance)
    prev_debt = baseline_path_internal(ctx, time_period=time_period - 1)
    interest_rate = read_interest_baseline(ctx, time_period=time_period)
    growth_rate = read_growth_baseline(ctx, time_period=time_period)
    primary_balance = read_primary_balance_baseline(ctx, time_period=time_period)
    return xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(xl_number(prev_debt) * xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(interest_rate), xl_number(100.0))))), xl_number(xl_number(1.0) + xl_number((lambda _ln, _rn: _ln / _rn if _rn != 0 else xl_raise(XlError.DIV))(xl_number(growth_rate), xl_number(100.0)))))) - xl_number(primary_balance)


@xl_memoize
def shocked_primary_balance(ctx: EvalContext, time_period: int) -> float:
    """Compute the shocked primary balance as the baseline primary balance plus the shock impact.

Args:
    ctx (EvalContext): Workbook evaluation context.
    time_period (int): Projection year in {1, 2, 3, 4, 5}.

Returns:
    float: Shocked primary balance as a percentage of GDP.

Note:
    Covers Engine!C16:G16. Excel: =Inputs!C18+CHOOSE(Inputs!B22,0,0,Engine!B9)*Engine!C10.
"""
    primary_balance_baseline = read_primary_balance_baseline(ctx, time_period=time_period)
    shock_type = read_shock_type(ctx)
    shock_active_value = shock_active(ctx, time_period=time_period)
    return xl_number(primary_balance_baseline) + xl_number(xl_number(xl_raise(XlError.VALUE) if (shock_type_int := xl_int(shock_type)) < 1 or shock_type_int > 3 else 0.0 if shock_type_int == 1 else 0.0 if shock_type_int == 2 else shock_magnitude_resolved(ctx) if shock_type_int == 3 else xl_raise(XlError.VALUE)) * xl_number(shock_active_value))


@xl_memoize
def shock_active(ctx: EvalContext, time_period: int) -> float:
    """Determine whether the shock is active in the given time period.

Args:
    time_period (int): The time period index (1–5) for which to check shock activity.
        Determines which column in the engine sheet to read the period header from.

Returns:
    float: 1.0 if the shock is active in the period, 0.0 otherwise.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!C5>=Inputs!B21,1,0).
"""
    period_column_letter = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G'}
    period_number = xl_cell(ctx, f'Engine!{period_column_letter[time_period]}5')
    shock_year = read_shock_year(ctx)
    return 1.0 if (is_shock_active := xl_bool(xl_compare('>=', period_number, shock_year))) else 0.0


@xl_memoize
def initial_debt_resolved(ctx: EvalContext) -> CellValue:
    """Retrieve the initial debt-to-GDP ratio for the selected country from the Inputs table.

Args:
    ctx (EvalContext): The workbook evaluation context.

Returns:
    CellValue: The initial debt-to-GDP ratio for the selected country.

Note:
    Covers Inputs!B6. Excel: =INDEX(Inputs!A10:C12,MATCH(Inputs!B5,Inputs!A10:A12,0),2).
"""
    country_name = read_country_name(ctx)
    country_range = xl_range(ctx, 'Inputs!A10:A12')
    matched_row = xl_match(country_name, country_range, 0.0)
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), matched_row, 2.0), 0.0, 0.0)


@xl_memoize
def shock_magnitude_resolved(ctx: EvalContext) -> CellValue:
    """Resolve the shock magnitude based on the configured shock type.

Args:
    ctx (EvalContext): Workbook evaluation context with cell bindings.

Returns:
    CellValue: The resolved shock magnitude in percentage points (PP), as a scalar value from the Inputs row.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!B26,0,Inputs!B22-1).
"""
    shock_type_code = read_shock_type(ctx)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_number(shock_type_code) - xl_number(1.0), None, None)


# --- Unrefactored formula cells ---

def cell_engine_b20(ctx):
    _t3 = read_country_name(ctx)
    _t4 = xl_range(ctx, 'Inputs!A10:A12')
    _t5 = xl_match(_t3, _t4, 0.0)
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), _t5, 2.0), 0.0, 0.0)


def cell_engine_b6(ctx):
    _t6 = read_country_name(ctx)
    _t7 = xl_range(ctx, 'Inputs!A10:A12')
    _t8 = xl_match(_t6, _t7, 0.0)
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), _t8, 2.0), 0.0, 0.0)


def cell_outputs_b12(ctx):
    _t9 = initial_debt_resolved(ctx)
    _t10 = read_interest_baseline(ctx, time_period=1)
    _t11 = read_growth_baseline(ctx, time_period=1)
    _t12 = read_primary_balance_baseline(ctx, time_period=1)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t9) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t10), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t11), xl_number(100.0))))))))) - xl_number(_t12))


def cell_outputs_b13(ctx):
    _t13 = initial_debt_resolved(ctx)
    _t14 = read_interest_baseline(ctx, time_period=1)
    _t15 = read_shock_type(ctx)
    _t17 = shock_active(ctx, time_period=1)
    _t18 = read_growth_baseline(ctx, time_period=1)
    _t19 = read_shock_type(ctx)
    _t21 = shock_active(ctx, time_period=1)
    _t22 = shocked_primary_balance(ctx, time_period=1)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t13) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t14) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t16 := xl_int(_t15)) < 1 or _t16 > 3 else ((0.0) if _t16 == 1 else (((shock_magnitude_resolved(ctx)) if _t16 == 2 else (((0.0) if _t16 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t17))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t18) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t20 := xl_int(_t19)) < 1 or _t20 > 3 else ((shock_magnitude_resolved(ctx)) if _t20 == 1 else (((0.0) if _t20 == 2 else (((0.0) if _t20 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t21))))), xl_number(100.0))))))))) - xl_number(_t22))


def cell_outputs_c12(ctx):
    _t23 = baseline_path_internal(ctx, time_period=1)
    _t24 = read_interest_baseline(ctx, time_period=2)
    _t25 = read_growth_baseline(ctx, time_period=2)
    _t26 = read_primary_balance_baseline(ctx, time_period=2)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t23) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t24), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t25), xl_number(100.0))))))))) - xl_number(_t26))


def cell_outputs_c13(ctx):
    _t27 = shocked_path_internal(ctx, time_period=1)
    _t28 = read_interest_baseline(ctx, time_period=2)
    _t29 = read_shock_type(ctx)
    _t31 = shock_active(ctx, time_period=2)
    _t32 = read_growth_baseline(ctx, time_period=2)
    _t33 = read_shock_type(ctx)
    _t35 = shock_active(ctx, time_period=2)
    _t36 = shocked_primary_balance(ctx, time_period=2)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t27) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t28) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t30 := xl_int(_t29)) < 1 or _t30 > 3 else ((0.0) if _t30 == 1 else (((shock_magnitude_resolved(ctx)) if _t30 == 2 else (((0.0) if _t30 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t31))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t32) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t34 := xl_int(_t33)) < 1 or _t34 > 3 else ((shock_magnitude_resolved(ctx)) if _t34 == 1 else (((0.0) if _t34 == 2 else (((0.0) if _t34 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t35))))), xl_number(100.0))))))))) - xl_number(_t36))


def cell_outputs_d12(ctx):
    _t37 = baseline_path_internal(ctx, time_period=2)
    _t38 = read_interest_baseline(ctx, time_period=3)
    _t39 = read_growth_baseline(ctx, time_period=3)
    _t40 = read_primary_balance_baseline(ctx, time_period=3)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t37) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t38), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t39), xl_number(100.0))))))))) - xl_number(_t40))


def cell_outputs_d13(ctx):
    _t41 = shocked_path_internal(ctx, time_period=2)
    _t42 = read_interest_baseline(ctx, time_period=3)
    _t43 = read_shock_type(ctx)
    _t45 = shock_active(ctx, time_period=3)
    _t46 = read_growth_baseline(ctx, time_period=3)
    _t47 = read_shock_type(ctx)
    _t49 = shock_active(ctx, time_period=3)
    _t50 = shocked_primary_balance(ctx, time_period=3)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t41) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t42) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t44 := xl_int(_t43)) < 1 or _t44 > 3 else ((0.0) if _t44 == 1 else (((shock_magnitude_resolved(ctx)) if _t44 == 2 else (((0.0) if _t44 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t45))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t46) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t48 := xl_int(_t47)) < 1 or _t48 > 3 else ((shock_magnitude_resolved(ctx)) if _t48 == 1 else (((0.0) if _t48 == 2 else (((0.0) if _t48 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t49))))), xl_number(100.0))))))))) - xl_number(_t50))


def cell_outputs_e12(ctx):
    _t51 = baseline_path_internal(ctx, time_period=3)
    _t52 = read_interest_baseline(ctx, time_period=4)
    _t53 = read_growth_baseline(ctx, time_period=4)
    _t54 = read_primary_balance_baseline(ctx, time_period=4)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t51) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t52), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t53), xl_number(100.0))))))))) - xl_number(_t54))


def cell_outputs_e13(ctx):
    _t55 = shocked_path_internal(ctx, time_period=3)
    _t56 = read_interest_baseline(ctx, time_period=4)
    _t57 = read_shock_type(ctx)
    _t59 = shock_active(ctx, time_period=4)
    _t60 = read_growth_baseline(ctx, time_period=4)
    _t61 = read_shock_type(ctx)
    _t63 = shock_active(ctx, time_period=4)
    _t64 = shocked_primary_balance(ctx, time_period=4)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t55) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t56) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t58 := xl_int(_t57)) < 1 or _t58 > 3 else ((0.0) if _t58 == 1 else (((shock_magnitude_resolved(ctx)) if _t58 == 2 else (((0.0) if _t58 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t59))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t60) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t62 := xl_int(_t61)) < 1 or _t62 > 3 else ((shock_magnitude_resolved(ctx)) if _t62 == 1 else (((0.0) if _t62 == 2 else (((0.0) if _t62 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t63))))), xl_number(100.0))))))))) - xl_number(_t64))


def cell_outputs_f12(ctx):
    _t65 = baseline_path_internal(ctx, time_period=4)
    _t66 = read_interest_baseline(ctx, time_period=5)
    _t67 = read_growth_baseline(ctx, time_period=5)
    _t68 = read_primary_balance_baseline(ctx, time_period=5)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t65) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t66), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number(_t67), xl_number(100.0))))))))) - xl_number(_t68))


def cell_outputs_f13(ctx):
    _t69 = shocked_path_internal(ctx, time_period=4)
    _t70 = read_interest_baseline(ctx, time_period=5)
    _t71 = read_shock_type(ctx)
    _t73 = shock_active(ctx, time_period=5)
    _t74 = read_growth_baseline(ctx, time_period=5)
    _t75 = read_shock_type(ctx)
    _t77 = shock_active(ctx, time_period=5)
    _t78 = shocked_primary_balance(ctx, time_period=5)
    return (xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t69) * xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t70) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t72 := xl_int(_t71)) < 1 or _t72 > 3 else ((0.0) if _t72 == 1 else (((shock_magnitude_resolved(ctx)) if _t72 == 2 else (((0.0) if _t72 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t73))))), xl_number(100.0)))))))), xl_number((xl_number(1.0) + xl_number(((lambda _ln, _rn: (_ln / _rn if _rn != 0 else xl_raise(XlError.DIV)))(xl_number((xl_number(_t74) + xl_number((xl_number(((xl_raise(XlError.VALUE) if (_t76 := xl_int(_t75)) < 1 or _t76 > 3 else ((shock_magnitude_resolved(ctx)) if _t76 == 1 else (((0.0) if _t76 == 2 else (((0.0) if _t76 == 3 else (xl_raise(XlError.VALUE)))))))))) * xl_number(_t77))))), xl_number(100.0))))))))) - xl_number(_t78))


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
