from __future__ import annotations

from .runtime import CellValue, EvalContext, xl_cell, xl_range

# --- Series binding readers ---

_LEAF_INDEX_COUNTRY_NAME = {
    (): 'Inputs!B5',
}

def read_country_name(ctx: EvalContext) -> CellValue:
    """Return the country name currently selected in the Inputs sheet.

    Returns the selected country name from the Inputs sheet.
    A single record representing the value from cell Inputs!B5.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - OBS_VALUE: The name of the selected country, chosen from the country profile table.
            Optional record fields:
                - PARAMETER: Identifies the parameter represented by this record. If supplied, expected value: "country_name".

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

    Returns the initial debt-to-GDP ratio for each country in the profile table.
    Each record corresponds to a row in the table at Inputs!A10:C12, where the COUNTRY is the row label and OBS_VALUE is the value in column B.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - COUNTRY: The country name, as it appears in the country profile table.
                - OBS_VALUE: The initial debt-to-GDP ratio, expressed as a percentage of GDP.
            Optional record fields:
                - INDICATOR: Identifies the series as the initial debt-to-GDP ratio. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: Unit of measure for the observation value, expressed as percent of GDP. If supplied, expected value: "PC_GDP".

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
    """Read the initial debt-to-GDP values from the country profile lookup table.

    Returns records of each country's initial debt-to-GDP ratio.
    Each record maps a country name from column A to its initial debt ratio from column B in the country profile table.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - COUNTRY: The country name as listed in the country profile table.
                - OBS_VALUE: The initial debt-to-GDP ratio as a percentage of GDP.
            Optional record fields:
                - INDICATOR: A constant indicator label for the series. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: The unit of measure for the observation value. If supplied, expected value: "PC_GDP".

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
    """Read the baseline real GDP growth rates for projection years 1 through 5.

    Returns the specified projection years and their corresponding baseline real GDP growth rates.
    Each record corresponds to a cell in the `growth_baseline` range (Inputs!C16:G16), with `TIME_PERIOD` from column headers and `OBS_VALUE` from the data cells.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year, an integer from 1 to 5.
                - OBS_VALUE: Baseline real GDP growth rate for the projection year, expressed as a percentage per annum.
            Optional record fields:
                - INDICATOR: Economic indicator constant identifying the series. If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: Unit of measure for the observation value. If supplied, expected value: "PERCENT_PER_ANNUM".

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
    """Read the baseline real GDP growth path from the Inputs sheet.

    Returns the baseline real GDP growth rate for each projection year (1–5) as a list of records.
    Each record corresponds to one cell in the `growth_baseline` range (Inputs!C16:G16), with the year from the column header and the growth rate from the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: The projection year (1 to 5).
                - OBS_VALUE: The baseline real GDP growth rate.
            Optional record fields:
                - INDICATOR: The economic indicator represented by the series. If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: The unit of measurement for the rate. If supplied, expected value: "PERCENT_PER_ANNUM".

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
    """Read baseline real interest rates from the Inputs sheet.

    Returns the baseline real interest rates for projection years 1 through 5.
    Each record maps to one column in the `interest_baseline` range (Inputs!C17:G17); TIME_PERIOD is derived from the column header and OBS_VALUE from the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 through 5).
                - OBS_VALUE: Baseline real interest rate.
            Optional record fields:
                - INDICATOR: Identifies the series as the real interest rate baseline. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: Unit of measure for the interest rate. If supplied, expected value: "PERCENT_PER_ANNUM".

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
    """Read baseline real interest rates for projection years.

    Returns the baseline real interest rates for the projection horizon (years 1 through 5) as a list of records.
    Each record corresponds to one projection year, with TIME_PERIOD from the column header and OBS_VALUE from the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 to 5).
                - OBS_VALUE: Real interest rate in percent per annum.
            Optional record fields:
                - INDICATOR: Indicator type for this series. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: Unit of measurement for the interest rate. If supplied, expected value: "PERCENT_PER_ANNUM".

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

    Returns the baseline primary balance values for each projection year.
    Each record corresponds to a cell in the Inputs!C18:G18 range, where TIME_PERIOD identifies the projection year and OBS_VALUE provides the primary balance.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: The projection year.
                - OBS_VALUE: The primary balance value as a percentage of GDP.
            Optional record fields:
                - INDICATOR: Identifier for the indicator series. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: Unit of measure of the observation value. If supplied, expected value: "PC_GDP".

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
    """Read the baseline primary balance path from the Inputs sheet.

    Returns a list of records representing the baseline primary balance path for projection years 1 through 5.
    Each record corresponds to a cell in the baseline primary balance range (Inputs!C18:G18); the column header provides the projection year, and the cell value provides the observation value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: The projection year, from 1 to 5.
                - OBS_VALUE: The baseline primary balance as a percentage of GDP. A positive value indicates a surplus.
            Optional record fields:
                - INDICATOR: Identifies this series as the primary balance. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: The unit of measurement for the observation value. If supplied, expected value: "PC_GDP".

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
    """Return the year in which the shock first applies.

    Returns the shock year currently set in the workbook.
    Each record corresponds to the single scalar cell Inputs!B21.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - OBS_VALUE: The first projection year in which the shock takes effect.
            Optional record fields:
                - PARAMETER: Identifies the parameter as the shock year. If supplied, expected value: "shock_year".

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
    """Read the shock type, which indicates the macroeconomic parameter that the shock modifies.

    Return the shock type code (an integer) specifying whether the shock applies to growth, the interest rate, or the primary balance.
    Returns a single record with the shock type integer in the `OBS_VALUE` field.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - OBS_VALUE: The shock type code. 1 corresponds to real GDP growth, 2 to the real interest rate, and 3 to the primary balance.
            Optional record fields:
                - PARAMETER: The parameter identifier, constant for this series. If supplied, expected value: "shock_type".

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
    """Read the shock magnitude values configured for each shock parameter.

    Returns the shock magnitudes as records keyed by the affected shock parameter.
    Each record corresponds to a cell in the shock table (Inputs!B26:D26), where the column header provides the SHOCK_PARAMETER key and the cell value provides the OBS_VALUE magnitude.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - SHOCK_PARAMETER: Name of the shock parameter (e.g., Growth, Interest, Primary Balance).
                - OBS_VALUE: Shock magnitude value, in the unit specified by UNIT_MEASURE.
            Optional record fields:
                - PARAMETER: Identifies this series as containing the shock magnitudes. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: Unit of measure for the shock magnitude values. If supplied, expected value: "PP".

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
    """Read the shock magnitudes for each affected parameter from the Shock Table.

    Returns the magnitude, in percentage points, of the shock applied to each parameter (growth, interest rate, primary balance) as configured in the Shock Table.
    Each record corresponds to one cell in the Shock Table (Inputs!B26:D26); the column header (trimmed) maps to the SHOCK_PARAMETER field, and the cell value maps to OBS_VALUE.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - SHOCK_PARAMETER: The parameter affected by the shock (e.g., growth, interest rate, primary balance).
                - OBS_VALUE: The shock magnitude, expressed in percentage points.
            Optional record fields:
                - PARAMETER: Constant field that classifies the record as containing shock magnitude data. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: The unit of measurement for the observation value. If supplied, expected value: "PP".

    Source binding:
        Workbook range: Inputs!B26:D26
        Layout: series
        Value type: float

    Examples:
        read_shock_magnitudes_range(ctx=ctx)
    """
    return xl_range(ctx, 'Inputs!B26:D26')
