from __future__ import annotations

from .runtime import CellValue, EvalContext, xl_cell, xl_range

# --- Series binding readers ---

_LEAF_INDEX_COUNTRY_NAME = {
    (): 'Inputs!B5',
}

def read_country_name(ctx: EvalContext) -> CellValue:
    """Read the selected country name from the Inputs sheet.

    Returns the current country name as a record.
    Each record corresponds to the scalar cell `country_name` (Inputs!B5).

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B5
        Layout: scalar
        Value type: string

    Examples:
        read_country_name(ctx=ctx)
    """
    return xl_cell(ctx, 'Inputs!B5')

_LEAF_INDEX_COUNTRY_INITIAL_DEBT = {
    (('COUNTRY', 'Borvelia'),): 'Inputs!B10',
    (('COUNTRY', 'Litellia'),): 'Inputs!B11',
    (('COUNTRY', 'Aurelium'),): 'Inputs!B12',
}

def read_country_initial_debt(
    ctx: EvalContext,
    *,
    country: str,
) -> CellValue:
    """Read the initial debt-to-GDP values from the country profile lookup table.

    Returns records containing each country's initial debt-to-GDP ratio as a percentage of GDP.
    Each record corresponds to a row in the country profile table (Inputs!A10:C12), with the COUNTRY label in column A and the OBS_VALUE in column B.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B10:B12
        Layout: series
        Value type: float

    Examples:
        read_country_initial_debt(ctx=ctx)
    """
    key_tuple = (('COUNTRY', country),)
    address = _LEAF_INDEX_COUNTRY_INITIAL_DEBT.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_country_initial_debt_range(ctx: EvalContext) -> CellValue:
    """Return the initial debt-to-GDP ratios from the country profile lookup table.

    Returns the country-specific initial debt-to-GDP values for all countries in the table.
    Each record corresponds to a row in the input range, with the country name as key.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B10:B12
        Layout: series
        Value type: float

    Examples:
        read_country_initial_debt_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!B10:B12')

_LEAF_INDEX_GROWTH_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C16',
    (('TIME_PERIOD', 2),): 'Inputs!D16',
    (('TIME_PERIOD', 3),): 'Inputs!E16',
    (('TIME_PERIOD', 4),): 'Inputs!F16',
    (('TIME_PERIOD', 5),): 'Inputs!G16',
}

def read_growth_baseline(
    ctx: EvalContext,
    *,
    time_period: int,
) -> CellValue:
    """Read the baseline real GDP growth rates for projection years 1–5.

    Returns the baseline real GDP growth trajectory as a series of records.
    Each record corresponds to a cell in the range Inputs!C16:G16, with TIME_PERIOD derived from the column header in row 15 and OBS_VALUE from the cell value.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C16:G16
        Layout: series
        Value type: float

    Examples:
        read_growth_baseline(ctx=ctx)
    """
    key_tuple = (('TIME_PERIOD', time_period),)
    address = _LEAF_INDEX_GROWTH_BASELINE.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_growth_baseline_range(ctx: EvalContext) -> CellValue:
    """Read the baseline real GDP growth rates from the Inputs sheet.

    Returns records for the growth_baseline series representing projected real GDP growth rates for years 1 through 5.
    Each record corresponds to a cell in the range Inputs!C16:G16, with TIME_PERIOD derived from column headers and OBS_VALUE from cell values.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C16:G16
        Layout: series
        Value type: float

    Examples:
        read_growth_baseline_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!C16:G16')

_LEAF_INDEX_INTEREST_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C17',
    (('TIME_PERIOD', 2),): 'Inputs!D17',
    (('TIME_PERIOD', 3),): 'Inputs!E17',
    (('TIME_PERIOD', 4),): 'Inputs!F17',
    (('TIME_PERIOD', 5),): 'Inputs!G17',
}

