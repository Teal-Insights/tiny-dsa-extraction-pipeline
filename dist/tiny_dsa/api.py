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
    _LEAF_INDEX_COUNTRY_INITIAL_DEBT,
    _LEAF_INDEX_COUNTRY_NAME,
    _LEAF_INDEX_COUNTRY_PROFILE_NAMES,
    _LEAF_INDEX_ENGINE_YEAR_LABELS,
    _LEAF_INDEX_GROWTH_BASELINE,
    _LEAF_INDEX_INTEREST_BASELINE,
    _LEAF_INDEX_PRIMARY_BALANCE_BASELINE,
    _LEAF_INDEX_SHOCK_MAGNITUDES,
    _LEAF_INDEX_SHOCK_TYPE,
    _LEAF_INDEX_SHOCK_YEAR,
    read_country_initial_debt,
    read_country_initial_debt_range,
    read_country_name,
    read_country_profile_names,
    read_country_profile_names_range,
    read_engine_year_labels,
    read_engine_year_labels_range,
    read_growth_baseline,
    read_growth_baseline_range,
    read_interest_baseline,
    read_interest_baseline_range,
    read_primary_balance_baseline,
    read_primary_balance_baseline_range,
    read_shock_magnitudes,
    read_shock_magnitudes_range,
    read_shock_type,
    read_shock_year,
)
from ._output_leaves import (
    _OUTPUT_LEAVES_OUTPUT_BASELINE,
    _OUTPUT_LEAVES_OUTPUT_SHOCKED,
    _OUTPUT_LEAVES_OUTPUT_DELTA,
)
from .data import CONSTANTS, DEFAULT_INPUTS
from .internals import _resolve_formula
from .runtime import EvalContext, XlErrorException, prepare_context_inputs, xl_cell


def make_context(inputs: dict[str, object] | None = None) -> EvalContext:
    """Create an EvalContext with merged inputs."""
    merged = prepare_context_inputs(DEFAULT_INPUTS, CONSTANTS, inputs)
    return EvalContext(inputs=merged, resolver=_resolve_formula, iterative_enabled=False, iterate_count=100, iterate_delta=0.001)


# --- Series binding setters (Records API) ---

