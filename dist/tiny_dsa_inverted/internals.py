"""First-level-dependency internals for the inverted Tiny DSA graph.

Each function is one bound internal (or output) series. Arguments are the
series' first-level cell dependencies — never a workbook context, never a
transitive leaf. Length follows the trimmed arguments: callers slice with
``trim`` before the call so unused years cannot expand the required inputs.

Codegen recipe (per bound series ``S``):
1. Collect first-level formula dependencies and group them by binding id.
2. Emit one parameter per dependency series (scalar or sequence).
3. Translate the Excel operator tree; replace ``xl_cell`` / ``xl_eval`` with
   those parameters. ``CHOOSE`` / ``MATCH`` / ``/`` become ``xl_choose`` /
   ``xl_match`` / ``xl_div``.
4. For ``layout: row_series``, zip aligned sequences. Recursive (lag) edges
   walk left-to-right from the year-0 scalar.
5. Return a tuple whose length equals the aligned argument length.
"""

from __future__ import annotations

from collections.abc import Sequence

from .runtime import XlError, require_aligned, xl_choose, xl_div, xl_match


def initial_debt_resolved(
    country_name: str,
    country_profile_names: Sequence[str],
    country_initial_debt: Sequence[float],
) -> float:
    """Resolve initial debt-to-GDP via INDEX/MATCH on the country profile table.

    Covers Inputs!B6.
    Excel: ``=INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2)``.
    """
    require_aligned(country_profile_names, country_initial_debt)
    row_index = xl_match(country_name, country_profile_names, 0)
    return float(country_initial_debt[row_index - 1])


def engine_initial_debt_baseline(initial_debt_resolved: float) -> float:
    """Year-0 baseline debt stock on the Engine sheet.

    Covers Engine!B6. Excel: ``=Inputs!B6``.
    """
    return initial_debt_resolved


def engine_initial_debt_shocked(initial_debt_resolved: float) -> float:
    """Year-0 shocked debt stock on the Engine sheet.

    Covers Engine!B20. Excel: ``=Inputs!B6``.
    """
    return initial_debt_resolved


def shock_magnitude_resolved(
    shock_type: int,
    shock_magnitudes: Sequence[float],
) -> float:
    """Select the shock-table magnitude for the configured shock type.

    Covers Engine!B9.
    Excel: ``=OFFSET(Inputs!$B$26,0,Inputs!$B$22-1)``.
    """
    if shock_type < 1 or shock_type > len(shock_magnitudes):
        raise XlError("#VALUE!")
    return float(shock_magnitudes[shock_type - 1])


def shock_active(
    engine_year_labels: Sequence[int],
    shock_year: int,
) -> tuple[int, ...]:
    """Shock-activation flag per projection year.

    Covers Engine!C10:G10.
    Excel: ``=IF(Engine!C5>=Inputs!$B$21,1,0)``.
    """
    return tuple(1 if year >= shock_year else 0 for year in engine_year_labels)


def shocked_growth(
    growth_baseline: Sequence[float],
    shock_type: int,
    shock_magnitude: float,
    shock_active: Sequence[int],
) -> tuple[float, ...]:
    """Shocked real GDP growth path.

    Covers Engine!C14:G14.
    Excel: ``=Inputs!C16+CHOOSE(Inputs!$B$22,$B$9,0,0)*Engine!C10``.
    """
    n = require_aligned(growth_baseline, shock_active)
    chosen = xl_choose(shock_type, shock_magnitude, 0.0, 0.0)
    return tuple(
        float(growth_baseline[i]) + chosen * float(shock_active[i]) for i in range(n)
    )


def shocked_interest(
    interest_baseline: Sequence[float],
    shock_type: int,
    shock_magnitude: float,
    shock_active: Sequence[int],
) -> tuple[float, ...]:
    """Shocked real interest-rate path.

    Covers Engine!C15:G15.
    Excel: ``=Inputs!C17+CHOOSE(Inputs!$B$22,0,$B$9,0)*Engine!C10``.
    """
    n = require_aligned(interest_baseline, shock_active)
    chosen = xl_choose(shock_type, 0.0, shock_magnitude, 0.0)
    return tuple(
        float(interest_baseline[i]) + chosen * float(shock_active[i]) for i in range(n)
    )


