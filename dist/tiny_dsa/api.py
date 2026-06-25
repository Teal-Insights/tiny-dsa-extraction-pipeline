from __future__ import annotations

from .data import CONSTANTS, DEFAULT_INPUTS
from .internals import _resolve_formula
from .runtime import EvalContext, coerce_inputs_dict, xl_cell, xl_range
import warnings


def make_context(inputs=None):
    """Create an EvalContext with merged inputs."""
    merged = dict(DEFAULT_INPUTS)
    merged.update(CONSTANTS)
    if inputs is not None:
        merged.update(inputs)
    return EvalContext(inputs=coerce_inputs_dict(merged), resolver=_resolve_formula, iterative_enabled=False, iterate_count=100, iterate_delta=0.001)


# --- Series binding setters (Records API) ---

Scalar = str | int | float | bool | None
Record = dict[str, object]
Records = list[Record]

def _coerce_records(records, measure_field, *, allow_scalar=False) -> Records:
    if not allow_scalar:
        return records
    if not isinstance(records, list):
        if isinstance(records, dict):
            return [records]
        return [{measure_field: records}]
    return records

def _apply_series_records(
    ctx,
    records,
    *,
    key_fields,
    allowed_fields,
    measure_field,
    leaf_index,
    strict,
    fn_name,
    allow_address=False,
    requires_address=False,
) -> None:
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(
                f"record[{index}]: address required for {fn_name} (duplicate keys in binding)"
            )
        if address is None:
            if not requires_address:
                missing = [field for field in key_fields if field not in record]
                if missing:
                    raise ValueError(f"record[{index}]: missing key fields {missing!r}")
                key_tuple = tuple((field, record[field]) for field in key_fields)
                address = leaf_index.get(key_tuple)
                if address is None:
                    raise ValueError(
                        f"record[{index}]: no leaf matches key {dict(key_tuple)!r}"
                    )
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_COUNTRY_NAME = {
    (): 'Inputs!B5',
}

