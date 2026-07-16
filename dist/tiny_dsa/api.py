from __future__ import annotations

import warnings

from ._api_helpers import (
    DataFrameInput,
    EmptyMeasure,
    Record,
    Records,
    Sequence,
    _apply_series_records,
    coerce_setter_input,
)
from ._readers import (
    _LEAF_INDEX_COUNTRY_NAME,
    _LEAF_INDEX_COUNTRY_INITIAL_DEBT,
    _LEAF_INDEX_GROWTH_BASELINE,
    _LEAF_INDEX_INTEREST_BASELINE,
    _LEAF_INDEX_PRIMARY_BALANCE_BASELINE,
    _LEAF_INDEX_SHOCK_YEAR,
    _LEAF_INDEX_SHOCK_TYPE,
    _LEAF_INDEX_SHOCK_MAGNITUDES,
)
from .data import CONSTANTS, DEFAULT_INPUTS
from .internals import _resolve_formula
from .runtime import EvalContext, coerce_inputs_dict, xl_cell, xl_range_rows


def make_context(inputs: dict[str, object] | None = None) -> EvalContext:
    """Create an EvalContext with merged inputs."""
    merged: dict[str, object] = dict(DEFAULT_INPUTS)
    merged.update(CONSTANTS)
    if inputs is not None:
        merged.update(inputs)
    return EvalContext(inputs=coerce_inputs_dict(merged), resolver=_resolve_formula, iterative_enabled=False, iterate_count=100, iterate_delta=0.001)


# --- Series binding setters (Records API) ---

def set_country_name(
    ctx: EvalContext,
    records: Records | Record | str,
    *,
    strict: bool = True,
) -> None:
    """Set the country name for the Tiny-DSA scenario.

    Updates the selected country in the Inputs sheet, which controls the initial debt-to-GDP ratio via the country profile table.
    The OBS_VALUE from each record is written to the workbook cell country_name (Inputs!B5).

    Args:
        records (Records | Record | str): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The country name to select.
            Optional record fields:
                - PARAMETER: Identifies this record as the country name parameter. If supplied, expected value: "country_name".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B5
        Layout: scalar
        Value type: string

    Examples:
        set_country_name(ctx, 'Borvelia')
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='scalar',
            key_fields=(),
            measure_field='OBS_VALUE',
            key_order=None,
            strict=strict,
            measure_dtype='string',
        ),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_COUNTRY_NAME,
        strict=strict,
        fn_name='set_country_name',
        allow_address=False,
        requires_address=False,
    )

_KEY_ORDER_COUNTRY_INITIAL_DEBT = ('Aurelium', 'Borvelia', 'Litellia')

def set_country_initial_debt(
    ctx: EvalContext,
    records: Records | Record | Sequence[float] | DataFrameInput,
    *,
    strict: bool = True,
    empty_measure: EmptyMeasure = "write",
) -> None:
    """Set initial debt-to-GDP ratios for the country profile lookup table.

    Updates the initial debt-to-GDP ratios for the pre-defined set of countries in the country profile table.
    Each record corresponds to a row in the country profile table (Inputs!A10:C12), keyed by the COUNTRY label in column A, and places the OBS_VALUE in column B.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - COUNTRY: Country identifier used as the lookup key.
                - OBS_VALUE: Initial debt-to-GDP ratio, in percent of GDP.
            Optional record fields:
                - INDICATOR: Economic indicator that this series represents. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: Unit of measure for the observation value. If supplied, expected value: "PC_GDP".

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

        set_country_initial_debt(ctx, [60.0, 80.0])
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='series',
            key_fields=('COUNTRY',),
            measure_field='OBS_VALUE',
            key_order=_KEY_ORDER_COUNTRY_INITIAL_DEBT,
            strict=strict,
            empty_measure=empty_measure,
            requires_address=False,
            key_dtypes={'COUNTRY': 'string'},
            measure_dtype='float',
        ),
        key_fields=('COUNTRY',),
        allowed_fields=frozenset({'COUNTRY', 'INDICATOR', 'OBS_VALUE', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_COUNTRY_INITIAL_DEBT,
        strict=strict,
        fn_name='set_country_initial_debt',
        allow_address=False,
        requires_address=False,
    )

_KEY_ORDER_GROWTH_BASELINE = (1, 2, 3, 4, 5)

def set_growth_baseline(
    ctx: EvalContext,
    records: Records | Record | Sequence[float] | DataFrameInput,
    *,
    strict: bool = True,
    empty_measure: EmptyMeasure = "write",
) -> None:
    """Set baseline real GDP growth rates for projection years 1 through 5.

    Updates the baseline real GDP growth path used in the debt recursion.
    Each record maps to a single cell in the `growth_baseline` range: `TIME_PERIOD` identifies the year, `OBS_VALUE` provides the growth rate.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year index (1 to 5).
                - OBS_VALUE: Real GDP growth rate expressed as a percentage per annum.
            Optional record fields:
                - INDICATOR: Series indicator identifying the data as real GDP growth. If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: Unit of measurement for the growth rate values. If supplied, expected value: "PERCENT_PER_ANNUM".

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

        set_growth_baseline(ctx, [3.5, 3.5])
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='series',
            key_fields=('TIME_PERIOD',),
            measure_field='OBS_VALUE',
            key_order=_KEY_ORDER_GROWTH_BASELINE,
            strict=strict,
            empty_measure=empty_measure,
            requires_address=False,
            key_dtypes={'TIME_PERIOD': 'int'},
            measure_dtype='float',
        ),
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_GROWTH_BASELINE,
        strict=strict,
        fn_name='set_growth_baseline',
        allow_address=False,
        requires_address=False,
    )

_KEY_ORDER_INTEREST_BASELINE = (1, 2, 3, 4, 5)

def set_interest_baseline(
    ctx: EvalContext,
    records: Records | Record | Sequence[float] | DataFrameInput,
    *,
    strict: bool = True,
    empty_measure: EmptyMeasure = "write",
) -> None:
    """Set baseline real interest rate assumptions for the five-year projection horizon.

    Updates the baseline real interest rate path for each projection year.
    Each record corresponds to a projection year, with TIME_PERIOD indicating the year and OBS_VALUE the real interest rate for that year.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: The projection year within the five-year horizon.
                - OBS_VALUE: The baseline real interest rate for the year, expressed as the effective real rate on general-government debt, in percent per annum.
            Optional record fields:
                - INDICATOR: The economic indicator associated with this series. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: The unit of measure for the interest rate. If supplied, expected value: "PERCENT_PER_ANNUM".

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

        set_interest_baseline(ctx, [4.0, 4.0])
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='series',
            key_fields=('TIME_PERIOD',),
            measure_field='OBS_VALUE',
            key_order=_KEY_ORDER_INTEREST_BASELINE,
            strict=strict,
            empty_measure=empty_measure,
            requires_address=False,
            key_dtypes={'TIME_PERIOD': 'int'},
            measure_dtype='float',
        ),
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_INTEREST_BASELINE,
        strict=strict,
        fn_name='set_interest_baseline',
        allow_address=False,
        requires_address=False,
    )

