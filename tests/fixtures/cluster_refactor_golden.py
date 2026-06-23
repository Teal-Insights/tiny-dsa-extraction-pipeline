"""Golden cluster refactor responses for integration tests (no LLM/cache)."""

from __future__ import annotations

from src.formula_clustering import EngineColumn
from src.internals_refactor import ClusterRefactorResponse, MemberBinding

SHOCK_ACTIVE_DOCSTRING = """\
Return 1.0 when the shock is active for the given projection column.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    1.0 if the projection year is at or after the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0).\
"""

PRIMARY_BALANCE_SHOCKED_DOCSTRING = """\
Return the shocked primary balance for the given projection column.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    Baseline primary balance plus the shock adjustment for the column.

Note:
    Covers Engine!C16:G16.\
"""

BASELINE_DEBT_DOCSTRING = """\
Return the baseline debt-to-GDP ratio for the given projection column.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    Recursed baseline debt-to-GDP ratio for the column.

Note:
    Covers Engine!C6:G6.\
"""

DEBT_TO_GDP_DOCSTRING = """\
Return the shocked debt-to-GDP ratio for the given projection column.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    Recursed shocked debt-to-GDP ratio for the column.

Note:
    Covers Engine!C20:G20.\
"""

OUTPUT_DELTA_DOCSTRING = """\
Return the debt-to-GDP delta between shocked and baseline paths.

Args:
    ctx: Workbook evaluation context.
    col: Engine column letter (C through G).

Returns:
    Shocked debt-to-GDP minus baseline debt-to-GDP for the column.

Note:
    Covers Outputs!B14:F14.\
"""


def _quoted_docstring(text: str) -> str:
    return f'    """{text}"""\n'


SHOCK_ACTIVE_HELPER = f"""\
def shock_active(ctx, col):
{_quoted_docstring(SHOCK_ACTIVE_DOCSTRING)}    projection_year = xl_cell(ctx, f'Engine!{{col}}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    year_at_or_after_shock = xl_ge(projection_year, shock_year)
    is_active = to_bool(year_at_or_after_shock)
    if isinstance(is_active, XlError):
        return is_active
    return 1.0 if is_active else 0.0\
"""

PRIMARY_BALANCE_SHOCKED_HELPER = f"""\
def primary_balance_shocked(ctx, col):
{_quoted_docstring(PRIMARY_BALANCE_SHOCKED_DOCSTRING)}    shock_type = xl_cell(ctx, 'Inputs!B22')
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
    baseline_primary_balance = xl_cell(ctx, f'Inputs!{{col}}18')
    shock_adjustment = xl_mul(shock_multiplier, shock_active(ctx, col))
    return xl_add(baseline_primary_balance, shock_adjustment)\
"""

BASELINE_DEBT_HELPER = f"""\
def baseline_debt(ctx, col):
{_quoted_docstring(BASELINE_DEBT_DOCSTRING)}    if col == 'C':
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        previous_column = chr(ord(col) - 1)
        prior_debt = baseline_debt(ctx, previous_column)
    growth_rate = xl_cell(ctx, f'Inputs!{{col}}17')
    interest_rate = xl_cell(ctx, f'Inputs!{{col}}16')
    primary_balance = xl_cell(ctx, f'Inputs!{{col}}18')
    growth_term = xl_add(1.0, xl_div(growth_rate, 100.0))
    interest_term = xl_add(1.0, xl_div(interest_rate, 100.0))
    scaled_debt = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(scaled_debt, primary_balance)\
"""

DEBT_TO_GDP_HELPER = f"""\
def debt_to_gdp(ctx, col):
{_quoted_docstring(DEBT_TO_GDP_DOCSTRING)}    if col == 'C':
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        previous_column = chr(ord(col) - 1)
        prior_debt = debt_to_gdp(ctx, previous_column)
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
    growth_rate = xl_cell(ctx, f'Inputs!{{col}}17')
    interest_rate = xl_cell(ctx, f'Inputs!{{col}}16')
    shock_activation = shock_active(ctx, col)
    shocked_primary_balance = primary_balance_shocked(ctx, col)
    growth_numerator = xl_add(growth_rate, xl_mul(growth_shock_factor, shock_activation))
    growth_term = xl_add(1.0, xl_div(growth_numerator, 100.0))
    interest_denominator = xl_add(interest_rate, xl_mul(interest_shock_factor, shock_activation))
    interest_term = xl_add(1.0, xl_div(interest_denominator, 100.0))
    debt_ratio = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(debt_ratio, shocked_primary_balance)\
"""