def set_country_name(
    ctx: EvalContext,
    records: Records | Record | Scalar,
    *,
    strict: bool = True,
) -> None:
    """Set the country name for the debt sustainability analysis.

    Updates the country name in the Inputs sheet, which drives the initial debt-to-GDP lookup from the country profile table.
    Each record corresponds to the scalar input cell Inputs!B5; the OBS_VALUE field provides the value to write.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - OBS_VALUE: The country name to set, as listed in the country profile table.
            Optional record fields:
                - PARAMETER: Identifies the parameter as the country name. If supplied, expected value: "country_name".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B5
        Layout: scalar
        Value type: string

    Examples:
        set_country_name(ctx, [
            {'OBS_VALUE': 'Borvelia'},
        ])
    """
    _apply_series_records(
        ctx,
        _coerce_records(records, 'OBS_VALUE', allow_scalar=True),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_COUNTRY_NAME,
        strict=strict,
        fn_name='set_country_name',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_COUNTRY_INITIAL_DEBT = {
    (('COUNTRY', 'Borvelia'),): 'Inputs!B10',
    (('COUNTRY', 'Litellia'),): 'Inputs!B11',
    (('COUNTRY', 'Aurelium'),): 'Inputs!B12',
}

def set_country_initial_debt(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Set the country initial debt values in the Tiny-DSA workbook’s country profile table.

    Updates the initial debt-to-GDP series for the countries in the country profile lookup table.
    Each record corresponds to a row in the country profile table; the COUNTRY is written to column A and the OBS_VALUE to column B.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - COUNTRY: The country name.
                - OBS_VALUE: The initial debt-to-GDP ratio.
            Optional record fields:
                - INDICATOR: The indicator type for this series. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: The unit of measure for the observation value. If supplied, expected value: "PC_GDP".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B10:B12
        Layout: series
        Value type: float

    Examples:
        set_country_initial_debt(ctx, [
            {'COUNTRY': 'Borvelia', 'OBS_VALUE': 60.0},
            {'COUNTRY': 'Litellia', 'OBS_VALUE': 80.0},
        ])
    """
    _apply_series_records(
        ctx,
        records,
        key_fields=('COUNTRY',),
        allowed_fields=frozenset({'COUNTRY', 'INDICATOR', 'OBS_VALUE', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_COUNTRY_INITIAL_DEBT,
        strict=strict,
        fn_name='set_country_initial_debt',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_GROWTH_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C16',
    (('TIME_PERIOD', 2),): 'Inputs!D16',
    (('TIME_PERIOD', 3),): 'Inputs!E16',
    (('TIME_PERIOD', 4),): 'Inputs!F16',
    (('TIME_PERIOD', 5),): 'Inputs!G16',
}

def set_growth_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Set the baseline real GDP growth rates for projection years 1 through 5.

    Updates the growth_baseline series on the Inputs sheet with new values for each year.
    Each record provides a projection year (TIME_PERIOD) and the corresponding real GDP growth rate (OBS_VALUE), which are written to the Inputs sheet range C16:G16.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - TIME_PERIOD: Projection year.
                - OBS_VALUE: Real GDP growth rate for the projection year.
            Optional record fields:
                - INDICATOR: Economic indicator identifier (constant). If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: Unit of measurement for the series (constant). If supplied, expected value: "PERCENT_PER_ANNUM".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!C16:G16
        Layout: series
        Value type: float

    Examples:
        set_growth_baseline(ctx, [
            {'TIME_PERIOD': 1, 'OBS_VALUE': 3.5},
            {'TIME_PERIOD': 2, 'OBS_VALUE': 3.5},
        ])
    """
    _apply_series_records(
        ctx,
        records,
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_GROWTH_BASELINE,
        strict=strict,
        fn_name='set_growth_baseline',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_INTEREST_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C17',
    (('TIME_PERIOD', 2),): 'Inputs!D17',
    (('TIME_PERIOD', 3),): 'Inputs!E17',
    (('TIME_PERIOD', 4),): 'Inputs!F17',
    (('TIME_PERIOD', 5),): 'Inputs!G17',
}

def set_interest_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Set baseline real interest rates for the five-year projection horizon.

    Updates the real interest rate assumption in the baseline scenario.
    Each record corresponds to one cell in the `interest_baseline` named range (Inputs!C17:G17), matched by the TIME_PERIOD key.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - TIME_PERIOD: Projection year index, from 1 to 5.
                - OBS_VALUE: Real interest rate value, in percent per annum.
            Optional record fields:
                - INDICATOR: Constant identifier denoting the real interest rate series. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: Unit of measure for the interest rate observation. If supplied, expected value: "PERCENT_PER_ANNUM".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!C17:G17
        Layout: series
        Value type: float

    Examples:
        set_interest_baseline(ctx, [
            {'TIME_PERIOD': 1, 'OBS_VALUE': 4.0},
            {'TIME_PERIOD': 2, 'OBS_VALUE': 4.0},
        ])
    """
    _apply_series_records(
        ctx,
        records,
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_INTEREST_BASELINE,
        strict=strict,
        fn_name='set_interest_baseline',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_PRIMARY_BALANCE_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C18',
    (('TIME_PERIOD', 2),): 'Inputs!D18',
    (('TIME_PERIOD', 3),): 'Inputs!E18',
    (('TIME_PERIOD', 4),): 'Inputs!F18',
    (('TIME_PERIOD', 5),): 'Inputs!G18',
}

def set_primary_balance_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Set the baseline primary balance trajectory for projection years 1 through 5.

    Updates the primary balance path used to compute the baseline debt-to-GDP trajectory in the DSA workbook.
    Each record maps to a cell in the Inputs!C18:G18 range: TIME_PERIOD selects the year column, and OBS_VALUE is written to that cell.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - TIME_PERIOD: Projection year identifier, from 1 to 5.
                - OBS_VALUE: Primary balance as a percentage of GDP, where positive values denote a surplus.
            Optional record fields:
                - INDICATOR: Identifies the series as the primary balance. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: Indicates the values are expressed as a percentage of GDP. If supplied, expected value: "PC_GDP".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!C18:G18
        Layout: series
        Value type: float

    Examples:
        set_primary_balance_baseline(ctx, [
            {'TIME_PERIOD': 1, 'OBS_VALUE': -1.0},
            {'TIME_PERIOD': 2, 'OBS_VALUE': -0.5},
        ])
    """
    _apply_series_records(
        ctx,
        records,
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_PRIMARY_BALANCE_BASELINE,
        strict=strict,
        fn_name='set_primary_balance_baseline',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_SHOCK_YEAR = {
    (): 'Inputs!B21',
}

def set_shock_year(
    ctx: EvalContext,
    records: Records | Record | Scalar,
    *,
    strict: bool = True,
) -> None:
    """Set the first projection year in which the selected shock applies.

    Updates the shock_year scalar input on the Inputs sheet to the given year.
    Each record supplies the OBS_VALUE to write to the single-cell named range `shock_year`.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - OBS_VALUE: The projection year, an integer between 1 and 5, in which the shock takes effect and persists through the horizon.
            Optional record fields:
                - PARAMETER: The parameter identifier; always 'shock_year' for this setter. If supplied, expected value: "shock_year".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B21
        Layout: scalar
        Value type: int

    Examples:
        set_shock_year(ctx, [
            {'OBS_VALUE': 2},
        ])
    """
    _apply_series_records(
        ctx,
        _coerce_records(records, 'OBS_VALUE', allow_scalar=True),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_SHOCK_YEAR,
        strict=strict,
        fn_name='set_shock_year',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_SHOCK_TYPE = {
    (): 'Inputs!B22',
}

def set_shock_type(
    ctx: EvalContext,
    records: Records | Record | Scalar,
    *,
    strict: bool = True,
) -> None:
    """Set the shock type code indicating which parameter the shock affects.

    Write the selected shock type integer to the Inputs sheet.
    The OBS_VALUE field is written directly to the scalar cell Inputs!B22.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - OBS_VALUE: Integer code selecting the parameter affected by the shock: 1 for real GDP growth, 2 for real interest rate, 3 for primary balance.
            Optional record fields:
                - PARAMETER: Constant attribute identifying this series as the shock type parameter. If supplied, expected value: "shock_type".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B22
        Layout: scalar
        Value type: int

    Examples:
        set_shock_type(ctx, [
            {'OBS_VALUE': 1},
        ])
    """
    _apply_series_records(
        ctx,
        _coerce_records(records, 'OBS_VALUE', allow_scalar=True),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_SHOCK_TYPE,
        strict=strict,
        fn_name='set_shock_type',
        allow_address=False,
        requires_address=False,
    )

_LEAF_INDEX_SHOCK_MAGNITUDES = {
    (('SHOCK_PARAMETER', 'Growth'),): 'Inputs!B26',
    (('SHOCK_PARAMETER', 'Interest'),): 'Inputs!C26',
    (('SHOCK_PARAMETER', 'Primary balance'),): 'Inputs!D26',
}

def set_shock_magnitudes(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Set shock magnitudes for the debt sustainability scenario.

    Update the three shock magnitude cells (Inputs!B26:D26) that define the step-change deviations applied to growth, the interest rate, or the primary balance under a configured shock.
    Records are matched to the shock_table columns by the SHOCK_PARAMETER key against the column headers in row 25, with OBS_VALUE written to the corresponding data cell.

    Args:
        records (Records): Records to apply to the workbook inputs.
            Required record fields:
                - SHOCK_PARAMETER: The name of the macro-fiscal variable whose shock magnitude is being set.
                - OBS_VALUE: The size of the shock, in percentage points, to be applied when a corresponding shock type is selected.
            Optional record fields:
                - PARAMETER: A constant field denoting the parameter role of the series. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: The measurement unit for the shock magnitude. If supplied, expected value: "PP".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B26:D26
        Layout: series
        Value type: float

    Examples:
        set_shock_magnitudes(ctx, [
            {'SHOCK_PARAMETER': 'Growth', 'OBS_VALUE': -2.0},
            {'SHOCK_PARAMETER': 'Interest', 'OBS_VALUE': 2.0},
        ])
    """
    _apply_series_records(
        ctx,
        records,
        key_fields=('SHOCK_PARAMETER',),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER', 'SHOCK_PARAMETER', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_SHOCK_MAGNITUDES,
        strict=strict,
        fn_name='set_shock_magnitudes',
        allow_address=False,
        requires_address=False,
    )

# --- Series binding output compute (Records API) ---

_OUTPUT_LEAVES_OUTPUT_BASELINE = [
    ('Outputs!B12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_baseline(ctx=None, *, inputs=None) -> Records:
    """Compute the baseline debt-to-GDP path records for the five-year projection horizon.

    Return the computed baseline debt-to-GDP trajectory as a series of records.
    Each record maps to one projection year; TIME_PERIOD is derived from column headers in row 11 and OBS_VALUE from the corresponding data cell in Outputs!B12:F12.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year index (1 to 5).
                - OBS_VALUE: Baseline debt-to-GDP ratio for the projection year.
            Optional record fields:
                - SCENARIO: Scenario identifier for the output series. If supplied, expected value: "baseline".
                - UNIT_MEASURE: Unit of measure for the observation values. If supplied, expected value: "PC_GDP".

    Source binding:
        Workbook range: Outputs!B12:F12
        Layout: series
        Value type: float

    Examples:
        compute_output_baseline(ctx=ctx)
    """
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_BASELINE:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records

_OUTPUT_LEAVES_OUTPUT_SHOCKED = [
    ('Outputs!B13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_shocked(ctx=None, *, inputs=None) -> Records:
    """Compute the shocked debt-to-GDP ratio path over the projection horizon.

    Returns the debt-to-GDP ratio under the shocked scenario for each projection year (1 to 5).
    Each record corresponds to a single column in the Outputs!B13:F13 range, keyed by projection year.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: The projection year index (1 to 5).
                - OBS_VALUE: The shocked debt-to-GDP ratio, expressed in percent of GDP.
            Optional record fields:
                - SCENARIO: Identifies the record as belonging to the shocked scenario. If supplied, expected value: "shocked".
                - UNIT_MEASURE: Indicates that the observation is measured as a percent of GDP. If supplied, expected value: "PC_GDP".

    Source binding:
        Workbook range: Outputs!B13:F13
        Layout: series
        Value type: float

    Examples:
        compute_output_shocked(ctx=ctx)
    """
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_SHOCKED:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records

_OUTPUT_LEAVES_OUTPUT_DELTA = [
    ('Outputs!B14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!C14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!D14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!E14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!F14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PP'}),
]

def compute_output_delta(ctx=None, *, inputs=None) -> Records:
    """Return the difference between shocked and baseline debt-to-GDP projections for years 1 to 5.

    Provides the delta (shocked minus baseline) computed from the Outputs sheet's output_delta range.
    Each record corresponds to a cell in Outputs!B14:F14, with TIME_PERIOD from the column header and OBS_VALUE as the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year within the five-year horizon.
                - OBS_VALUE: Difference between the shocked and baseline debt-to-GDP ratio for the year.
            Optional record fields:
                - SCENARIO: Scenario identifier indicating the delta series. If supplied, expected value: "shocked_minus_baseline".
                - UNIT_MEASURE: Unit of measurement for the observation values. If supplied, expected value: "PP".

    Source binding:
        Workbook range: Outputs!B14:F14
        Layout: series
        Value type: float

    Examples:
        compute_output_delta(ctx=ctx)
    """
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_DELTA:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records


TARGETS = {
    'Outputs!B12:Outputs!F12': xl_range,
    'Outputs!B13:Outputs!F13': xl_range,
    'Outputs!B14:Outputs!F14': xl_range,
}


def compute_all(ctx=None, *, inputs=None):
    """Compute all target cells and return results."""
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn(
            "inputs will be ignored because ctx was provided",
            UserWarning,
            stacklevel=2,
        )
    return {target: handler(ctx, target) for target, handler in TARGETS.items()}
