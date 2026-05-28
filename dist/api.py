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

Record = dict[str, object]
Records = list[Record]

_LEAF_INDEX_COUNTRY_NAME = {
    (('PARAMETER', 'country_name'),): 'Inputs!B5',
}

def set_country_name(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Selected country name from the country profile table."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'PARAMETER', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_country_name (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_COUNTRY_NAME.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

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
    """Baseline real GDP growth rates for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'INDICATOR', 'TIME_PERIOD', 'UNIT_MEASURE', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_growth_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_GROWTH_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

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
    """Baseline real interest rates for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'INDICATOR', 'TIME_PERIOD', 'UNIT_MEASURE', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_interest_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_INTEREST_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

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
    """Baseline primary balance path for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'INDICATOR', 'TIME_PERIOD', 'UNIT_MEASURE', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_primary_balance_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_PRIMARY_BALANCE_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_SHOCK_YEAR = {
    (('PARAMETER', 'shock_year'),): 'Inputs!B21',
}

def set_shock_year(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """First projection year in which the selected shock applies."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'PARAMETER', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_shock_year (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_YEAR.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_SHOCK_TYPE = {
    (('PARAMETER', 'shock_type'),): 'Inputs!B22',
}

def set_shock_type(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Shock type code: 1 for growth, 2 for interest, 3 for primary balance."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'PARAMETER', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_shock_type (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_TYPE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

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
    """Shock magnitudes keyed by affected parameter."""
    key_fields = ('SHOCK_PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'PARAMETER', 'SHOCK_PARAMETER', 'UNIT_MEASURE', 'OBS_VALUE'}
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
            raise ValueError(f"record[{index}]: address required for set_shock_magnitudes (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_MAGNITUDES.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

# --- Series binding output compute (Records API) ---

_OUTPUT_LEAVES_OUTPUT_BASELINE = [
    ('Outputs!B12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_baseline(inputs=None, *, ctx=None) -> Records:
    """Baseline debt-to-GDP path for projection years 1 through 5."""
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

def compute_output_shocked(inputs=None, *, ctx=None) -> Records:
    """Shocked debt-to-GDP path for projection years 1 through 5."""
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

def compute_output_delta(inputs=None, *, ctx=None) -> Records:
    """Difference between the shocked and baseline paths in percentage points."""
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


def compute_all(inputs=None, *, ctx=None):
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