def set_country_name(
    ctx: EvalContext,
    records: Records | Record | str,
    *,
    strict: bool = True,
) -> None:
    """Set the country name input in the Inputs sheet.

    Updates the country_name scalar that determines the initial debt-to-GDP ratio via the country profile lookup.
    The OBS_VALUE from each record is written to the country_name cell (Inputs!B5).

    Args:
        records (Records | Record | str): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The country name to assign to the active scenario. This value is used as the lookup key against the country profile table.
            Optional record fields:
                - PARAMETER: Attribute identifying the series as the country_name parameter. If supplied, expected value: "country_name".

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
    """Set initial debt-to-GDP values in the country profile lookup table.

    Updates the initial debt-to-GDP values for country profile rows on the Inputs sheet.
    Each record is matched to a row in the country profile data range by the COUNTRY label in column A, and OBS_VALUE is written to the corresponding data cell.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - COUNTRY: Country name used as the key to match the record to a row in the country profile table.
                - OBS_VALUE: Initial debt-to-GDP value for the matched country, expressed as a percentage of GDP.
            Optional record fields:
                - INDICATOR: Indicator constant identifying this series as the country initial debt series. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: Unit of measure for the observation values in this series. If supplied, expected value: "PC_GDP".

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

        set_country_initial_debt(ctx, [40.0, 60.0, 80.0])
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

    Updates the growth_baseline series in the Inputs sheet with real GDP growth values for each projection year.
    Each record is matched to a cell in the growth_baseline range by its TIME_PERIOD, and the OBS_VALUE is written to that year's column.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year identifying the column in the growth_baseline range to update.
                - OBS_VALUE: Real GDP growth rate for the corresponding projection year.
            Optional record fields:
                - INDICATOR: Indicator identifying the observation as belonging to the baseline real GDP growth series. If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: Unit of measure for the observation values. If supplied, expected value: "PERCENT_PER_ANNUM".

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

        set_growth_baseline(ctx, [3.5, 3.5, 3.5, 3.5, 3.5])
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
    """Set baseline real interest rates for projection years 1 through 5.

    Updates the five baseline real interest rate cells in Inputs!C17:G17 from records.
    Each record's TIME_PERIOD selects a column in the interest_baseline range and OBS_VALUE is written as the real interest rate.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year identifying the column in the baseline interest rate range.
                - OBS_VALUE: Real interest rate for the projection year, in percent per annum.
            Optional record fields:
                - INDICATOR: Identifies the series as the baseline real interest rate. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: Unit of measure for the observation values. If supplied, expected value: "PERCENT_PER_ANNUM".

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

        set_interest_baseline(ctx, [4.0, 4.0, 4.0, 4.0, 4.0])
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
    """Set the baseline primary balance series for projection years 1 through 5.

    Writes the supplied primary balance records to the baseline primary balance series in the Inputs sheet.
    Each record's TIME_PERIOD selects the corresponding projection-year cell in the series range, and OBS_VALUE is written to that cell.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year identifying the column in the baseline primary balance series.
                - OBS_VALUE: Primary balance as a percent of GDP; positive values denote a surplus.
            Optional record fields:
                - INDICATOR: Fixed indicator identifier for the baseline primary balance series. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: Unit of measure attached to each observation in the series. If supplied, expected value: "PC_GDP".

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

        set_primary_balance_baseline(ctx, [-1.0, -0.5, 0.0, 0.5, 1.0])
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
    """Set the shock year used to configure the shock scenario in the Tiny-DSA tool.

    Updates the single shock_year input cell on the Inputs sheet with the provided observation value.
    Each observation value maps directly to the scalar shock_year cell (Inputs!B21); when records are supplied, the OBS_VALUE field provides the scalar to write.

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The first projection year in which the selected shock takes effect.
            Optional record fields:
                - PARAMETER: Optional label identifying the parameter as shock_year; it must match the series context if provided. If supplied, expected value: "shock_year".

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
    """Set the shock type code in the SHOCK CONFIGURATION section of the Inputs sheet.

    Updates the shock_type cell (Inputs!B22) with the integer code that selects which parameter the configured shock affects.
    A scalar value or sequence of values is normalized to records; each record's OBS_VALUE is written to the scalar cell Inputs!B22, with optional PARAMETER used for key matching.

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: Integer observation value that identifies the parameter affected by the shock.
            Optional record fields:
                - PARAMETER: Attribute that names the parameter series and associates the record with the shock_type API. If supplied, expected value: "shock_type".

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
    """Set shock magnitudes for the configured shock types in the Inputs sheet.

    Updates the shock magnitude values in the shock table section of the Inputs sheet.
    Each record supplies a shock magnitude for one shock type, keyed by SHOCK_PARAMETER, and the value is written to the corresponding column of the shock table range.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - SHOCK_PARAMETER: Key identifying which shock type the record supplies a magnitude for; matches the column header of the shock table.
                - OBS_VALUE: Shock magnitude to write for the identified shock parameter, expressed in percentage points. Positive values denote an increase and negative values a decrease in the affected parameter.
            Optional record fields:
                - PARAMETER: Series context key identifying this input as a shock magnitude; fixed for the series and not included in individual records. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: Unit-of-measure attribute fixed for the series; indicates the unit in which observation values are expressed. If supplied, expected value: "PP".

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

        set_shock_magnitudes(ctx, [-2.0, 2.0, -1.0])
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

def compute_output_baseline(ctx=None, *, inputs=None) -> Records:
    """Compute the baseline debt-to-GDP output series for projection years 1 through 5.

    Computes and returns the baseline debt-to-GDP path for the output series.
    Each record maps to one year's value in the Outputs!B12:F12 baseline row, keyed by TIME_PERIOD.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year identifying the column in the baseline output row.
                - OBS_VALUE: Projected general-government debt-to-GDP ratio for the baseline scenario in the given projection year.
            Optional record fields:
                - SCENARIO: Scenario context for the output series. If supplied, expected value: "baseline".
                - UNIT_MEASURE: Unit in which the observation values are expressed. If supplied, expected value: "PC_GDP".

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
        try:
            record[measure_field] = xl_cell(ctx, address)
        except XlErrorException as err:
            record[measure_field] = err.code
        if include_address:
            record["address"] = address
        records.append(record)
    return records

