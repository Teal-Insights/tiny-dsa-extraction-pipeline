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
    return xl_sub(debt_to_gdp(ctx, col), baseline_debt(ctx, col))

def debt_to_gdp(ctx, col):
    '''Engine!C20:G20'''
    if col == 'C':
        prior_debt = xl_eval(ctx, 'Inputs!B6', cell_inputs_b6)
    else:
        prev_col = chr(ord(col) - 1)
        prior_debt = debt_to_gdp(ctx, prev_col)
    if col == 'C':
        eng10_fn = cell_engine_c10
        eng16_fn = cell_engine_c16
    elif col == 'D':
        eng10_fn = cell_engine_d10
        eng16_fn = cell_engine_d16
    elif col == 'E':
        eng10_fn = cell_engine_e10
        eng16_fn = cell_engine_e16
    elif col == 'F':
        eng10_fn = cell_engine_f10
        eng16_fn = cell_engine_f16
    elif col == 'G':
        eng10_fn = cell_engine_g10
        eng16_fn = cell_engine_g16
    else:
        eng10_fn = None
        eng16_fn = None
    input17 = xl_cell(ctx, f'Inputs!{col}17')
    input16 = xl_cell(ctx, f'Inputs!{col}16')
    eng10 = shock_active(ctx, col)
    eng16 = primary_balance_shocked(ctx, col)
    chooser1 = (_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE))))))))
    chooser2 = (_t3 if isinstance((_t3 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t4 if isinstance((_t4 := to_int(_t3)), XlError) else XlError.VALUE if _t4 < 1 or _t4 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t4 == 1 else (((0.0) if _t4 == 2 else (((0.0) if _t4 == 3 else (XlError.VALUE))))))))
    num_adjust = xl_div(xl_add(input17, xl_mul(chooser1, eng10)), 100.0)
    denom_adjust = xl_div(xl_add(input16, xl_mul(chooser2, eng10)), 100.0)
    return xl_sub(xl_div(xl_mul(prior_debt, xl_add(1.0, num_adjust)), xl_add(1.0, denom_adjust)), eng16)

def baseline_debt(ctx, col):
    """Calculate baseline debt for Engine!C6:G6."""
    if col == 'C':
        prior = xl_eval(ctx, 'Inputs!B6', cell_inputs_b6)
    else:
        prior_col = chr(ord(col) - 1)
        prior = baseline_debt(ctx, prior_col)
    rate17 = xl_cell(ctx, 'Inputs!' + col + '17')
    rate16 = xl_cell(ctx, 'Inputs!' + col + '16')
    subtract18 = xl_cell(ctx, 'Inputs!' + col + '18')
    return xl_sub(xl_div(xl_mul(prior, xl_add(1.0, xl_div(rate17, 100.0))), xl_add(1.0, xl_div(rate16, 100.0))), subtract18)

def primary_balance_shocked(ctx, col):
    """Covers Engine!C16:G16."""
    return xl_add(
        xl_cell(ctx, f'Inputs!{col}18'),
        xl_mul(
            (_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))),
            shock_active(ctx, col)
        )
    )

def shock_active(ctx, col):
    '''Formula: =IF(Engine!{COL}5>=Inputs!$B$21,1,0). Engine!C10:G10'''
    cell_eng5 = xl_cell(ctx, f'Engine!{col}5')
    b21 = xl_cell(ctx, 'Inputs!B21')
    t1 = xl_ge(cell_eng5, b21)
    t2 = to_bool(t1)
    if isinstance(t2, XlError):
        return t2
    return 1.0 if t2 else 0.0

def cell_inputs_b6(ctx):
    '''Formula: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).'''
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), xl_match(xl_cell(ctx, 'Inputs!B5'), np.array(np.array([[xl_cell(ctx, 'Inputs!A10')], [xl_cell(ctx, 'Inputs!A11')], [xl_cell(ctx, 'Inputs!A12')]], dtype=object), dtype=object), 0.0), 2.0), 0.0, 0.0)


def cell_engine_b9(ctx):
    '''Formula: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1).'''
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_sub(xl_cell(ctx, 'Inputs!B22'), 1.0), None, None)



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
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