_KEY_ORDER_PRIMARY_BALANCE_BASELINE = (1, 2, 3, 4, 5)

def set_primary_balance_baseline(
    ctx: EvalContext,
    records: Records | Record | Sequence[float] | DataFrameInput,
    *,
    strict: bool = True,
    empty_measure: EmptyMeasure = "write",
) -> None:
    """Set the baseline primary balance path for projection years 1 through 5.

    Updates the primary balance baseline values, expressed as percent of GDP with positive values denoting a surplus.
    Each record corresponds to one projection year; TIME_PERIOD maps to the column header (1–5), and OBS_VALUE sets the primary balance for that year.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year (1 through 5).
                - OBS_VALUE: Primary balance value, expressed as percent of GDP (positive values denote a surplus).
            Optional record fields:
                - INDICATOR: The indicator type. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: The unit of measure for the values. If supplied, expected value: "PC_GDP".

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

        set_primary_balance_baseline(ctx, [-1.0, -0.5])
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='series',
            key_fields=('TIME_PERIOD',),
            measure_field='OBS_VALUE',
            key_order=_KEY_ORDER_PRIMARY_BALANCE_BASELINE,
            strict=strict,
            empty_measure=empty_measure,
            requires_address=False,
            key_dtypes={'TIME_PERIOD': 'int'},
            measure_dtype='float',
        ),
        key_fields=('TIME_PERIOD',),
        allowed_fields=frozenset({'INDICATOR', 'OBS_VALUE', 'TIME_PERIOD', 'UNIT_MEASURE'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_PRIMARY_BALANCE_BASELINE,
        strict=strict,
        fn_name='set_primary_balance_baseline',
        allow_address=False,
        requires_address=False,
    )

def set_shock_year(
    ctx: EvalContext,
    records: Records | Record | int,
    *,
    strict: bool = True,
) -> None:
    """Set the projection year for shock activation.

    Updates the shock year cell (Inputs!B21) with the integer value representing the first projection period in which the shock applies.
    Each record provides an integer observation value; the setter normalizes the input into records, checks record shape and key matching, and writes the value to cell Inputs!B21.

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The first projection year (integer) in which the configured shock becomes active.
            Optional record fields:
                - PARAMETER: A constant string identifier for the shock year parameter. If supplied, expected value: "shock_year".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B21
        Layout: scalar
        Value type: int

    Examples:
        set_shock_year(ctx, 2)
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='scalar',
            key_fields=(),
            measure_field='OBS_VALUE',
            key_order=None,
            strict=strict,
            measure_dtype='int',
        ),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_SHOCK_YEAR,
        strict=strict,
        fn_name='set_shock_year',
        allow_address=False,
        requires_address=False,
    )

