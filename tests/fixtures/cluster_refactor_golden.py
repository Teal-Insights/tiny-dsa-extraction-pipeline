"""Golden cluster refactor responses for integration tests (no LLM/cache)."""

from __future__ import annotations

from src.internals_refactor import (
    ClusterRefactorResponse,
    HelperParameter,
    MemberKeys,
)
from src.refactor_bindings import BindingKeyValue

_COLUMN_BY_TIME_PERIOD = {1: "C", 2: "D", 3: "E", 4: "F", 5: "G"}

SHOCK_ACTIVE_DOCSTRING = """\
Return 1.0 when the shock is active for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    1.0 if the projection year is at or after the shock year, else 0.0.

Note:
    Covers Engine!C10:G10. Excel: =IF(Engine!{col}5>=Inputs!$B$21,1,0).\
"""

PRIMARY_BALANCE_SHOCKED_DOCSTRING = """\
Return the shocked primary balance for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Baseline primary balance plus the shock adjustment for the year.

Note:
    Covers Engine!C16:G16.\
"""

BASELINE_DEBT_DOCSTRING = """\
Return the baseline debt-to-GDP ratio for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Recursed baseline debt-to-GDP ratio for the year.

Note:
    Covers Engine!C6:G6.\
"""

DEBT_TO_GDP_DOCSTRING = """\
Return the shocked debt-to-GDP ratio for the given projection year.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Recursed shocked debt-to-GDP ratio for the year.

Note:
    Covers Engine!C20:G20.\
"""

OUTPUT_DELTA_DOCSTRING = """\
Return the debt-to-GDP delta between shocked and baseline paths.

Args:
    ctx: Workbook evaluation context.
    time_period: Projection year index (1 through 5).

Returns:
    Shocked debt-to-GDP minus baseline debt-to-GDP for the year.

Note:
    Covers Outputs!B14:F14.\
"""


def _quoted_docstring(text: str) -> str:
    return f'    """{text}"""\n'


SHOCK_ACTIVE_HELPER = f"""\
def shock_active(ctx, time_period: int):
{_quoted_docstring(SHOCK_ACTIVE_DOCSTRING)}    column = {_COLUMN_BY_TIME_PERIOD!r}[time_period]
    projection_year = xl_cell(ctx, f'Engine!{{column}}5')
    shock_year = xl_cell(ctx, 'Inputs!B21')
    year_at_or_after_shock = xl_ge(projection_year, shock_year)
    is_active = to_bool(year_at_or_after_shock)
    if isinstance(is_active, XlError):
        return is_active
    return 1.0 if is_active else 0.0\
"""

PRIMARY_BALANCE_SHOCKED_HELPER = f"""\
def primary_balance_shocked(ctx, time_period: int):
{_quoted_docstring(PRIMARY_BALANCE_SHOCKED_DOCSTRING)}    column = {_COLUMN_BY_TIME_PERIOD!r}[time_period]
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
    baseline_primary_balance = xl_cell(ctx, f'Inputs!{{column}}18')
    shock_adjustment = xl_mul(shock_multiplier, shock_active(ctx, time_period=time_period))
    return xl_add(baseline_primary_balance, shock_adjustment)\
"""

BASELINE_DEBT_HELPER = f"""\
def baseline_debt(ctx, time_period: int):
{_quoted_docstring(BASELINE_DEBT_DOCSTRING)}    if time_period == 1:
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        prior_debt = baseline_debt(ctx, time_period=time_period - 1)
    column = {_COLUMN_BY_TIME_PERIOD!r}[time_period]
    growth_rate = xl_cell(ctx, f'Inputs!{{column}}17')
    interest_rate = xl_cell(ctx, f'Inputs!{{column}}16')
    primary_balance = xl_cell(ctx, f'Inputs!{{column}}18')
    growth_term = xl_add(1.0, xl_div(growth_rate, 100.0))
    interest_term = xl_add(1.0, xl_div(interest_rate, 100.0))
    scaled_debt = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(scaled_debt, primary_balance)\
"""

DEBT_TO_GDP_HELPER = f"""\
def debt_to_gdp(ctx, time_period: int):
{_quoted_docstring(DEBT_TO_GDP_DOCSTRING)}    if time_period == 1:
        prior_debt = xl_eval(ctx, 'Inputs!B6', initial_debt_to_gdp)
    else:
        prior_debt = debt_to_gdp(ctx, time_period=time_period - 1)
    column = {_COLUMN_BY_TIME_PERIOD!r}[time_period]
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
    growth_rate = xl_cell(ctx, f'Inputs!{{column}}17')
    interest_rate = xl_cell(ctx, f'Inputs!{{column}}16')
    shock_activation = shock_active(ctx, time_period=time_period)
    shocked_primary_balance = primary_balance_shocked(ctx, time_period=time_period)
    growth_numerator = xl_add(growth_rate, xl_mul(growth_shock_factor, shock_activation))
    growth_term = xl_add(1.0, xl_div(growth_numerator, 100.0))
    interest_denominator = xl_add(interest_rate, xl_mul(interest_shock_factor, shock_activation))
    interest_term = xl_add(1.0, xl_div(interest_denominator, 100.0))
    debt_ratio = xl_mul(prior_debt, xl_div(growth_term, interest_term))
    return xl_sub(debt_ratio, shocked_primary_balance)\
"""