def read_interest_baseline(
    ctx: EvalContext,
    *,
    time_period: int,
) -> CellValue:
    """Return the baseline real interest rate assumptions.

    Read the real interest rate trajectory from the Inputs sheet for the five projection years.
    Each record corresponds to one projection year: TIME_PERIOD is the year index, and OBS_VALUE holds the real interest rate.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C17:G17
        Layout: series
        Value type: float

    Examples:
        read_interest_baseline(ctx=ctx)
    """
    key_tuple = (('TIME_PERIOD', time_period),)
    address = _LEAF_INDEX_INTEREST_BASELINE.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_interest_baseline_range(ctx: EvalContext) -> CellValue:
    """Read the baseline real interest rate series for projection years 1 through 5.

    Returns the real interest rate path from the `interest_baseline` range (Inputs!C17:G17).
    Each record maps a projection year (`TIME_PERIOD`) to its corresponding cell value in the range, with year 1 corresponding to cell C17, year 2 to D17, and so on.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C17:G17
        Layout: series
        Value type: float

    Examples:
        read_interest_baseline_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!C17:G17')

_LEAF_INDEX_PRIMARY_BALANCE_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C18',
    (('TIME_PERIOD', 2),): 'Inputs!D18',
    (('TIME_PERIOD', 3),): 'Inputs!E18',
    (('TIME_PERIOD', 4),): 'Inputs!F18',
    (('TIME_PERIOD', 5),): 'Inputs!G18',
}

def read_primary_balance_baseline(
    ctx: EvalContext,
    *,
    time_period: int,
) -> CellValue:
    """Read the baseline primary balance path for projection years 1–5.

    Returns the baseline primary balance series as a percentage of GDP, with positive values indicating a surplus.
    Each record corresponds to one year in the five-year projection horizon.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C18:G18
        Layout: series
        Value type: float

    Examples:
        read_primary_balance_baseline(ctx=ctx)
    """
    key_tuple = (('TIME_PERIOD', time_period),)
    address = _LEAF_INDEX_PRIMARY_BALANCE_BASELINE.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_primary_balance_baseline_range(ctx: EvalContext) -> CellValue:
    """Read the baseline primary balance path for projection years 1 through 5.

    Returns the baseline primary balance values as a percentage of GDP for each projection year.
    Each record corresponds to one cell in Inputs!C18:G18, mapping the column header year to TIME_PERIOD and the cell value to OBS_VALUE.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!C18:G18
        Layout: series
        Value type: float

    Examples:
        read_primary_balance_baseline_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!C18:G18')

_LEAF_INDEX_SHOCK_YEAR = {
    (): 'Inputs!B21',
}

def read_shock_year(ctx: EvalContext) -> CellValue:
    """Read the shock year from the Inputs sheet.

    Returns the projection year (1–5) in which the selected shock first takes effect.
    Reads the integer value from cell B21 of the Inputs sheet and returns it as a single record.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B21
        Layout: scalar
        Value type: int

    Examples:
        read_shock_year(ctx=ctx)
    """
    return xl_cell(ctx, 'Inputs!B21')

_LEAF_INDEX_SHOCK_TYPE = {
    (): 'Inputs!B22',
}

def read_shock_type(ctx: EvalContext) -> CellValue:
    """Read the shock type code that determines which parameter the shock affects.

    Returns the configured shock type code, an integer indicating which parameter the shock affects.
    The function returns a single record representing the value in cell Inputs!B22.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B22
        Layout: scalar
        Value type: int

    Examples:
        read_shock_type(ctx=ctx)
    """
    return xl_cell(ctx, 'Inputs!B22')

_LEAF_INDEX_SHOCK_MAGNITUDES = {
    (('SHOCK_PARAMETER', 'Growth'),): 'Inputs!B26',
    (('SHOCK_PARAMETER', 'Interest'),): 'Inputs!C26',
    (('SHOCK_PARAMETER', 'Primary balance'),): 'Inputs!D26',
}

def read_shock_magnitudes(
    ctx: EvalContext,
    *,
    shock_parameter: str,
) -> CellValue:
    """Read the shock magnitudes for the three configurable shock parameters.

    Returns the shock magnitudes keyed by the affected shock parameter.
    Each record corresponds to a single cell in the shock magnitude row (Inputs!B26:D26), keyed by the shock parameter name taken from the column headers.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B26:D26
        Layout: series
        Value type: float

    Examples:
        read_shock_magnitudes(ctx=ctx)
    """
    key_tuple = (('SHOCK_PARAMETER', shock_parameter),)
    address = _LEAF_INDEX_SHOCK_MAGNITUDES.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_shock_magnitudes_range(ctx: EvalContext) -> CellValue:
    """Return the shock magnitudes from the Shock Table for all shock-affected parameters.

    Returns a list of records, each containing a shock parameter name and its associated magnitude, as read from the Shock Table.
    Each record corresponds to a cell in the Shock Table row (cells B26:D26), with the parameter name taken from the column header in row 25.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!B26:D26
        Layout: series
        Value type: float

    Examples:
        read_shock_magnitudes_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!B26:D26')
