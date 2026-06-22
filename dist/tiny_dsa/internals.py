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
    """Outputs!B14:F14"""
    if col == 'C':
        fn20 = cell_engine_c20
        fn6 = cell_engine_c6
    elif col == 'D':
        fn20 = cell_engine_d20
        fn6 = cell_engine_d6
    elif col == 'E':
        fn20 = cell_engine_e20
        fn6 = cell_engine_e6
    elif col == 'F':
        fn20 = cell_engine_f20
        fn6 = cell_engine_f6
    elif col == 'G':
        fn20 = cell_engine_g20
        fn6 = cell_engine_g6
    else:
        raise ValueError(f"Invalid engine column: {col}")
    return xl_sub(
        xl_eval(ctx, f'Engine!{col}20', fn20),
        xl_eval(ctx, f'Engine!{col}6', fn6)
    )

def debt_to_gdp(ctx, col):
    """Engine!C20:G20"""
    if col == 'C':
        prior = xl_eval(ctx, 'Inputs!B6', cell_inputs_b6)
    elif col == 'D':
        prior = debt_to_gdp(ctx, 'C')
    elif col == 'E':
        prior = debt_to_gdp(ctx, 'D')
    elif col == 'F':
        prior = debt_to_gdp(ctx, 'E')
    elif col == 'G':
        prior = debt_to_gdp(ctx, 'F')
    else:
        prior = XlError.VALUE

    eng9 = xl_eval(ctx, 'Engine!B9', cell_engine_b9)

    input17 = xl_cell(ctx, 'Inputs!' + col + '17')
    input16 = xl_cell(ctx, 'Inputs!' + col + '16')

    if col == 'C':
        eng10 = xl_eval(ctx, 'Engine!C10', cell_engine_c10)
        eng16 = xl_eval(ctx, 'Engine!C16', cell_engine_c16)
    elif col == 'D':
        eng10 = xl_eval(ctx, 'Engine!D10', cell_engine_d10)
        eng16 = xl_eval(ctx, 'Engine!D16', cell_engine_d16)
    elif col == 'E':
        eng10 = xl_eval(ctx, 'Engine!E10', cell_engine_e10)
        eng16 = xl_eval(ctx, 'Engine!E16', cell_engine_e16)
    elif col == 'F':
        eng10 = xl_eval(ctx, 'Engine!F10', cell_engine_f10)
        eng16 = xl_eval(ctx, 'Engine!F16', cell_engine_f16)
    elif col == 'G':
        eng10 = xl_eval(ctx, 'Engine!G10', cell_engine_g10)
        eng16 = xl_eval(ctx, 'Engine!G16', cell_engine_g16)
    else:
        eng10 = XlError.VALUE
        eng16 = XlError.VALUE

    num_choose = (_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else (0.0 if _t2 == 1 else (eng9 if _t2 == 2 else (0.0 if _t2 == 3 else XlError.VALUE)))))
    den_choose = (_t3 if isinstance((_t3 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t4 if isinstance((_t4 := to_int(_t3)), XlError) else XlError.VALUE if _t4 < 1 or _t4 > 3 else (eng9 if _t4 == 1 else (0.0 if _t4 == 2 else (0.0 if _t4 == 3 else XlError.VALUE)))))

    num_term = xl_add(1.0, xl_div(xl_add(input17, xl_mul(num_choose, eng10)), 100.0))
    den_term = xl_add(1.0, xl_div(xl_add(input16, xl_mul(den_choose, eng10)), 100.0))

    return xl_sub(xl_div(xl_mul(prior, num_term), den_term), eng16)

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
    ten_func_map = {
        'C': cell_engine_c10,
        'D': cell_engine_d10,
        'E': cell_engine_e10,
        'F': cell_engine_f10,
        'G': cell_engine_g10,
    }
    return xl_add(
        xl_cell(ctx, f'Inputs!{col}18'),
        xl_mul(
            (_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))),
            xl_eval(ctx, f'Engine!{col}10', ten_func_map[col])
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


def cell_engine_c6(ctx):
    '''Formula: =Inputs!B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18.'''
    return baseline_debt(ctx, "C")


def cell_engine_d6(ctx):
    '''Formula: =C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18.'''
    return baseline_debt(ctx, "D")


def cell_engine_e6(ctx):
    '''Formula: =D6*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Inputs!E18.'''
    return baseline_debt(ctx, "E")


def cell_engine_f6(ctx):
    '''Formula: =E6*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Inputs!F18.'''
    return baseline_debt(ctx, "F")


def cell_engine_g6(ctx):
    '''Formula: =F6*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Inputs!G18.'''
    return baseline_debt(ctx, "G")


def cell_engine_b9(ctx):
    '''Formula: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1).'''
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_sub(xl_cell(ctx, 'Inputs!B22'), 1.0), None, None)


def cell_engine_c10(ctx):
    '''Formula: =IF(C5>=Inputs!$B$21,1,0).'''
    return shock_active(ctx, "C")


def cell_engine_d10(ctx):
    '''Formula: =IF(D5>=Inputs!$B$21,1,0).'''
    return shock_active(ctx, "D")