def set_shock_type(
    ctx: EvalContext,
    records: Records | Record | int,
    *,
    strict: bool = True,
) -> None:
    """Set the shock type for the stress scenario in the Tiny-DSA workbook.

    Updates the shock type parameter in the Inputs sheet, specifying which macroeconomic variable the shock applies to (real GDP growth, real interest rate, or primary balance).
    The OBS_VALUE from the record is written to the shock_type cell (Inputs!B22).

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The shock type code indicating the affected parameter: 1 for real GDP growth, 2 for real interest rate, 3 for primary balance.
            Optional record fields:
                - PARAMETER: Identifies the workbook parameter being configured; for this setter it is always the shock type. If supplied, expected value: "shock_type".

    Returns:
        None: Applies the input updates to ctx.

    Source binding:
        Workbook range: Inputs!B22
        Layout: scalar
        Value type: int

    Examples:
        set_shock_type(ctx, 1)
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='scalar',
            key_fields=(),
            measure_field='OBS_VALUE',
            key_order=None,
            strict=strict,
            measure_dtype='int',
        ),
        key_fields=(),
        allowed_fields=frozenset({'OBS_VALUE', 'PARAMETER'}),
        measure_field='OBS_VALUE',
        leaf_index=_LEAF_INDEX_SHOCK_TYPE,
        strict=strict,
        fn_name='set_shock_type',
        allow_address=False,
        requires_address=False,
    )

_KEY_ORDER_SHOCK_MAGNITUDES = ('Growth', 'Interest', 'Primary balance')

def set_shock_magnitudes(
    ctx: EvalContext,
    records: Records | Record | Sequence[float] | DataFrameInput,
    *,
    strict: bool = True,
    empty_measure: EmptyMeasure = "write",
) -> None:
    """Set shock magnitudes for each shock parameter.

    Sets the three shock magnitudes in the shock table (Inputs!B26:D26) that determine the size of the shock applied to a selected parameter.
    Each record provides a shock parameter name and its associated magnitude; the three records map left-to-right across the three cells of the shock table.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - SHOCK_PARAMETER: Name of the shock parameter (e.g., 'Growth', 'Interest', 'Primary Balance').
                - OBS_VALUE: Magnitude of the shock in percentage points.
            Optional record fields:
                - PARAMETER: Constant identifier for the shock magnitudes series. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: Unit of measure for the shock magnitudes. If supplied, expected value: "PP".

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

        set_shock_magnitudes(ctx, [-2.0, 2.0])
    """
    _apply_series_records(
        ctx,
        coerce_setter_input(
            records,
            layout='series',
            key_fields=('SHOCK_PARAMETER',),
            measure_field='OBS_VALUE',
            key_order=_KEY_ORDER_SHOCK_MAGNITUDES,
            strict=strict,
            empty_measure=empty_measure,
            requires_address=False,
            key_dtypes={'SHOCK_PARAMETER': 'string'},
            measure_dtype='float',
        ),
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

_OUTPUT_LEAVES_OUTPUT_BASELINE: list[tuple[str, Record]] = [
    ('Outputs!B12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_baseline(ctx=None, *, inputs=None) -> Records:
    """Compute the baseline debt-to-GDP path for projection years 1 through 5.

    Returns the baseline debt-to-GDP projections from the Outputs sheet.
    Each record matches a cell in Outputs!B12:F12, with TIME_PERIOD from the column header and OBS_VALUE from the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 to 5).
                - OBS_VALUE: Debt-to-GDP ratio as a percentage of GDP.
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

