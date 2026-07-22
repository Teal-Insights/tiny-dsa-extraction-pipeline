"""Unit tests for differential scenario input isolation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.differential.differential_scenario_inputs import (
    collect_scenario_input_addresses,
)
from tests.differential.differential_types import Axis, AxisPoint, Scenario


def _inputs_for_excel(scenario: Scenario) -> dict[str, Any]:
    return dict(scenario.inputs)


def test_collect_scenario_input_addresses_unions_all_writes() -> None:
    axes = (
        Axis(
            name="demo",
            points=(
                AxisPoint(
                    label="shock",
                    scenario=Scenario(
                        id="shock",
                        inputs={"Inputs!A1": 1.0, "Inputs!B1": 2.0},
                    ),
                ),
                AxisPoint(
                    label="baseline",
                    scenario=Scenario(id="baseline", inputs={"Inputs!A1": 0.0}),
                ),
            ),
        ),
    )
    addresses = collect_scenario_input_addresses(axes, _inputs_for_excel)
    assert addresses == frozenset({"Inputs!A1", "Inputs!B1"})


@dataclass
class _FakeGoldenDriver:
    """Minimal driver reproducing set_inputs carryover without Excel."""

    baselines: dict[str, Any]
    state: dict[str, Any]

    def record_input_baselines(self, cells: frozenset[str]) -> None:
        self.baselines = {cell: self.state[cell] for cell in cells}

    def reset_inputs(self) -> None:
        for cell, value in self.baselines.items():
            self.state[cell] = value

    def set_inputs(self, inputs: dict[str, Any]) -> None:
        self.state.update(inputs)


def _debt_to_gdp(*, shock: float, debt_target: float) -> float:
    base = 0.268015
    after_shock = base - 0.013274 * shock
    if debt_target > 200:
        return after_shock
    return base


def test_reset_inputs_prevents_scenario_carryover() -> None:
    """Reproduce issue #47 MCVE: undeclared inputs must not persist."""
    baseline = {"Inputs!A1": 0.0, "Inputs!B1": 60.0}
    all_cells = frozenset(baseline)
    driver = _FakeGoldenDriver(baselines={}, state=dict(baseline))
    driver.record_input_baselines(all_cells)

    scenarios: list[tuple[str, dict[str, float]]] = [
        ("discrete_shock", {"Inputs!A1": 1.0}),
        ("boundary_debt_target", {"Inputs!B1": 300.0}),
        ("canonical", {}),
    ]

    def run_without_reset() -> dict[str, float]:
        state = dict(baseline)
        results: dict[str, float] = {}
        for name, writes in scenarios:
            state.update(writes)
            results[name] = _debt_to_gdp(
                shock=state["Inputs!A1"],
                debt_target=state["Inputs!B1"],
            )
        return results

    def run_with_reset() -> dict[str, float]:
        results: dict[str, float] = {}
        for name, writes in scenarios:
            driver.reset_inputs()
            driver.set_inputs(writes)
            results[name] = _debt_to_gdp(
                shock=driver.state["Inputs!A1"],
                debt_target=driver.state["Inputs!B1"],
            )
        return results

    buggy = run_without_reset()
    fixed = run_with_reset()

    assert buggy["discrete_shock"] == 0.268015
    assert buggy["boundary_debt_target"] == 0.254741
    assert buggy["canonical"] == 0.254741

    assert fixed["discrete_shock"] == 0.268015
    assert fixed["boundary_debt_target"] == 0.268015
    assert fixed["canonical"] == 0.268015