def compute_output_shocked(ctx=None, *, inputs=None) -> Records:
    """Compute the shocked debt-to-GDP path for the configured shock scenario.

    Returns the shocked debt-to-GDP ratio for each projection year in the output series.
    Each record corresponds to one projection year, with the shocked debt-to-GDP ratio read from the corresponding cell in the output_shocked range (Outputs!B13:F13).

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 through 5) to which the shocked debt-to-GDP ratio applies.
                - OBS_VALUE: Shocked debt-to-GDP ratio for the projection year, expressed as a percentage of GDP.
            Optional record fields:
                - SCENARIO: Scenario label for the output series; identifies the series as the shocked path. If supplied, expected value: "shocked".
                - UNIT_MEASURE: Unit of measure for the observation value. If supplied, expected value: "PC_GDP".

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
        try:
            record[measure_field] = xl_cell(ctx, address)
        except XlErrorException as err:
            record[measure_field] = err.code
        if include_address:
            record["address"] = address
        records.append(record)
    return records

def compute_output_delta(ctx=None, *, inputs=None) -> Records:
    """Compute the output_delta series as the difference between the shocked and baseline debt-to-GDP paths.

    Returns the shocked-minus-baseline debt-to-GDP trajectory in percentage points of GDP for the projection horizon.
    Each record corresponds to one projection year: TIME_PERIOD is read from the column header on the Outputs sheet and OBS_VALUE is read from the data cell in that column of the output_delta range.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year for the debt-to-GDP difference.
                - OBS_VALUE: Computed difference between the shocked and baseline debt-to-GDP ratios for the projection year, expressed in percentage points.
            Optional record fields:
                - SCENARIO: Scenario identifier indicating that the observation is the shocked path minus the baseline path. If supplied, expected value: "shocked_minus_baseline".
                - UNIT_MEASURE: Unit in which the observation value is expressed. If supplied, expected value: "PP".

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
        try:
            record[measure_field] = xl_cell(ctx, address)
        except XlErrorException as err:
            record[measure_field] = err.code
        if include_address:
            record["address"] = address
        records.append(record)
    return records


def list_setters() -> list[str]:
    """Return generated series-binding setter function names."""
    return ['set_country_initial_debt', 'set_country_name', 'set_growth_baseline', 'set_interest_baseline', 'set_primary_balance_baseline', 'set_shock_magnitudes', 'set_shock_type', 'set_shock_year']


def list_readers() -> list[str]:
    """Return generated series-binding reader function names."""
    return ['read_country_initial_debt', 'read_country_name', 'read_country_profile_names', 'read_engine_year_labels', 'read_growth_baseline', 'read_interest_baseline', 'read_primary_balance_baseline', 'read_shock_magnitudes', 'read_shock_type', 'read_shock_year']


def list_computes() -> list[str]:
    """Return generated series-binding compute function names."""
    return ['compute_output_baseline', 'compute_output_delta', 'compute_output_shocked']


def list_reader_leaves() -> dict[str, dict[str, object]]:
    """Return address → semantic reader call metadata."""
    return {'Engine!C5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_engine_year_labels(ctx, time_period=1)'}, 'Engine!D5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_engine_year_labels(ctx, time_period=2)'}, 'Engine!E5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_engine_year_labels(ctx, time_period=3)'}, 'Engine!F5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_engine_year_labels(ctx, time_period=4)'}, 'Engine!G5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_engine_year_labels(ctx, time_period=5)'}, 'Inputs!A10': {'series_id': 'country_profile_names', 'reader': 'read_country_profile_names', 'keys': {'COUNTRY': 'Borvelia'}, 'kwargs': {'country': 'Borvelia'}, 'kind': 'keyed', 'call_form': "read_country_profile_names(ctx, country='Borvelia')"}, 'Inputs!A11': {'series_id': 'country_profile_names', 'reader': 'read_country_profile_names', 'keys': {'COUNTRY': 'Litellia'}, 'kwargs': {'country': 'Litellia'}, 'kind': 'keyed', 'call_form': "read_country_profile_names(ctx, country='Litellia')"}, 'Inputs!A12': {'series_id': 'country_profile_names', 'reader': 'read_country_profile_names', 'keys': {'COUNTRY': 'Aurelium'}, 'kwargs': {'country': 'Aurelium'}, 'kind': 'keyed', 'call_form': "read_country_profile_names(ctx, country='Aurelium')"}, 'Inputs!B10': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Borvelia'}, 'kwargs': {'country': 'Borvelia'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Borvelia')"}, 'Inputs!B11': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Litellia'}, 'kwargs': {'country': 'Litellia'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Litellia')"}, 'Inputs!B12': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt', 'keys': {'COUNTRY': 'Aurelium'}, 'kwargs': {'country': 'Aurelium'}, 'kind': 'keyed', 'call_form': "read_country_initial_debt(ctx, country='Aurelium')"}, 'Inputs!B21': {'series_id': 'shock_year', 'reader': 'read_shock_year', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_shock_year(ctx)'}, 'Inputs!B22': {'series_id': 'shock_type', 'reader': 'read_shock_type', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_shock_type(ctx)'}, 'Inputs!B26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Growth'}, 'kwargs': {'shock_parameter': 'Growth'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Growth')"}, 'Inputs!B5': {'series_id': 'country_name', 'reader': 'read_country_name', 'keys': {}, 'kwargs': {}, 'kind': 'scalar', 'call_form': 'read_country_name(ctx)'}, 'Inputs!C16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=1)'}, 'Inputs!C17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=1)'}, 'Inputs!C18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 1}, 'kwargs': {'time_period': 1}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=1)'}, 'Inputs!C26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Interest'}, 'kwargs': {'shock_parameter': 'Interest'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Interest')"}, 'Inputs!D16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=2)'}, 'Inputs!D17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=2)'}, 'Inputs!D18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 2}, 'kwargs': {'time_period': 2}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=2)'}, 'Inputs!D26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes', 'keys': {'SHOCK_PARAMETER': 'Primary balance'}, 'kwargs': {'shock_parameter': 'Primary balance'}, 'kind': 'keyed', 'call_form': "read_shock_magnitudes(ctx, shock_parameter='Primary balance')"}, 'Inputs!E16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=3)'}, 'Inputs!E17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=3)'}, 'Inputs!E18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 3}, 'kwargs': {'time_period': 3}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=3)'}, 'Inputs!F16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=4)'}, 'Inputs!F17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=4)'}, 'Inputs!F18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 4}, 'kwargs': {'time_period': 4}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=4)'}, 'Inputs!G16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_growth_baseline(ctx, time_period=5)'}, 'Inputs!G17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_interest_baseline(ctx, time_period=5)'}, 'Inputs!G18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline', 'keys': {'TIME_PERIOD': 5}, 'kwargs': {'time_period': 5}, 'kind': 'keyed', 'call_form': 'read_primary_balance_baseline(ctx, time_period=5)'}}