_OUTPUT_LEAVES_OUTPUT_SHOCKED: list[tuple[str, Record]] = [
    ('Outputs!B13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_shocked(ctx=None, *, inputs=None) -> Records:
    """Return the shocked debt-to-GDP ratio path for projection years 1 through 5.

    Return the computed shocked scenario debt-to-GDP ratios as a list of records, one per projection year.
    Each record corresponds to one projection year and its shocked debt-to-GDP value from the Outputs sheet.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 through 5).
                - OBS_VALUE: Shocked debt-to-GDP ratio, expressed as percent of GDP.
            Optional record fields:
                - SCENARIO: Scenario identifier; always the shocked scenario. If supplied, expected value: "shocked".
                - UNIT_MEASURE: Unit of measure for the ratio. If supplied, expected value: "PC_GDP".

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

_OUTPUT_LEAVES_OUTPUT_DELTA: list[tuple[str, Record]] = [
    ('Outputs!B14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!C14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!D14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!E14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!F14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PP'}),
]

def compute_output_delta(ctx=None, *, inputs=None) -> Records:
    """Return the difference between shocked and baseline debt-to-GDP paths (in percentage points) for years 1–5.

    Compute and return the delta (shocked minus baseline) of the debt-to-GDP ratio projection for each projection year.
    Each returned record corresponds to one column (year) in the Outputs!B14:F14 range.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 through 5).
                - OBS_VALUE: Difference between shocked and baseline debt-to-GDP ratios, in percentage points.
            Optional record fields:
                - SCENARIO: Identifies the scenario as the difference between shocked and baseline paths. If supplied, expected value: "shocked_minus_baseline".
                - UNIT_MEASURE: Unit of measure for the observation value. If supplied, expected value: "PP".

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


def list_setters() -> list[str]:
    """Return generated series-binding setter function names."""
    return ['set_country_initial_debt', 'set_country_name', 'set_growth_baseline', 'set_interest_baseline', 'set_primary_balance_baseline', 'set_shock_magnitudes', 'set_shock_type', 'set_shock_year']


def list_readers() -> list[str]:
    """Return generated series-binding reader function names."""
    return ['read_country_initial_debt', 'read_country_name', 'read_growth_baseline', 'read_interest_baseline', 'read_primary_balance_baseline', 'read_shock_magnitudes', 'read_shock_type', 'read_shock_year']


def list_computes() -> list[str]:
    """Return generated series-binding compute function names."""
    return ['compute_output_baseline', 'compute_output_delta', 'compute_output_shocked']


def list_reader_leaves() -> dict[str, dict[str, object]]:
    """Return address → semantic reader call metadata."""
    return {'Inputs!B10': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Borvelia'}, 'kwargs': {'country': 'Borvelia'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Borvelia')"}, 'Inputs!B11': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Litellia'}, 'kwargs': {'country': 'Litellia'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Litellia')"}, 'Inputs!B12': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Aurelium'}, 'kwargs': {'country': 'Aurelium'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Aurelium')"}, 'Inputs!B21': {'series_id': 'shock_year', 'reader': 'read_shock_year', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_shock_year(ctx)'}, 'Inputs!B22': {'series_id': 'shock_type', 'reader': 'read_shock_type', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_shock_type(ctx)'}, 'Inputs!B26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Growth'}, 'kwargs': {'shock_parameter': 'Growth'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Growth')"}, 'Inputs!B5': {'series_id': 'country_name', 'reader': 'read_country_name', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_country_name(ctx)'}, 'Inputs!C16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=1)'}, 'Inputs!C17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=1)'}, 'Inputs!C18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=1)'}, 'Inputs!C26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Interest'}, 'kwargs': {'shock_parameter': 'Interest'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Interest')"}, 'Inputs!D16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=2)'}, 'Inputs!D17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=2)'}, 'Inputs!D18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=2)'}, 'Inputs!D26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Primary balance'}, 'kwargs': {'shock_parameter': 'Primary balance'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Primary balance')"}, 'Inputs!E16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=3)'}, 'Inputs!E17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=3)'}, 'Inputs!E18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=3)'}, 'Inputs!F16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=4)'}, 'Inputs!F17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=4)'}, 'Inputs!F18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=4)'}, 'Inputs!G16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=5)'}, 'Inputs!G17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=5)'}, 'Inputs!G18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=5)'}}


def list_reader_ranges() -> dict[str, dict[str, object]]:
    """Return binding-aligned data_range → range-reader metadata."""
    return {'Inputs!B10:B12': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt_range', 'data_range': 'Inputs!B10:B12', 'call_form': 'read_country_initial_debt_range(ctx)'}, 'Inputs!B26:D26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes_range', 'data_range': 'Inputs!B26:D26', 'call_form': 'read_shock_magnitudes_range(ctx)'}, 'Inputs!C16:G16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline_range', 'data_range': 'Inputs!C16:G16', 'call_form': 'read_growth_baseline_range(ctx)'}, 'Inputs!C17:G17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline_range', 'data_range': 'Inputs!C17:G17', 'call_form': 'read_interest_baseline_range(ctx)'}, 'Inputs!C18:G18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline_range', 'data_range': 'Inputs!C18:G18', 'call_form': 'read_primary_balance_baseline_range(ctx)'}}


TARGETS = {
    'Outputs!B12:F12': xl_range_rows,
    'Outputs!B13:F13': xl_range_rows,
    'Outputs!B14:F14': xl_range_rows,
}


def compute_all(ctx: EvalContext | None = None, *, inputs: dict[str, object] | None = None) -> dict[str, object]:
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