OUTPUT_DELTA_HELPER = f"""\
def output_delta(ctx, time_period: int):
{_quoted_docstring(OUTPUT_DELTA_DOCSTRING)}    return xl_sub(
        debt_to_gdp(ctx, time_period=time_period),
        baseline_debt(ctx, time_period=time_period),
    )\
"""


TIME_PERIOD_PARAMETER = (
    HelperParameter(name="time_period", concept="TIME_PERIOD", dtype="int"),
)


def _member_keys(
    addresses: tuple[tuple[str, str, dict[str, BindingKeyValue]], ...],
) -> tuple[MemberKeys, ...]:
    return tuple(
        MemberKeys(
            address=address,
            function_name=function_name,
            keys=keys,
        )
        for address, function_name, keys in addresses
    )


ENGINE_ROW_MEMBER_KEYS = {
    10: _member_keys(
        (
            ("Engine!C10", "cell_engine_c10", {"TIME_PERIOD": 1}),
            ("Engine!D10", "cell_engine_d10", {"TIME_PERIOD": 2}),
            ("Engine!E10", "cell_engine_e10", {"TIME_PERIOD": 3}),
            ("Engine!F10", "cell_engine_f10", {"TIME_PERIOD": 4}),
            ("Engine!G10", "cell_engine_g10", {"TIME_PERIOD": 5}),
        )
    ),
    16: _member_keys(
        (
            ("Engine!C16", "cell_engine_c16", {"TIME_PERIOD": 1}),
            ("Engine!D16", "cell_engine_d16", {"TIME_PERIOD": 2}),
            ("Engine!E16", "cell_engine_e16", {"TIME_PERIOD": 3}),
            ("Engine!F16", "cell_engine_f16", {"TIME_PERIOD": 4}),
            ("Engine!G16", "cell_engine_g16", {"TIME_PERIOD": 5}),
        )
    ),
    6: _member_keys(
        (
            ("Engine!C6", "cell_engine_c6", {"TIME_PERIOD": 1}),
            ("Engine!D6", "cell_engine_d6", {"TIME_PERIOD": 2}),
            ("Engine!E6", "cell_engine_e6", {"TIME_PERIOD": 3}),
            ("Engine!F6", "cell_engine_f6", {"TIME_PERIOD": 4}),
            ("Engine!G6", "cell_engine_g6", {"TIME_PERIOD": 5}),
        )
    ),
    20: _member_keys(
        (
            ("Engine!C20", "cell_engine_c20", {"TIME_PERIOD": 1}),
            ("Engine!D20", "cell_engine_d20", {"TIME_PERIOD": 2}),
            ("Engine!E20", "cell_engine_e20", {"TIME_PERIOD": 3}),
            ("Engine!F20", "cell_engine_f20", {"TIME_PERIOD": 4}),
            ("Engine!G20", "cell_engine_g20", {"TIME_PERIOD": 5}),
        )
    ),
}

OUTPUT_ROW_MEMBER_KEYS = _member_keys(
    (
        ("Outputs!B14", "cell_outputs_b14", {"TIME_PERIOD": 1}),
        ("Outputs!C14", "cell_outputs_c14", {"TIME_PERIOD": 2}),
        ("Outputs!D14", "cell_outputs_d14", {"TIME_PERIOD": 3}),
        ("Outputs!E14", "cell_outputs_e14", {"TIME_PERIOD": 4}),
        ("Outputs!F14", "cell_outputs_f14", {"TIME_PERIOD": 5}),
    )
)


GOLDEN_CLUSTER_REFACTOR_RESPONSES: dict[int, ClusterRefactorResponse] = {
    10: ClusterRefactorResponse(
        helper_name="shock_active",
        helper_docstring=SHOCK_ACTIVE_DOCSTRING,
        uses_first_year_branch=False,
        parameters=TIME_PERIOD_PARAMETER,
        helper_source=SHOCK_ACTIVE_HELPER,
        member_keys=ENGINE_ROW_MEMBER_KEYS[10],
    ),
    16: ClusterRefactorResponse(
        helper_name="primary_balance_shocked",
        helper_docstring=PRIMARY_BALANCE_SHOCKED_DOCSTRING,
        uses_first_year_branch=False,
        parameters=TIME_PERIOD_PARAMETER,
        helper_source=PRIMARY_BALANCE_SHOCKED_HELPER,
        member_keys=ENGINE_ROW_MEMBER_KEYS[16],
    ),
    6: ClusterRefactorResponse(
        helper_name="baseline_debt",
        helper_docstring=BASELINE_DEBT_DOCSTRING,
        uses_first_year_branch=True,
        parameters=TIME_PERIOD_PARAMETER,
        helper_source=BASELINE_DEBT_HELPER,
        member_keys=ENGINE_ROW_MEMBER_KEYS[6],
    ),
    20: ClusterRefactorResponse(
        helper_name="debt_to_gdp",
        helper_docstring=DEBT_TO_GDP_DOCSTRING,
        uses_first_year_branch=True,
        parameters=TIME_PERIOD_PARAMETER,
        helper_source=DEBT_TO_GDP_HELPER,
        member_keys=ENGINE_ROW_MEMBER_KEYS[20],
    ),
    14: ClusterRefactorResponse(
        helper_name="output_delta",
        helper_docstring=OUTPUT_DELTA_DOCSTRING,
        uses_first_year_branch=False,
        parameters=TIME_PERIOD_PARAMETER,
        helper_source=OUTPUT_DELTA_HELPER,
        member_keys=OUTPUT_ROW_MEMBER_KEYS,
    ),
}