def cell_engine_e10(ctx):
    '''Formula: =IF(E5>=Inputs!$B$21,1,0).'''
    return shock_active(ctx, "E")


def cell_engine_f10(ctx):
    '''Formula: =IF(F5>=Inputs!$B$21,1,0).'''
    return shock_active(ctx, "F")


def cell_engine_g10(ctx):
    '''Formula: =IF(G5>=Inputs!$B$21,1,0).'''
    return shock_active(ctx, "G")


def cell_engine_c16(ctx):
    '''Formula: =Inputs!C18+CHOOSE(Inputs!$B$22,0,0,$B$9)*C10.'''
    return primary_balance_shocked(ctx, "C")


def cell_engine_d16(ctx):
    '''Formula: =Inputs!D18+CHOOSE(Inputs!$B$22,0,0,$B$9)*D10.'''
    return primary_balance_shocked(ctx, "D")


def cell_engine_e16(ctx):
    '''Formula: =Inputs!E18+CHOOSE(Inputs!$B$22,0,0,$B$9)*E10.'''
    return primary_balance_shocked(ctx, "E")


def cell_engine_f16(ctx):
    '''Formula: =Inputs!F18+CHOOSE(Inputs!$B$22,0,0,$B$9)*F10.'''
    return primary_balance_shocked(ctx, "F")


def cell_engine_g16(ctx):
    '''Formula: =Inputs!G18+CHOOSE(Inputs!$B$22,0,0,$B$9)*G10.'''
    return primary_balance_shocked(ctx, "G")


def cell_engine_c20(ctx):
    '''Formula: =Inputs!B6*(1+(Inputs!C17+CHOOSE(Inputs!$B$22,0,$B$9,0)*C10)/100)/(1+(Inputs!C16+CHOOSE(Inputs!$B$22,$B$9,0,0)*C10)/100)-C16.'''
    return debt_to_gdp(ctx, "C")


def cell_engine_d20(ctx):
    '''Formula: =C20*(1+(Inputs!D17+CHOOSE(Inputs!$B$22,0,$B$9,0)*D10)/100)/(1+(Inputs!D16+CHOOSE(Inputs!$B$22,$B$9,0,0)*D10)/100)-D16.'''
    return debt_to_gdp(ctx, "D")


def cell_engine_e20(ctx):
    '''Formula: =D20*(1+(Inputs!E17+CHOOSE(Inputs!$B$22,0,$B$9,0)*E10)/100)/(1+(Inputs!E16+CHOOSE(Inputs!$B$22,$B$9,0,0)*E10)/100)-E16.'''
    return debt_to_gdp(ctx, "E")


def cell_engine_f20(ctx):
    '''Formula: =E20*(1+(Inputs!F17+CHOOSE(Inputs!$B$22,0,$B$9,0)*F10)/100)/(1+(Inputs!F16+CHOOSE(Inputs!$B$22,$B$9,0,0)*F10)/100)-F16.'''
    return debt_to_gdp(ctx, "F")


def cell_engine_g20(ctx):
    '''Formula: =F20*(1+(Inputs!G17+CHOOSE(Inputs!$B$22,0,$B$9,0)*G10)/100)/(1+(Inputs!G16+CHOOSE(Inputs!$B$22,$B$9,0,0)*G10)/100)-G16.'''
    return debt_to_gdp(ctx, "G")


def cell_outputs_b14(ctx):
    '''Formula: =Engine!C20-Engine!C6.'''
    return output_delta(ctx, "C")


def cell_outputs_c14(ctx):
    '''Formula: =Engine!D20-Engine!D6.'''
    return output_delta(ctx, "D")


def cell_outputs_d14(ctx):
    '''Formula: =Engine!E20-Engine!E6.'''
    return output_delta(ctx, "E")


def cell_outputs_e14(ctx):
    '''Formula: =Engine!F20-Engine!F6.'''
    return output_delta(ctx, "F")


def cell_outputs_f14(ctx):
    '''Formula: =Engine!G20-Engine!G6.'''
    return output_delta(ctx, "G")


# --- Projection public address aliases ---

def cell_outputs_b12(ctx):
    return xl_eval(ctx, 'Engine!C6', cell_engine_c6)

def cell_outputs_b13(ctx):
    return xl_eval(ctx, 'Engine!C20', cell_engine_c20)

def cell_outputs_c12(ctx):
    return xl_eval(ctx, 'Engine!D6', cell_engine_d6)

def cell_outputs_c13(ctx):
    return xl_eval(ctx, 'Engine!D20', cell_engine_d20)

def cell_outputs_d12(ctx):
    return xl_eval(ctx, 'Engine!E6', cell_engine_e6)

def cell_outputs_d13(ctx):
    return xl_eval(ctx, 'Engine!E20', cell_engine_e20)

def cell_outputs_e12(ctx):
    return xl_eval(ctx, 'Engine!F6', cell_engine_f6)

def cell_outputs_e13(ctx):
    return xl_eval(ctx, 'Engine!F20', cell_engine_f20)

def cell_outputs_f12(ctx):
    return xl_eval(ctx, 'Engine!G6', cell_engine_g6)

def cell_outputs_f13(ctx):
    return xl_eval(ctx, 'Engine!G20', cell_engine_g20)

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
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
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
