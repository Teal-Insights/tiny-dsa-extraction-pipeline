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
from ._output_leaves import (
    _OUTPUT_LEAVES_OUTPUT_BASELINE,
    _OUTPUT_LEAVES_OUTPUT_SHOCKED,
    _OUTPUT_LEAVES_OUTPUT_DELTA,
)
from .data import CONSTANTS, DEFAULT_INPUTS
from .internals import _resolve_formula
from .runtime import (
    EvalContext,
    XlErrorException,
    coerce_inputs_dict,
    xl_cell,
    xl_range_rows,
)


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
    """Set the country name for the Tiny-DSA workbook.

    Sets the user-selected country name in the Inputs sheet, which drives the initial debt-to-GDP lookup.
    Each record's OBS_VALUE field maps directly to the Inputs!B5 cell.

    Args:
        records (Records | Record | str): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The selected country name, as listed in the country profile table.
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
    """Set the initial debt-to-GDP ratio for each country in the profile table.

    Assigns initial debt-to-GDP ratios to countries in the profile lookup table.
    Each record corresponds to a row in the profile table; the COUNTRY field identifies the row by its label in column A, and the OBS_VALUE is written to the data cell in column B.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - COUNTRY: The country name used as the row label in the profile table.
                - OBS_VALUE: The initial debt-to-GDP ratio value for the country.
            Optional record fields:
                - INDICATOR: Constant field that identifies the series indicator concept. If supplied, expected value: "initial_debt_to_gdp".
                - UNIT_MEASURE: Constant field that specifies the unit of measure for the series. If supplied, expected value: "PC_GDP".

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
    """Set the baseline real GDP growth rates for projection years 1 through 5.

    Updates the annual baseline real GDP growth assumption used in debt projections.
    Each record is matched to a cell in the workbook range Inputs!C16:G16 by its TIME_PERIOD value.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: The projection year, ranging from 1 to 5.
                - OBS_VALUE: The baseline real GDP growth rate, expressed in percent per annum.
            Optional record fields:
                - INDICATOR: Indicator identifying this series as real GDP growth. If supplied, expected value: "real_gdp_growth".
                - UNIT_MEASURE: Unit of measure for the growth rates. If supplied, expected value: "PERCENT_PER_ANNUM".

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
    """Set the baseline real interest rates for projection years 1–5.

    Updates the baseline real interest rate path for the debt sustainability analysis projection.
    Records are matched to cells in the Inputs!C17:G17 range by the TIME_PERIOD dimension.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year, from 1 to 5.
                - OBS_VALUE: Effective real interest rate on general-government debt for the corresponding year.
            Optional record fields:
                - INDICATOR: Indicates that this series contains real interest rate data. If supplied, expected value: "real_interest_rate".
                - UNIT_MEASURE: Unit of measure for the interest rate. If supplied, expected value: "PERCENT_PER_ANNUM".

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
    """Set the baseline primary balance path for projection years 1 through 5.

    Updates the baseline primary balance values for each year in the projection horizon.
    Each record maps to a cell in Inputs!C18:G18, with TIME_PERIOD identifying the year (1-5) and OBS_VALUE providing the primary balance.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - TIME_PERIOD: Projection year within the five-year horizon.
                - OBS_VALUE: Primary balance as percent of GDP (positive denotes surplus).
            Optional record fields:
                - INDICATOR: Identifies the series as primary balance. If supplied, expected value: "primary_balance".
                - UNIT_MEASURE: Unit of measure, percent of GDP. If supplied, expected value: "PC_GDP".

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
    """Set the shock year for the Tiny-DSA scenario.

    Specifies the first projection year when the shock applies.
    Writes the provided year to cell `Inputs!B21`.

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: The projection year when the shock takes effect.
            Optional record fields:
                - PARAMETER: Constant attribute identifying the configuration as the shock year. If supplied, expected value: "shock_year".

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
    """Set the shock type for the debt sustainability scenario.

    Updates the shock type code on the Inputs sheet.
    Each record maps to the cell Inputs!B22, with OBS_VALUE providing the integer code.

    Args:
        records (Records | Record | int): A bare scalar value, a single record dict, or a list of records.
            Required record fields:
                - OBS_VALUE: Integer code specifying which parameter the shock applies to: 1 for real GDP growth, 2 for real interest rate, 3 for primary balance.
            Optional record fields:
                - PARAMETER: Identifies the parameter being configured (shock type). If supplied, expected value: "shock_type".

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
    """Set the shock magnitudes for the debt sustainability scenario.

    Updates the three shock magnitude cells that define the size of alternative shocks to real GDP growth, the real interest rate, and the primary balance.
    Each record matches a column in the shock table by the shock parameter name; its observation value is written to the corresponding cell.

    Args:
        records (Records | Record | Sequence[float] | DataFrameInput): A list of records, a single record dict, a tidy pandas/polars DataFrame, or a 1-D iterable of measure values in key order.
        empty_measure (EmptyMeasure): How to treat rows with missing measure values (`None` or float NaN after DataFrame coercion). "write" (default) passes values through; "skip" drops them; "error" raises. Empty key fields always raise.
            Required record fields:
                - SHOCK_PARAMETER: Name of the shock parameter (e.g., Growth, Interest, Primary balance).
                - OBS_VALUE: Shock magnitude, expressed in percentage points.
            Optional record fields:
                - PARAMETER: Identifies the parameter series being set. If supplied, expected value: "shock_magnitude".
                - UNIT_MEASURE: Unit of measure for the shock magnitude. If supplied, expected value: "PP".

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
    """Return the baseline debt-to-GDP trajectory for projection years 1 to 5.

    Returns the baseline debt-to-GDP path as a list of records, each containing a projection year and the corresponding debt-to-GDP ratio.
    Each record corresponds to a cell in the Outputs sheet range B12:F12, with TIME_PERIOD taken from the column header and OBS_VALUE from the cell value.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 to 5).
                - OBS_VALUE: Baseline debt-to-GDP ratio, expressed as a percentage of GDP.
            Optional record fields:
                - SCENARIO: Scenario identifier for the observation. If supplied, expected value: "baseline".
                - UNIT_MEASURE: Unit of measure for the observation value. If supplied, expected value: "PC_GDP".

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
    """Compute the shocked debt-to-GDP path for projection years 1 to 5.

    Returns the shocked debt-to-GDP trajectory as records loaded from the Outputs sheet.
    Each record corresponds to a cell in Outputs!B13:F13, with TIME_PERIOD from column headers and OBS_VALUE from cell values.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year (1 to 5).
                - OBS_VALUE: Debt-to-GDP ratio (shocked scenario) in percent of GDP.
            Optional record fields:
                - SCENARIO: Scenario identifier, constant for this series. If supplied, expected value: "shocked".
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
    """Compute the difference between shocked and baseline debt-to-GDP ratios (percentage points).

    Returns records representing the annual difference (shocked minus baseline) in debt-to-GDP ratio over the projection horizon.
    Each record corresponds to a cell in the Outputs!B14:F14 range, mapping column index to TIME_PERIOD and cell value to OBS_VALUE.

    Args:
        ctx (EvalContext | None): Existing evaluation context, if available.
        inputs (dict[str, object] | None): Optional input map when ctx is omitted.

    Returns:
        Records: Computed output records.
            Required record fields:
                - TIME_PERIOD: Projection year identifier, from 1 to 5.
                - OBS_VALUE: Difference between shocked and baseline debt-to-GDP ratio, in percentage points.
            Optional record fields:
                - SCENARIO: Scenario indicator for the difference path. If supplied, expected value: "shocked_minus_baseline".
                - UNIT_MEASURE: Unit of measure for the difference values. If supplied, expected value: "PP".

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
