"""Named calculation functions for every bound formula series."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from . import data
from .tensor import Domain, Series
from .excel import XlError, as_measure, xl_add, xl_bool, xl_choose_lazy, xl_div, xl_ge, xl_index, xl_match, xl_mul, xl_sub
from .runtime import CoordinateReader, axis_step, evaluate, publish, view

@publish(key=(), domain=None, cells=data.INITIAL_DEBT_RESOLVED_CELLS)
def initial_debt_resolved(*, country_profile_names: data.Series[str | None], country_name: str, country_initial_debt: data.CountryInitialDebt) -> float | str:
    """Compute `initial_debt_resolved` using authored coordinate identities."""
    data.COUNTRY_PROFILE_NAMES.schema.validate(country_profile_names)
    data.COUNTRY_INITIAL_DEBT.schema.validate(country_initial_debt)
    _initial_debt_resolved_table_0 = view(country_initial_debt, rows=data.COUNTRY_AXIS.keys)
    _initial_debt_resolved_table_1 = view(country_profile_names, rows=data.COUNTRY_AXIS.keys)
    try:
        return as_measure(xl_index(_initial_debt_resolved_table_0, xl_match(country_name, _initial_debt_resolved_table_1, 0), 1))
    except XlError as error:
        return error.code

@publish(key=(), domain=None, cells=data.ENGINE_INITIAL_DEBT_BASELINE_CELLS)
def engine_initial_debt_baseline(*, initial_debt_resolved: float | str) -> float | str:
    """Compute `engine_initial_debt_baseline` using authored coordinate identities."""
    try:
        return as_measure(initial_debt_resolved)
    except XlError as error:
        return error.code

@publish(key=(), domain=None, cells=data.ENGINE_INITIAL_DEBT_SHOCKED_CELLS)
def engine_initial_debt_shocked(*, initial_debt_resolved: float | str) -> float | str:
    """Compute `engine_initial_debt_shocked` using authored coordinate identities."""
    try:
        return as_measure(initial_debt_resolved)
    except XlError as error:
        return error.code

@publish(key=(), domain=None, cells=data.SHOCK_MAGNITUDE_RESOLVED_CELLS)
def shock_magnitude_resolved(*, shock_type: int | str, shock_magnitudes: data.ShockMagnitudes) -> float | str:
    """Compute `shock_magnitude_resolved` using authored coordinate identities."""
    data.SHOCK_MAGNITUDES.schema.validate(shock_magnitudes)
    try:
        return as_measure(shock_magnitudes[axis_step(data.SHOCK_PARAMETER_AXIS, 'Growth', xl_sub(shock_type, 1))])
    except XlError as error:
        return error.code

@publish(data.SHOCK_ACTIVE.schema, cells=data.SHOCK_ACTIVE.cells)
def shock_active(*, engine_year_labels: data.Series[int | str | None], shock_year: int | str) -> data.Series[int | str | None]:
    """Compute `shock_active` using authored coordinate identities."""
    data.ENGINE_YEAR_LABELS.schema.validate(engine_year_labels)
    def formula(time_period: int) -> int | str | None:
        return as_measure((1 if xl_bool(xl_ge(engine_year_labels[time_period], shock_year)) else 0), 'int')

    return data.SHOCK_ACTIVE.collect(evaluate(formula, data.SHOCK_ACTIVE.required))

@publish(data.SHOCKED_GROWTH.schema, cells=data.SHOCKED_GROWTH.cells)
def shocked_growth(*, growth_baseline: data.GrowthBaseline, shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: data.Series[int | str | None]) -> data.Series[float | str | None]:
    """Compute `shocked_growth` using authored coordinate identities."""
    data.GROWTH_BASELINE.schema.validate(growth_baseline)
    def formula(time_period: int) -> float | str | None:
        return as_measure(xl_add(growth_baseline[time_period], xl_mul(xl_choose_lazy(shock_type, lambda: shock_magnitude_resolved, lambda: 0, lambda: 0), shock_active[time_period])))

    return data.SHOCKED_GROWTH.collect(evaluate(formula, data.SHOCKED_GROWTH.required))

@publish(data.SHOCKED_INTEREST.schema, cells=data.SHOCKED_INTEREST.cells)
def shocked_interest(*, interest_baseline: data.InterestBaseline, shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: data.Series[int | str | None]) -> data.Series[float | str | None]:
    """Compute `shocked_interest` using authored coordinate identities."""
    data.INTEREST_BASELINE.schema.validate(interest_baseline)
    def formula(time_period: int) -> float | str | None:
        return as_measure(xl_add(interest_baseline[time_period], xl_mul(xl_choose_lazy(shock_type, lambda: 0, lambda: shock_magnitude_resolved, lambda: 0), shock_active[time_period])))

    return data.SHOCKED_INTEREST.collect(evaluate(formula, data.SHOCKED_INTEREST.required))

@publish(data.SHOCKED_PRIMARY_BALANCE.schema, cells=data.SHOCKED_PRIMARY_BALANCE.cells)
def shocked_primary_balance(*, primary_balance_baseline: data.PrimaryBalanceBaseline, shock_type: int | str, shock_magnitude_resolved: float | str, shock_active: data.Series[int | str | None]) -> data.Series[float | str | None]:
    """Compute `shocked_primary_balance` using authored coordinate identities."""
    data.PRIMARY_BALANCE_BASELINE.schema.validate(primary_balance_baseline)
    def formula(time_period: int) -> float | str | None:
        return as_measure(xl_add(primary_balance_baseline[time_period], xl_mul(xl_choose_lazy(shock_type, lambda: 0, lambda: 0, lambda: shock_magnitude_resolved), shock_active[time_period])))

    return data.SHOCKED_PRIMARY_BALANCE.collect(evaluate(formula, data.SHOCKED_PRIMARY_BALANCE.required))

@publish(data.BASELINE_PATH_INTERNAL.schema, cells=data.BASELINE_PATH_INTERNAL.cells)
def baseline_path_internal(*, engine_initial_debt_baseline: float | str, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline) -> data.Series[float | str | None]:
    """Compute `baseline_path_internal` using authored coordinate identities."""
    data.GROWTH_BASELINE.schema.validate(growth_baseline)
    data.INTEREST_BASELINE.schema.validate(interest_baseline)
    data.PRIMARY_BALANCE_BASELINE.schema.validate(primary_balance_baseline)
    def formula(time_period: int) -> float | str | None:
        if time_period == 1:
            return as_measure(xl_sub(xl_div(xl_mul(engine_initial_debt_baseline, xl_add(1, xl_div(interest_baseline[time_period], 100))), xl_add(1, xl_div(growth_baseline[time_period], 100))), primary_balance_baseline[time_period]))
        return as_measure(xl_sub(xl_div(xl_mul(baseline_path_internal[time_period - 1], xl_add(1, xl_div(interest_baseline[time_period], 100))), xl_add(1, xl_div(growth_baseline[time_period], 100))), primary_balance_baseline[time_period]))

    baseline_path_internal = CoordinateReader('baseline_path_internal', data.BASELINE_PATH_INTERNAL.required, formula)
    return data.BASELINE_PATH_INTERNAL.collect((coord, baseline_path_internal[coord]) for coord in data.BASELINE_PATH_INTERNAL.required)

@publish(data.SHOCKED_PATH_INTERNAL.schema, cells=data.SHOCKED_PATH_INTERNAL.cells)
def shocked_path_internal(*, engine_initial_debt_shocked: float | str, shocked_growth: data.Series[float | str | None], shocked_interest: data.Series[float | str | None], shocked_primary_balance: data.Series[float | str | None]) -> data.Series[float | str | None]:
    """Compute `shocked_path_internal` using authored coordinate identities."""
    def formula(time_period: int) -> float | str | None:
        if time_period == 1:
            return as_measure(xl_sub(xl_div(xl_mul(engine_initial_debt_shocked, xl_add(1, xl_div(shocked_interest[time_period], 100))), xl_add(1, xl_div(shocked_growth[time_period], 100))), shocked_primary_balance[time_period]))
        return as_measure(xl_sub(xl_div(xl_mul(shocked_path_internal[time_period - 1], xl_add(1, xl_div(shocked_interest[time_period], 100))), xl_add(1, xl_div(shocked_growth[time_period], 100))), shocked_primary_balance[time_period]))

    shocked_path_internal = CoordinateReader('shocked_path_internal', data.SHOCKED_PATH_INTERNAL.required, formula)
    return data.SHOCKED_PATH_INTERNAL.collect((coord, shocked_path_internal[coord]) for coord in data.SHOCKED_PATH_INTERNAL.required)

@publish(data.OUTPUT_BASELINE.schema, cells=data.OUTPUT_BASELINE.cells)
def output_baseline(*, baseline_path_internal: data.Series[float | str | None]) -> data.OutputBaseline:
    """Compute `output_baseline` using authored coordinate identities."""
    def formula(time_period: int) -> float | str | None:
        return as_measure(baseline_path_internal[time_period])

    return data.OUTPUT_BASELINE.collect(evaluate(formula, data.OUTPUT_BASELINE.required))

@publish(data.OUTPUT_SHOCKED.schema, cells=data.OUTPUT_SHOCKED.cells)
def output_shocked(*, shocked_path_internal: data.Series[float | str | None]) -> data.OutputShocked:
    """Compute `output_shocked` using authored coordinate identities."""
    def formula(time_period: int) -> float | str | None:
        return as_measure(shocked_path_internal[time_period])

    return data.OUTPUT_SHOCKED.collect(evaluate(formula, data.OUTPUT_SHOCKED.required))

@publish(data.OUTPUT_DELTA.schema, cells=data.OUTPUT_DELTA.cells)
def output_delta(*, output_baseline: data.OutputBaseline, output_shocked: data.OutputShocked) -> data.OutputDelta:
    """Compute `output_delta` using authored coordinate identities."""
    def formula(time_period: int) -> float | str | None:
        return as_measure(xl_sub(output_shocked[time_period], output_baseline[time_period]))

    return data.OUTPUT_DELTA.collect(evaluate(formula, data.OUTPUT_DELTA.required))
