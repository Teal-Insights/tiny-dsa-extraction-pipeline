"""Generated functions accepting and returning named-coordinate values."""
from __future__ import annotations
from datetime import datetime
from functools import cached_property
from . import data, internals, validation
from .data import _CONSTANTS_0, _CONSTANTS_1
from .runtime import publish


class Model:
    """Formula series of the workbook, evaluated on demand from bound inputs.

    Each attribute evaluates its named formula once per model. Only the
    inputs bound at construction are available, so a public function
    supplies exactly the leaves of its output.
    """

    country_name: str
    country_initial_debt: data.CountryInitialDebt
    growth_baseline: data.GrowthBaseline
    interest_baseline: data.InterestBaseline
    primary_balance_baseline: data.PrimaryBalanceBaseline
    shock_year: int | str
    shock_type: int | str
    shock_magnitudes: data.ShockMagnitudes

    def __init__(self, **inputs: object) -> None:
        for name, value in inputs.items():
            check = validation.CHECKS.get(name)
            setattr(self, name, value if check is None else check(value))

    @cached_property
    def initial_debt_resolved(self) -> float | str:
        return internals.initial_debt_resolved(country_profile_names=data.COUNTRY_PROFILE_NAMES, country_name=self.country_name, country_initial_debt=self.country_initial_debt)

    @cached_property
    def engine_initial_debt_baseline(self) -> float | str:
        return internals.engine_initial_debt_baseline(initial_debt_resolved=self.initial_debt_resolved)

    @cached_property
    def engine_initial_debt_shocked(self) -> float | str:
        return internals.engine_initial_debt_shocked(initial_debt_resolved=self.initial_debt_resolved)

    @cached_property
    def shock_magnitude_resolved(self) -> float | str:
        return internals.shock_magnitude_resolved(shock_type=self.shock_type, shock_magnitudes=self.shock_magnitudes)

    @cached_property
    def shock_active(self) -> data.Series[int | str | None]:
        return internals.shock_active(engine_year_labels=data.ENGINE_YEAR_LABELS, shock_year=self.shock_year)

    @cached_property
    def shocked_growth(self) -> data.Series[float | str | None]:
        return internals.shocked_growth(growth_baseline=self.growth_baseline, shock_type=self.shock_type, shock_magnitude_resolved=self.shock_magnitude_resolved, shock_active=self.shock_active)

    @cached_property
    def shocked_interest(self) -> data.Series[float | str | None]:
        return internals.shocked_interest(interest_baseline=self.interest_baseline, shock_type=self.shock_type, shock_magnitude_resolved=self.shock_magnitude_resolved, shock_active=self.shock_active)

    @cached_property
    def shocked_primary_balance(self) -> data.Series[float | str | None]:
        return internals.shocked_primary_balance(primary_balance_baseline=self.primary_balance_baseline, shock_type=self.shock_type, shock_magnitude_resolved=self.shock_magnitude_resolved, shock_active=self.shock_active)

    @cached_property
    def baseline_path_internal(self) -> data.Series[float | str | None]:
        return internals.baseline_path_internal(engine_initial_debt_baseline=self.engine_initial_debt_baseline, growth_baseline=self.growth_baseline, interest_baseline=self.interest_baseline, primary_balance_baseline=self.primary_balance_baseline)

    @cached_property
    def shocked_path_internal(self) -> data.Series[float | str | None]:
        return internals.shocked_path_internal(engine_initial_debt_shocked=self.engine_initial_debt_shocked, shocked_growth=self.shocked_growth, shocked_interest=self.shocked_interest, shocked_primary_balance=self.shocked_primary_balance)

    @cached_property
    def output_baseline(self) -> data.OutputBaseline:
        return internals.output_baseline(baseline_path_internal=self.baseline_path_internal)

    @cached_property
    def output_shocked(self) -> data.OutputShocked:
        return internals.output_shocked(shocked_path_internal=self.shocked_path_internal)

    @cached_property
    def output_delta(self) -> data.OutputDelta:
        return internals.output_delta(output_baseline=self.output_baseline, output_shocked=self.output_shocked)

@publish(data.OUTPUT_BASELINE.schema, constants=_CONSTANTS_0, cells=data.OUTPUT_BASELINE.cells)
def compute_output_baseline(*, country_name: str, country_initial_debt: data.CountryInitialDebt, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline) -> data.OutputBaseline:
    """Compute `output_baseline` using authored coordinate identities."""
    return Model(**locals()).output_baseline

@publish(data.OUTPUT_SHOCKED.schema, constants=_CONSTANTS_1, cells=data.OUTPUT_SHOCKED.cells)
def compute_output_shocked(*, country_name: str, country_initial_debt: data.CountryInitialDebt, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline, shock_year: int | str, shock_type: int | str, shock_magnitudes: data.ShockMagnitudes) -> data.OutputShocked:
    """Compute `output_shocked` using authored coordinate identities."""
    return Model(**locals()).output_shocked

@publish(data.OUTPUT_DELTA.schema, constants=_CONSTANTS_1, cells=data.OUTPUT_DELTA.cells)
def compute_output_delta(*, country_name: str, country_initial_debt: data.CountryInitialDebt, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline, shock_year: int | str, shock_type: int | str, shock_magnitudes: data.ShockMagnitudes) -> data.OutputDelta:
    """Compute `output_delta` using authored coordinate identities."""
    return Model(**locals()).output_delta

__all__ = [
    'Model',
    'compute_output_baseline',
    'compute_output_shocked',
    'compute_output_delta',
]
