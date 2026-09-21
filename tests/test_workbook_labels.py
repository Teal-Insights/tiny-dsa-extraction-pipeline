from __future__ import annotations

from typing import Annotated, Literal

import pytest

import workbook_config
from tests.differential.workbook_labels import (
    literal_constraint_values,
    resolve_public_input_value,
    validate_reference_labels,
)


def test_resolve_public_input_value_exact_match() -> None:
    choices = ("High ", "Low", "Baseline")
    assert resolve_public_input_value("High ", choices) == "High "


def test_resolve_public_input_value_strip_match() -> None:
    choices = ("High ", "Low", "Baseline")
    assert resolve_public_input_value("High", choices) == "High "


def test_resolve_public_input_value_prefix_match() -> None:
    choices = ("Real interest rate (a)", "Nominal interest rate")
    assert (
        resolve_public_input_value("Real interest rate", choices)
        == "Real interest rate (a)"
    )


def test_resolve_public_input_value_raises_on_no_match() -> None:
    with pytest.raises(ValueError, match="no workbook label match"):
        resolve_public_input_value("Unknown", ("High ", "Low"))


def test_resolve_public_input_value_raises_on_ambiguous_strip() -> None:
    with pytest.raises(ValueError, match="ambiguous stripped match"):
        resolve_public_input_value("High", ("High ", "High  "))


def test_literal_constraint_values_extracts_literal_members() -> None:
    constraint = Annotated[str, Literal["High ", "Low"]]
    assert literal_constraint_values(constraint) == ("High ", "Low")


def test_literal_constraint_values_returns_empty_for_non_literal() -> None:
    assert literal_constraint_values(Annotated[float, "x"]) == ()


def test_validate_reference_labels_reports_missing_reference() -> None:
    failures = validate_reference_labels(
        reference_labels={},
        constraint_literals={"Inputs!A1": ("High ",)},
        scenario_values={},
    )
    assert failures == ["missing reference labels for constrained input 'Inputs!A1'"]


def test_validate_reference_labels_reports_literal_and_scenario_mismatches() -> None:
    failures = validate_reference_labels(
        reference_labels={"Inputs!A1": ("High ", "Low")},
        constraint_literals={"Inputs!A1": ("High",)},
        scenario_values={"Inputs!A1": ("Unknown", "High")},
    )
    assert failures == [
        "CONSTRAINTS literal 'High' for 'Inputs!A1' not among reference labels ['High ', 'Low']",
        "scenario value 'Unknown' for 'Inputs!A1': no workbook label match for 'Unknown' among ['High ', 'Low']",
    ]


def test_workbook_reference_labels_match_constraints_and_scenarios() -> None:
    if not workbook_config.REFERENCE_LABEL_CELLS:
        pytest.skip("REFERENCE_LABEL_CELLS not configured for this workbook")

    constraint_literals = {
        key: literal_constraint_values(constraint)
        for key, constraint in getattr(workbook_config, "CONSTRAINTS", {}).items()
        if literal_constraint_values(constraint)
    }
    scenario_values = workbook_config.REFERENCE_LABEL_SCENARIO_VALUES

    failures = validate_reference_labels(
        reference_labels=workbook_config.REFERENCE_LABEL_CELLS,
        constraint_literals=constraint_literals,
        scenario_values=scenario_values,
    )
    assert failures == [], "\n".join(failures)