def shocked_primary_balance(
    primary_balance_baseline: Sequence[float],
    shock_type: int,
    shock_magnitude: float,
    shock_active: Sequence[int],
) -> tuple[float, ...]:
    """Shocked primary-balance path.

    Covers Engine!C16:G16.
    Excel: ``=Inputs!C18+CHOOSE(Inputs!$B$22,0,0,$B$9)*Engine!C10``.
    """
    n = require_aligned(primary_balance_baseline, shock_active)
    chosen = xl_choose(shock_type, 0.0, 0.0, shock_magnitude)
    return tuple(
        float(primary_balance_baseline[i]) + chosen * float(shock_active[i])
        for i in range(n)
    )


def _debt_step(
    prior_debt: float,
    interest_rate: float,
    growth_rate: float,
    primary_balance: float,
) -> float:
    """One-year real-terms debt-dynamics identity."""
    interest_term = 1.0 + xl_div(interest_rate, 100.0)
    growth_term = 1.0 + xl_div(growth_rate, 100.0)
    return xl_div(prior_debt * interest_term, growth_term) - primary_balance


def baseline_path_internal(
    initial_debt: float,
    growth_baseline: Sequence[float],
    interest_baseline: Sequence[float],
    primary_balance_baseline: Sequence[float],
) -> tuple[float, ...]:
    """Baseline debt-to-GDP path, recursed from year 0.

    Covers Engine!C6:G6.
    Excel (year 1): ``=B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18``.
    Later years read the previous Engine!{col}6 cell in place of B6.
    """
    n = require_aligned(growth_baseline, interest_baseline, primary_balance_baseline)
    path: list[float] = []
    prior = initial_debt
    for i in range(n):
        prior = _debt_step(
            prior,
            float(interest_baseline[i]),
            float(growth_baseline[i]),
            float(primary_balance_baseline[i]),
        )
        path.append(prior)
    return tuple(path)


def shocked_path_internal(
    initial_debt: float,
    shocked_growth: Sequence[float],
    shocked_interest: Sequence[float],
    shocked_primary_balance: Sequence[float],
) -> tuple[float, ...]:
    """Shocked debt-to-GDP path, recursed from year 0.

    Covers Engine!C20:G20.
    Excel (year 1): ``=B20*(1+C15/100)/(1+C14/100)-C16``.
    First-level deps are the shocked parameter rows, not the raw shock inputs.
    """
    n = require_aligned(shocked_growth, shocked_interest, shocked_primary_balance)
    path: list[float] = []
    prior = initial_debt
    for i in range(n):
        prior = _debt_step(
            prior,
            float(shocked_interest[i]),
            float(shocked_growth[i]),
            float(shocked_primary_balance[i]),
        )
        path.append(prior)
    return tuple(path)


def output_baseline(baseline_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Outputs-sheet baseline path (passthrough of the engine series).

    Covers Outputs!B12:F12. Excel: ``=Engine!C6``.
    """
    return tuple(float(value) for value in baseline_path_internal)


def output_shocked(shocked_path_internal: Sequence[float]) -> tuple[float, ...]:
    """Outputs-sheet shocked path (passthrough of the engine series).

    Covers Outputs!B13:F13. Excel: ``=Engine!C20``.
    """
    return tuple(float(value) for value in shocked_path_internal)


def output_delta(
    output_shocked: Sequence[float],
    output_baseline: Sequence[float],
) -> tuple[float, ...]:
    """Shocked minus baseline debt-to-GDP, in percentage points.

    Covers Outputs!B14:F14. Excel: ``=Outputs!B13-Outputs!B12``.
    """
    n = require_aligned(output_shocked, output_baseline)
    return tuple(float(output_shocked[i]) - float(output_baseline[i]) for i in range(n))
