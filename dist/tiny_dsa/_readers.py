from __future__ import annotations

from .runtime import CellValue, EvalContext, xl_cell, xl_range

# --- Series binding readers ---

_LEAF_INDEX_COUNTRY_NAME = {
    (): 'Inputs!B5',
}

def read_country_name(ctx: EvalContext) -> CellValue:
    """Read the currently selected country name from the Inputs sheet.

    Returns the user-selected country name that controls the initial debt-to-GDP lookup and identifies the active scenario.
    This scalar series maps to the single cell Inputs!B5; the constant parameter attribute appears on every record.

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
    """Read the initial debt-to-GDP ratios from the country profile lookup table.

    Returns one record per country containing its initial debt-to-GDP observation value.
    Each record corresponds to a row in the country profile lookup table, with COUNTRY read from the row label in column A and OBS_VALUE read from the data cell in column B.

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
    """Read the initial debt-to-GDP values from the country profile lookup table as a series keyed by country.

    Returns the initial debt-to-GDP ratio for each country profile in the lookup table.
    Each returned record corresponds to one row in the country profile lookup table, with the country name read from column A and the initial debt-to-GDP value read from the corresponding data cell in column B.

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
    """Read the baseline real GDP growth rates for projection years 1 through 5 from the Inputs sheet.

    Returns the baseline real GDP growth time series as a set of records, one per projection year.
    Each record corresponds to a projection-year column in the growth_baseline range, with TIME_PERIOD taken from the column header and OBS_VALUE from the data cell.

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
    """Read the baseline real GDP growth series for projection years 1 through 5.

    Returns the baseline real GDP growth rates for each projection year from the Inputs sheet.
    Each record corresponds to one projection-year cell in the growth baseline range, with TIME_PERIOD taken from the column header and OBS_VALUE from the cell value.

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
    """Read the baseline real interest rate series for projection years 1 through 5.

    Returns the baseline real interest rates used in the debt-dynamics recursion for each projection year.
    Each record contains one projection-year observation read from the corresponding cell in the interest_baseline range.

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
    """Read the baseline real interest rate series for projection years 1 through 5 from the Inputs sheet.

    Returns the baseline real interest rates for years 1 through 5 as records keyed by projection year.
    Each projection year from the column header is paired with the corresponding observation value in the data range, and the series-level indicator is applied to all returned records.

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
    """Read the baseline primary balance path for projection years 1 through 5.

    Returns records with the baseline primary balance for each projection year.
    Each record holds one projection year's observation, taken from the corresponding cell in the baseline primary balance data range; the projection year is read from the column header.

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

    Returns the baseline primary balance series as a set of records with projection-year keys and observation values.
    Each record maps one projection-year cell in Inputs!C18:G18, with TIME_PERIOD taken from the column header and OBS_VALUE taken from the cell value.

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

    Returns the first projection year in which the selected shock applies.
    The scalar cell Inputs!B21 is read as a single record containing the observation value.

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
    """Read the user-selected shock type from the Inputs sheet.

    Returns the shock type that determines which baseline parameter the configured shock magnitude is applied to.
    A single record is returned whose observation value is the current value of the shock_type cell on the Inputs sheet.

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
    """Read shock magnitudes keyed by affected parameter.

    Returns the shock magnitudes entered in the SHOCK TABLE section as a series of records.
    Each record corresponds to one cell in the shock magnitude range, with the column header on the Inputs sheet supplying the affected parameter.

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
    """Read the configured shock magnitudes for each shock parameter from the Inputs sheet.

    Returns the shock magnitudes for each shock parameter in the SHOCK TABLE section.
    Each record corresponds to one cell in the shock magnitudes data range, with the shock parameter identified by the cell's column header.

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

_LEAF_INDEX_COUNTRY_PROFILE_NAMES = {
    (('COUNTRY', 'Borvelia'),): 'Inputs!A10',
    (('COUNTRY', 'Litellia'),): 'Inputs!A11',
    (('COUNTRY', 'Aurelium'),): 'Inputs!A12',
}

def read_country_profile_names(
    ctx: EvalContext,
    *,
    country: str,
) -> CellValue:
    """Read country profile names from the country profile lookup table.

    Returns the country profile names listed in the country profile table on the Inputs sheet.
    Each record contains a country name from the row labels and the corresponding profile name from the data cells in the country profile table.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!A10:A12
        Layout: series
        Value type: string

    Examples:
        read_country_profile_names(ctx=ctx)
    """
    key_tuple = (('COUNTRY', country),)
    address = _LEAF_INDEX_COUNTRY_PROFILE_NAMES.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_country_profile_names_range(ctx: EvalContext) -> CellValue:
    """Read the country profile names range as a series of country-keyed records.

    Returns the country names from the country profile table that serve as MATCH lookup keys for the country selector.
    Each cell in Inputs!A10:A12 produces one record: the row label in column A provides the COUNTRY key, and the cell value provides the observation value OBS_VALUE.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Inputs!A10:A12
        Layout: series
        Value type: string

    Examples:
        read_country_profile_names_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!A10:A12')

_LEAF_INDEX_ENGINE_YEAR_LABELS = {
    (('TIME_PERIOD', 1),): 'Engine!C5',
    (('TIME_PERIOD', 2),): 'Engine!D5',
    (('TIME_PERIOD', 3),): 'Engine!E5',
    (('TIME_PERIOD', 4),): 'Engine!F5',
    (('TIME_PERIOD', 5),): 'Engine!G5',
}

def read_engine_year_labels(
    ctx: EvalContext,
    *,
    time_period: int,
) -> CellValue:
    """Reads the Engine baseline projection-year labels as a series of integer records.

    Returns the projection-year label for each column in the Engine baseline header range.
    Each record maps to one cell in Engine!C5:G5, with the projection year read from the column header and the cell value as the observation.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Engine!C5:G5
        Layout: series
        Value type: int

    Examples:
        read_engine_year_labels(ctx=ctx)
    """
    key_tuple = (('TIME_PERIOD', time_period),)
    address = _LEAF_INDEX_ENGINE_YEAR_LABELS.get(key_tuple)
    if address is None:
        raise ValueError(f"no leaf matches key {dict(key_tuple)!r}")
    return xl_cell(ctx, address)

def read_engine_year_labels_range(ctx: EvalContext) -> CellValue:
    """Read the projection-year label series from the Engine baseline header row.

    Returns the integer projection-year labels used on the Engine baseline path for comparison with the shock year.
    Each record corresponds to one column in the Engine!C5:G5 header range, keyed by TIME_PERIOD with the cell value as OBS_VALUE.

    Args:
        ctx (EvalContext): Evaluation context.

    Returns:
        CellValue: Value read from the bound cell or range.

    Source binding:
        Workbook range: Engine!C5:G5
        Layout: series
        Value type: int

    Examples:
        read_engine_year_labels_range(ctx=ctx)
    """
    return xl_range(ctx, 'Engine!C5:G5')
