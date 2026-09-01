"""Output orchestrators for the inverted Tiny DSA graph.

Each ``compute_*`` function is the entry point for one output series. Its
required arguments are the mutable input leaves in that series' subgraph;
constant leaves appear as defaulted arguments. The body walks first-level
internals from the bottom of the DAG to the output cells and returns those
root values.

There are no setters and no evaluation context. Callers pass input arrays.
The orchestrator is the only place that knows computation order and where
``trim`` is applied so a shorter horizon cannot rope unused years into the
leaf closure.

Codegen recipe (per output series ``O``):
1. Take the subgraph rooted at ``O``'s cells. The argument set is that
   subgraph's leaf closure, split into required inputs vs defaulted constants.
2. Topologically order bound internals. Emit one call per internal, passing
   either a previous result or a trimmed leaf argument.
3. ``trim(series, horizon)`` where ``horizon`` is the aligned length of the
   time-series inputs being computed. Recursive series keep a year-1 prefix.
4. Return the output series values (root cells) as a tuple. ``as_records``
   is an optional binding-shaped view, not part of the computation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from . import internals
from .data import COUNTRY_PROFILE_NAMES, ENGINE_YEAR_LABELS
from .runtime import require_aligned, trim


class OutputRecord(TypedDict):
    SCENARIO: str
    TIME_PERIOD: int
    UNIT_MEASURE: str
    OBS_VALUE: float


def _horizon(*series: Sequence[object]) -> int:
    return require_aligned(*series)


def compute_output_baseline(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
) -> tuple[float, ...]:
    """Compute the baseline debt-to-GDP path (Outputs!B12:F12).

    Leaf closure is the baseline subgraph only: country selector, profile table,
    and the three baseline parameter rows. Shock inputs are not arguments.
    """
    _horizon(growth_baseline, interest_baseline, primary_balance_baseline)
    initial_debt = internals.initial_debt_resolved(
        country_name,
        country_profile_names,
        country_initial_debt,
    )
    year0 = internals.engine_initial_debt_baseline(initial_debt)
    path = internals.baseline_path_internal(
        year0,
        growth_baseline,
        interest_baseline,
        primary_balance_baseline,
    )
    return internals.output_baseline(path)


def compute_output_shocked(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
    engine_year_labels: Sequence[int] = ENGINE_YEAR_LABELS,
) -> tuple[float, ...]:
    """Compute the shocked debt-to-GDP path (Outputs!B13:F13).

    Leaf closure is the shocked subgraph: the baseline leaves plus shock year,
    shock type, shock magnitudes, and engine year labels.
    """
    horizon = _horizon(growth_baseline, interest_baseline, primary_balance_baseline)
    years = trim(engine_year_labels, horizon)
    initial_debt = internals.initial_debt_resolved(
        country_name,
        country_profile_names,
        country_initial_debt,
    )
    year0 = internals.engine_initial_debt_shocked(initial_debt)
    magnitude = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    active = internals.shock_active(years, shock_year)
    growth = internals.shocked_growth(
        trim(growth_baseline, horizon),
        shock_type,
        magnitude,
        active,
    )
    interest = internals.shocked_interest(
        trim(interest_baseline, horizon),
        shock_type,
        magnitude,
        active,
    )
    primary_balance = internals.shocked_primary_balance(
        trim(primary_balance_baseline, horizon),
        shock_type,
        magnitude,
        active,
    )
    path = internals.shocked_path_internal(year0, growth, interest, primary_balance)
    return internals.output_shocked(path)


def compute_output_delta(
    *,
    country_name: str,
    country_initial_debt: Sequence[float],
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: Sequence[float],
    country_profile_names: Sequence[str] = COUNTRY_PROFILE_NAMES,
    engine_year_labels: Sequence[int] = ENGINE_YEAR_LABELS,
) -> tuple[float, ...]:
    """Compute shocked minus baseline debt-to-GDP (Outputs!B14:F14).

    Leaf closure is the union of the baseline and shocked subgraphs, which
    coincides with the shocked closure. The orchestrator computes both paths
    from shared leaves rather than through a context object.
    """
    horizon = _horizon(growth_baseline, interest_baseline, primary_balance_baseline)
    years = trim(engine_year_labels, horizon)
    initial_debt = internals.initial_debt_resolved(
        country_name,
        country_profile_names,
        country_initial_debt,
    )
    magnitude = internals.shock_magnitude_resolved(shock_type, shock_magnitudes)
    active = internals.shock_active(years, shock_year)
    baseline = internals.output_baseline(
        internals.baseline_path_internal(
            internals.engine_initial_debt_baseline(initial_debt),
            trim(growth_baseline, horizon),
            trim(interest_baseline, horizon),
            trim(primary_balance_baseline, horizon),
        )
    )
    shocked = internals.output_shocked(
        internals.shocked_path_internal(
            internals.engine_initial_debt_shocked(initial_debt),
            internals.shocked_growth(
                trim(growth_baseline, horizon),
                shock_type,
                magnitude,
                active,
            ),
            internals.shocked_interest(
                trim(interest_baseline, horizon),
                shock_type,
                magnitude,
                active,
            ),
            internals.shocked_primary_balance(
                trim(primary_balance_baseline, horizon),
                shock_type,
                magnitude,
                active,
            ),
        )
    )
    return internals.output_delta(shocked, baseline)


def as_records(
    values: Sequence[float],
    *,
    scenario: str,
    unit_measure: str,
    time_periods: Sequence[int] | None = None,
) -> list[OutputRecord]:
    """Wrap root-cell values in the output-binding record shape."""
    periods = ENGINE_YEAR_LABELS if time_periods is None else time_periods
    require_aligned(values, periods)
    return [
        {
            "SCENARIO": scenario,
            "TIME_PERIOD": int(period),
            "UNIT_MEASURE": unit_measure,
            "OBS_VALUE": float(value),
        }
        for period, value in zip(periods, values, strict=True)
    ]


__all__ = [
    "OutputRecord",
    "as_records",
    "compute_output_baseline",
    "compute_output_delta",
    "compute_output_shocked",
]