def list_reader_ranges() -> dict[str, dict[str, object]]:
    """Return binding-aligned data_range → range-reader metadata."""
    return {'Engine!C5:G5': {'series_id': 'engine_year_labels', 'reader': 'read_engine_year_labels_range', 'data_range': 'Engine!C5:G5', 'call_form': 'read_engine_year_labels_range(ctx)'}, 'Inputs!A10:A12': {'series_id': 'country_profile_names', 'reader': 'read_country_profile_names_range', 'data_range': 'Inputs!A10:A12', 'call_form': 'read_country_profile_names_range(ctx)'}, 'Inputs!B10:B12': {'series_id': 'country_initial_debt', 'reader': 'read_country_initial_debt_range', 'data_range': 'Inputs!B10:B12', 'call_form': 'read_country_initial_debt_range(ctx)'}, 'Inputs!B26:D26': {'series_id': 'shock_magnitudes', 'reader': 'read_shock_magnitudes_range', 'data_range': 'Inputs!B26:D26', 'call_form': 'read_shock_magnitudes_range(ctx)'}, 'Inputs!C16:G16': {'series_id': 'growth_baseline', 'reader': 'read_growth_baseline_range', 'data_range': 'Inputs!C16:G16', 'call_form': 'read_growth_baseline_range(ctx)'}, 'Inputs!C17:G17': {'series_id': 'interest_baseline', 'reader': 'read_interest_baseline_range', 'data_range': 'Inputs!C17:G17', 'call_form': 'read_interest_baseline_range(ctx)'}, 'Inputs!C18:G18': {'series_id': 'primary_balance_baseline', 'reader': 'read_primary_balance_baseline_range', 'data_range': 'Inputs!C18:G18', 'call_form': 'read_primary_balance_baseline_range(ctx)'}}
__all__ = ['make_context', 'list_setters', 'list_readers', 'list_computes', 'list_reader_leaves', 'list_reader_ranges', 'set_country_initial_debt', 'set_country_name', 'set_growth_baseline', 'set_interest_baseline', 'set_primary_balance_baseline', 'set_shock_magnitudes', 'set_shock_type', 'set_shock_year', 'read_country_initial_debt', 'read_country_name', 'read_country_profile_names', 'read_engine_year_labels', 'read_growth_baseline', 'read_interest_baseline', 'read_primary_balance_baseline', 'read_shock_magnitudes', 'read_shock_type', 'read_shock_year', 'read_country_initial_debt_range', 'read_growth_baseline_range', 'read_interest_baseline_range', 'read_primary_balance_baseline_range', 'read_shock_magnitudes_range', 'read_country_profile_names_range', 'read_engine_year_labels_range', 'compute_output_baseline', 'compute_output_delta', 'compute_output_shocked']
