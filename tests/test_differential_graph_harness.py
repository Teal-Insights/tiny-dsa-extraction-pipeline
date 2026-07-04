"""Unit tests for the graph-oracle differential harness."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from excel_grapher import XlError


def _load_harness_module():
    return importlib.import_module("tests.differential.differential_test_graph")


def test_values_match_passes_within_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(1.0000001, 1.0, atol=1e-6)
    assert match is True
    assert abs_diff is not None and abs_diff <= 1e-6
    assert rel_diff is not None
    assert note == ""


def test_values_match_fails_outside_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, _, _ = harness.values_match(1.01, 1.0, atol=1e-6)
    assert match is False
    assert abs_diff == pytest.approx(0.01)


def test_values_match_treats_matching_errors_as_pass() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        "#DIV/0!",
        XlError.DIV,
        atol=1e-6,
    )
    assert match is True
    assert abs_diff is None
    assert rel_diff is None
    assert "both error" in note


def test_run_differential_test_requires_scenarios(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = harness.GraphDifferentialConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        report_dir=tmp_path / "reports",
        targets=("Outputs!B1",),
        constraints={"Inputs!A1": float},
        library_name="Example",
    )
    with pytest.raises(RuntimeError, match="No differential scenarios"):
        harness.run_differential_test(config)
