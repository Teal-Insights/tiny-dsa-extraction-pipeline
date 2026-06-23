"""Golden singleton refactor responses for integration tests (no LLM/cache)."""

from __future__ import annotations

from src.internals_refactor import SingletonRefactorResponse

INITIAL_DEBT_TO_GDP_DOCSTRING = """\
Look up the initial debt-to-GDP ratio for the selected country.

Args:
    ctx: Workbook evaluation context.

Returns:
    Initial debt-to-GDP ratio from the country profile table.

Note:
    Covers Inputs!B6. Excel: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2).\
"""

SHOCK_MAGNITUDE_RESOLVED_DOCSTRING = """\
Resolve the shock magnitude for the selected shock type.

Args:
    ctx: Workbook evaluation context.

Returns:
    Shock magnitude from the shock table for Inputs!B22.

Note:
    Covers Engine!B9. Excel: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1).\
"""


def _quoted_docstring(text: str) -> str:
    return f'    """{text}"""\n'


INITIAL_DEBT_TO_GDP_SOURCE = f"""\
def initial_debt_to_gdp(ctx):
{_quoted_docstring(INITIAL_DEBT_TO_GDP_DOCSTRING)}    country_name = xl_cell(ctx, 'Inputs!B5')
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
    )\
"""

SHOCK_MAGNITUDE_RESOLVED_SOURCE = f"""\
def shock_magnitude_resolved(ctx):
{_quoted_docstring(SHOCK_MAGNITUDE_RESOLVED_DOCSTRING)}    shock_type = xl_cell(ctx, 'Inputs!B22')
    type_offset = xl_sub(shock_type, 1.0)
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, type_offset, None, None)\
"""


GOLDEN_SINGLETON_REFACTOR_RESPONSES: dict[str, SingletonRefactorResponse] = {
    "Inputs!B6": SingletonRefactorResponse(
        symbol_name="initial_debt_to_gdp",
        symbol_docstring=INITIAL_DEBT_TO_GDP_DOCSTRING,
        symbol_source=INITIAL_DEBT_TO_GDP_SOURCE,
    ),
    "Engine!B9": SingletonRefactorResponse(
        symbol_name="shock_magnitude_resolved",
        symbol_docstring=SHOCK_MAGNITUDE_RESOLVED_DOCSTRING,
        symbol_source=SHOCK_MAGNITUDE_RESOLVED_SOURCE,
    ),
}
