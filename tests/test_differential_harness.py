"""Unit tests for differential harness comparison and scenario gating."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _load_harness_module():
    return importlib.import_module(
        "tests.differential.differential_test_exported_library"
    )


def test_compare_cell_passes_within_atol() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.0000001,
        1.0,
        atol=1e-6,
    )
    assert comparison.passed is True


def test_compare_cell_fails_outside_atol() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.01,
        1.0,
        atol=1e-6,
    )
    assert comparison.passed is False


def test_compare_cell_matches_excel_error_string_to_xlerror() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#VALUE!",
        XlError.VALUE,
        atol=1e-6,
    )
    assert comparison.passed is True


def test_compare_cell_rejects_different_error_classes() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#DIV/0!",
        XlError.NA,
        atol=1e-6,
    )
    assert comparison.passed is False
    assert comparison.matched_error is False


def test_compare_cell_flags_matched_errors_for_review() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        XlError.NA,
        atol=1e-6,
    )
    assert comparison.passed is True
    assert comparison.matched_error is True
    assert comparison.flagged_matched_error is True


def test_compare_cell_suppresses_flag_when_scenario_expects_errors() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        XlError.NA,
        atol=1e-6,
        expects_error_values=True,
    )
    assert comparison.matched_error is True
    assert comparison.flagged_matched_error is False


def test_run_differential_test_requires_scenarios(tmp_path: Path, monkeypatch) -> None:
    harness = _load_harness_module()
    monkeypatch.setattr(harness, "build_scenarios", lambda: ())
    config = harness.DifferentialConfig(
        workbook_path=tmp_path / "workbook.xlsx",
        package_dir=tmp_path / "package",
        package_name="example.api",
        import_root=tmp_path,
        report_dir=tmp_path / "reports",
        library_name="Example",
    )
    with pytest.raises(RuntimeError, match="No differential scenarios"):
        harness.run_differential_test(config)