OUTPUT_DELTA_HELPER = f"""\
def output_delta(ctx, col):
{_quoted_docstring(OUTPUT_DELTA_DOCSTRING)}    return xl_sub(debt_to_gdp(ctx, col), baseline_debt(ctx, col))\
"""


def _bindings(
    addresses: tuple[tuple[str, str, EngineColumn], ...],
) -> tuple[MemberBinding, ...]:
    return tuple(
        MemberBinding(
            address=address,
            function_name=function_name,
            engine_column=engine_column,
        )
        for address, function_name, engine_column in addresses
    )


ENGINE_ROW_BINDINGS = {
    10: _bindings(
        (
            ("Engine!C10", "cell_engine_c10", "C"),
            ("Engine!D10", "cell_engine_d10", "D"),
            ("Engine!E10", "cell_engine_e10", "E"),
            ("Engine!F10", "cell_engine_f10", "F"),
            ("Engine!G10", "cell_engine_g10", "G"),
        )
    ),
    16: _bindings(
        (
            ("Engine!C16", "cell_engine_c16", "C"),
            ("Engine!D16", "cell_engine_d16", "D"),
            ("Engine!E16", "cell_engine_e16", "E"),
            ("Engine!F16", "cell_engine_f16", "F"),
            ("Engine!G16", "cell_engine_g16", "G"),
        )
    ),
    6: _bindings(
        (
            ("Engine!C6", "cell_engine_c6", "C"),
            ("Engine!D6", "cell_engine_d6", "D"),
            ("Engine!E6", "cell_engine_e6", "E"),
            ("Engine!F6", "cell_engine_f6", "F"),
            ("Engine!G6", "cell_engine_g6", "G"),
        )
    ),
    20: _bindings(
        (
            ("Engine!C20", "cell_engine_c20", "C"),
            ("Engine!D20", "cell_engine_d20", "D"),
            ("Engine!E20", "cell_engine_e20", "E"),
            ("Engine!F20", "cell_engine_f20", "F"),
            ("Engine!G20", "cell_engine_g20", "G"),
        )
    ),
}

OUTPUT_ROW_BINDINGS = _bindings(
    (
        ("Outputs!B14", "cell_outputs_b14", "C"),
        ("Outputs!C14", "cell_outputs_c14", "D"),
        ("Outputs!D14", "cell_outputs_d14", "E"),
        ("Outputs!E14", "cell_outputs_e14", "F"),
        ("Outputs!F14", "cell_outputs_f14", "G"),
    )
)


GOLDEN_CLUSTER_REFACTOR_RESPONSES: dict[int, ClusterRefactorResponse] = {
    10: ClusterRefactorResponse(
        helper_name="shock_active",
        helper_docstring=SHOCK_ACTIVE_DOCSTRING,
        uses_first_year_branch=False,
        helper_source=SHOCK_ACTIVE_HELPER,
        member_bindings=ENGINE_ROW_BINDINGS[10],
    ),
    16: ClusterRefactorResponse(
        helper_name="primary_balance_shocked",
        helper_docstring=PRIMARY_BALANCE_SHOCKED_DOCSTRING,
        uses_first_year_branch=False,
        helper_source=PRIMARY_BALANCE_SHOCKED_HELPER,
        member_bindings=ENGINE_ROW_BINDINGS[16],
    ),
    6: ClusterRefactorResponse(
        helper_name="baseline_debt",
        helper_docstring=BASELINE_DEBT_DOCSTRING,
        uses_first_year_branch=True,
        helper_source=BASELINE_DEBT_HELPER,
        member_bindings=ENGINE_ROW_BINDINGS[6],
    ),
    20: ClusterRefactorResponse(
        helper_name="debt_to_gdp",
        helper_docstring=DEBT_TO_GDP_DOCSTRING,
        uses_first_year_branch=True,
        helper_source=DEBT_TO_GDP_HELPER,
        member_bindings=ENGINE_ROW_BINDINGS[20],
    ),
    14: ClusterRefactorResponse(
        helper_name="output_delta",
        helper_docstring=OUTPUT_DELTA_DOCSTRING,
        uses_first_year_branch=False,
        helper_source=OUTPUT_DELTA_HELPER,
        member_bindings=OUTPUT_ROW_BINDINGS,
    ),
}
