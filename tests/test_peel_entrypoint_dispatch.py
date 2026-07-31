"""Tests for peel-split published-series entry-point dispatch (issue #143).

When a published output-leaf series is peeled across schedule units, the base
helper (bearing the bare series id, the one ``api.py`` addresses) must delegate
out-of-its-regime dispatch-key values to the owning sibling helper. Otherwise the
api-layer compute loop runs the base helper for every ``TIME_PERIOD`` in the
output-leaf range and crashes on years the base regime never covered.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import cast

from src.peel_entrypoint_dispatch import inject_peel_entrypoint_dispatch


_TWO_UNIT_SOURCE = '''\
@xl_memoize
def baseline_nominal_gdp_growth(ctx: EvalContext, time_period: int) -> float:
    """Early regime.

    Note:
        Covers Baseline!D15:X15.
    """
    return macrofiscal_nominal_gdp_growth(ctx, time_period=time_period)


@xl_memoize
def baseline_nominal_gdp_growth_2(ctx: EvalContext, time_period: int) -> float:
    """Late regime.

    Note:
        Covers Baseline!Y15:CP15.
    """
    return baseline_engine_nominal_gdp_lcu(ctx, time_period=time_period)
'''


def _defs(source: str) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    }


def _base_body_calls(source: str, base: str) -> list[str]:
    """Return the callee names invoked in the base helper body, in order."""
    node = _defs(source)[base]
    return [
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    ]


def test_base_helper_delegates_sibling_domain_years():
    scheduled_helper_by_address = {
        "Baseline!D15": "baseline_nominal_gdp_growth",
        "Baseline!E15": "baseline_nominal_gdp_growth",
        "Baseline!Y15": "baseline_nominal_gdp_growth_2",
        "Baseline!Z15": "baseline_nominal_gdp_growth_2",
    }
    address_to_series_id = {
        a: "baseline_nominal_gdp_growth" for a in scheduled_helper_by_address
    }
    address_time_periods = {
        "Baseline!D15": 2009,
        "Baseline!E15": 2010,
        "Baseline!Y15": 2030,
        "Baseline!Z15": 2031,
    }

    updated, rewritten = inject_peel_entrypoint_dispatch(
        _TWO_UNIT_SOURCE,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )

    assert rewritten == ["baseline_nominal_gdp_growth"]
    # The source must still parse.
    ast.parse(updated)
    # Base helper now delegates the sibling's years to the sibling, and preserves
    # its own body for its own regime.
    calls = _base_body_calls(updated, "baseline_nominal_gdp_growth")
    assert "baseline_nominal_gdp_growth_2" in calls
    assert "macrofiscal_nominal_gdp_growth" in calls
    node = _defs(updated)["baseline_nominal_gdp_growth"]
    first_exec = node.body[1] if isinstance(node.body[0], ast.Expr) else node.body[0]
    assert isinstance(first_exec, ast.If)
    # Guard tests the shared dispatch parameter against exactly the sibling domain.
    assert isinstance(first_exec.test, ast.Compare)
    assert isinstance(first_exec.test.left, ast.Name)
    assert first_exec.test.left.id == "time_period"
    comparator = first_exec.test.comparators[0]
    assert isinstance(comparator, ast.Set)
    domain = ast.literal_eval(comparator)
    assert domain == {2030, 2031}
    # The sibling helper is untouched.
    assert "baseline_nominal_gdp_growth_2" not in rewritten


def test_idempotent_second_pass_is_noop():
    scheduled_helper_by_address = {
        "Baseline!D15": "baseline_nominal_gdp_growth",
        "Baseline!Y15": "baseline_nominal_gdp_growth_2",
    }
    address_to_series_id = {
        a: "baseline_nominal_gdp_growth" for a in scheduled_helper_by_address
    }
    address_time_periods = {"Baseline!D15": 2009, "Baseline!Y15": 2030}

    once, rewritten_once = inject_peel_entrypoint_dispatch(
        _TWO_UNIT_SOURCE,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    twice, rewritten_twice = inject_peel_entrypoint_dispatch(
        once,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    assert rewritten_once == ["baseline_nominal_gdp_growth"]
    assert rewritten_twice == []
    assert twice == once


def test_sole_unit_series_is_untouched():
    source = '''\
@xl_memoize
def lonely_series(ctx: EvalContext, time_period: int) -> float:
    """Sole unit."""
    return macrofiscal_x(ctx, time_period=time_period)
'''
    scheduled_helper_by_address = {
        "S!A1": "lonely_series",
        "S!A2": "lonely_series",
    }
    address_to_series_id = {a: "lonely_series" for a in scheduled_helper_by_address}
    address_time_periods = {"S!A1": 2009, "S!A2": 2010}

    updated, rewritten = inject_peel_entrypoint_dispatch(
        source,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    assert rewritten == []
    assert updated == source


def test_rewritten_base_routes_out_of_regime_year_to_sibling():
    # Reproduces issue #143 end to end: the early-regime body crashes (KeyError)
    # for a sibling year exactly as macrofiscal_nominal_gdp_lcu's year->column map
    # does at 2030. After the rewrite the base must delegate that year to the
    # sibling instead of running the crashing body.
    scheduled_helper_by_address = {
        "Baseline!D15": "baseline_nominal_gdp_growth",
        "Baseline!Y15": "baseline_nominal_gdp_growth_2",
    }
    address_to_series_id = {
        a: "baseline_nominal_gdp_growth" for a in scheduled_helper_by_address
    }
    address_time_periods = {"Baseline!D15": 2009, "Baseline!Y15": 2030}

    updated, rewritten = inject_peel_entrypoint_dispatch(
        _TWO_UNIT_SOURCE,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    assert rewritten == ["baseline_nominal_gdp_growth"]

    early_years = {2009}

    def macrofiscal_nominal_gdp_growth(ctx, time_period):
        if time_period not in early_years:
            raise KeyError(time_period)  # mirrors start_column_by_year[time_period]
        return ("early", time_period)

    def baseline_engine_nominal_gdp_lcu(ctx, time_period):
        return ("engine", time_period)

    namespace = {
        "xl_memoize": lambda fn: fn,
        "EvalContext": object,
        "macrofiscal_nominal_gdp_growth": macrofiscal_nominal_gdp_growth,
        "baseline_engine_nominal_gdp_lcu": baseline_engine_nominal_gdp_lcu,
    }
    exec(updated, namespace)  # noqa: S102 - trusted generated source under test

    base = cast("Callable[..., object]", namespace["baseline_nominal_gdp_growth"])
    # Own-regime year still runs the base body.
    assert base(None, 2009) == ("early", 2009)
    # Sibling-regime year is delegated instead of crashing.
    assert base(None, 2030) == ("engine", 2030)


def test_overlapping_domains_are_not_rewritten():
    # If two units for the same series claim the same dispatch-key value, the peel
    # is not a clean partition; delegating would be ambiguous, so leave it alone.
    scheduled_helper_by_address = {
        "Baseline!D15": "baseline_nominal_gdp_growth",
        "Baseline!Y15": "baseline_nominal_gdp_growth_2",
    }
    address_to_series_id = {
        a: "baseline_nominal_gdp_growth" for a in scheduled_helper_by_address
    }
    address_time_periods = {"Baseline!D15": 2030, "Baseline!Y15": 2030}

    updated, rewritten = inject_peel_entrypoint_dispatch(
        _TWO_UNIT_SOURCE,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    assert rewritten == []
    assert updated == _TWO_UNIT_SOURCE
