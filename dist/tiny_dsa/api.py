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
    """Compute the baseline debt-to-GDP path for projection years 1 through 5.

    Recurse the real-terms debt-dynamics identity over the five-year horizon under the baseline macroeconomic and fiscal path, without any shock applied.

    Args:
        country_name: Name of the selected country, drawn from the country profile table; used only for labeling the output surface.
        country_initial_debt: Initial debt-to-GDP ratio at end of year 0, in percent of GDP, loaded from the country profile lookup.
        growth_baseline: Baseline real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Baseline effective real interest rates paid on outstanding general-government debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Baseline primary fiscal balances for years 1 through 5, in percent of GDP, with positive values denoting a surplus.

    Returns:
        Baseline debt-to-GDP path for projection years 1 through 5, in percent of GDP, obtained by annually applying debt(t) = debt(t-1) x (1 + r(t)) / (1 + g(t)) - primary_balance(t) from the initial debt stock.
    """
    return Model(**locals()).output_baseline

@publish(data.OUTPUT_SHOCKED.schema, constants=_CONSTANTS_1, cells=data.OUTPUT_SHOCKED.cells)
def compute_output_shocked(*, country_name: str, country_initial_debt: data.CountryInitialDebt, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline, shock_year: int | str, shock_type: int | str, shock_magnitudes: data.ShockMagnitudes) -> data.OutputShocked:
    """Compute the shocked debt-to-GDP path over the five-year projection horizon.

    Recurse the real-terms debt-dynamics identity with the selected parameter shifted by the shock magnitude from the shock year through the end of the horizon.

    Args:
        country_name: User-selected country, drawn from the country profile lookup table; used to resolve the initial debt-to-GDP ratio.
        country_initial_debt: Initial general-government debt-to-GDP ratio of the selected country at end of year 0, in percent of GDP, from which the recursion begins.
        growth_baseline: Baseline real GDP growth rates for projection years 1 through 5, in percent per annum.
        interest_baseline: Baseline real interest rates paid on outstanding general-government debt for years 1 through 5, in percent per annum; combined with growth in the snowball factor (1 + r) / (1 + g).
        primary_balance_baseline: Baseline primary fiscal balance for years 1 through 5, in percent of GDP, with positive values denoting a surplus.
        shock_year: Integer between 1 and 5 giving the first projection year in which the shock takes effect; the shock persists from that year through the end of the horizon.
        shock_type: Integer between 1 and 3 selecting the parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, and 3 for the primary balance.
        shock_magnitudes: Shock magnitudes in percentage points, one per shock type in order (growth, interest, primary balance); only the magnitude matching shock_type is applied, the others are ignored.

    Returns:
        Shocked debt-to-GDP path for projection years 1 through 5, in percent of GDP, produced by recursing the debt-dynamics identity with the shocked parameter applied from the shock year onwards.
    """
    return Model(**locals()).output_shocked

@publish(data.OUTPUT_DELTA.schema, constants=_CONSTANTS_1, cells=data.OUTPUT_DELTA.cells)
def compute_output_delta(*, country_name: str, country_initial_debt: data.CountryInitialDebt, growth_baseline: data.GrowthBaseline, interest_baseline: data.InterestBaseline, primary_balance_baseline: data.PrimaryBalanceBaseline, shock_year: int | str, shock_type: int | str, shock_magnitudes: data.ShockMagnitudes) -> data.OutputDelta:
    """Compute the year-by-year difference between the shocked and baseline debt-to-GDP paths.

    Produce the `output_delta` series that reports, for years 1 through 5, the shocked debt-to-GDP path minus the baseline path in percentage points of GDP.

    Args:
        country_name: Name of the user-selected country, drawn from the country profile lookup table.
        country_initial_debt: Initial general-government debt-to-GDP ratio for the selected country, in percent of GDP at end of year 0.
        growth_baseline: Five-year vector of baseline real GDP growth rates for years 1 through 5, in percent per annum.
        interest_baseline: Five-year vector of baseline real interest rates on outstanding debt for years 1 through 5, in percent per annum.
        primary_balance_baseline: Five-year vector of baseline primary balances for years 1 through 5, in percent of GDP, with positive values denoting a surplus.
        shock_year: First year in which the shock takes effect, an integer between 1 and 5; the shock applies from that year through the end of the horizon.
        shock_type: Parameter affected by the shock: 1 for real GDP growth, 2 for the real interest rate, or 3 for the primary balance.
        shock_magnitudes: Shock magnitudes in percentage points, one per shock type in order; only the magnitude associated with the selected shock type is applied.

    Returns:
        The `output_delta` series: for each year 1 through 5, the shocked debt-to-GDP path minus the baseline debt-to-GDP path, in percentage points.
    """
    return Model(**locals()).output_delta

__all__ = [
    'Model',
    'compute_output_baseline',
    'compute_output_shocked',
    'compute_output_delta',
]
