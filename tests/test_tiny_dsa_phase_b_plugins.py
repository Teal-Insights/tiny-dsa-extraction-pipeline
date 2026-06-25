from __future__ import annotations

from src.internals_refactor import validate_refactored_internals
from src.tiny_dsa_phase_b_plugins import apply_tiny_dsa_phase_b_plugins

_MESSY_HELPERS_SOURCE = """
from .runtime import XlError, np, to_int, xl_add, xl_cell, xl_div, xl_eval, xl_mul, xl_sub

def shock_active(ctx, col):
    return 1.0

def primary_balance_shocked(ctx, col):
    ten_func_map = {1: 0.1}
    baseline = xl_cell(ctx, f"Inputs!{col}18")
    shock = xl_eval(ctx, "Engine!C10", cell_engine_c10)
    return xl_add(baseline, shock)

def cell_engine_c10(ctx):
    return shock_active(ctx, "C")

def baseline_debt(ctx, col):
    return 0.5

def debt_to_gdp(ctx, col):
    if col == "C":
        col10 = xl_eval(ctx, "Engine!C10", cell_engine_c10)
        col16 = xl_eval(ctx, "Engine!C16", cell_engine_c16)
    elif col == "D":
        col10 = xl_eval(ctx, "Engine!D10", cell_engine_d10)
        col16 = xl_eval(ctx, "Engine!D16", cell_engine_d16)
    else:
        col10 = 0.0
        col16 = 0.0
    return xl_add(col10, col16)

def cell_engine_c16(ctx):
    return primary_balance_shocked(ctx, "C")

def cell_engine_d10(ctx):
    return shock_active(ctx, "D")

def cell_engine_d16(ctx):
    return primary_balance_shocked(ctx, "D")

def output_delta(ctx, col):
    fn20 = xl_eval(ctx, f"Engine!{col}20", cell_engine_c20)
    fn6 = xl_eval(ctx, f"Outputs!B12", cell_outputs_b12)
    return xl_sub(fn20, fn6)

def cell_engine_c20(ctx):
    return debt_to_gdp(ctx, "C")

def cell_outputs_b12(ctx):
    return baseline_debt(ctx, "C")
"""


def test_apply_tiny_dsa_phase_b_plugins_simplifies_primary_balance() -> None:
    updated = apply_tiny_dsa_phase_b_plugins(_MESSY_HELPERS_SOURCE)
    validate_refactored_internals(updated)

    assert "ten_func_map" not in updated
    assert "shock_active(ctx, col)" in _extract_function(
        updated, "primary_balance_shocked"
    )


def test_apply_tiny_dsa_phase_b_plugins_simplifies_debt_to_gdp() -> None:
    updated = apply_tiny_dsa_phase_b_plugins(_MESSY_HELPERS_SOURCE)
    validate_refactored_internals(updated)

    debt_to_gdp = _extract_function(updated, "debt_to_gdp")
    assert "xl_eval(ctx, 'Engine!C10', cell_engine_c10)" not in debt_to_gdp
    assert "eng10 = shock_active(ctx, col)" in debt_to_gdp
    assert "eng16 = primary_balance_shocked(ctx, col)" in debt_to_gdp


def test_apply_tiny_dsa_phase_b_plugins_simplifies_output_delta() -> None:
    updated = apply_tiny_dsa_phase_b_plugins(_MESSY_HELPERS_SOURCE)
    validate_refactored_internals(updated)

    output_delta = _extract_function(updated, "output_delta")
    assert "fn20" not in output_delta
    assert (
        "return xl_sub(debt_to_gdp(ctx, col), baseline_debt(ctx, col))" in output_delta
    )


def _extract_function(source: str, name: str) -> str:
    marker = f"def {name}("
    start = source.index(marker)
    next_def = source.find("\ndef ", start + 1)
    if next_def == -1:
        return source[start:]
    return source[start:next_def]
