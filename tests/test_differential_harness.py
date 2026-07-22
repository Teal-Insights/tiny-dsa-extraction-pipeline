"""Unit tests for differential harness comparison and scenario gating."""

from __future__ import annotations

import csv
import importlib
from pathlib import Path
from typing import Any

import pytest

from tests.differential.differential_types import ATOL, Scenario


def _load_harness_module():
    return importlib.import_module(
        "tests.differential.differential_test_exported_library"
    )


def _sample_config(tmp_path: Path):
    harness = _load_harness_module()
    return harness.DifferentialConfig(
        workbook_path=tmp_path / "workbook.xlsx",
        package_dir=tmp_path / "package",
        package_name="example.api",
        import_root=tmp_path,
        report_dir=tmp_path / "reports",
        library_name="Example",
    )


def _sample_comparisons(harness) -> list:
    return [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=ATOL,
        ),
        harness.compare_cell(
            "scenario:a",
            "Outputs!B2",
            "result[year=2]",
            2.0,
            2.01,
            atol=ATOL,
        ),
    ]


@pytest.mark.parametrize(
    ("excel", "mvp", "expected_pass"),
    [
        (None, None, True),
        (None, 1.0, False),
        (1.0, None, False),
        (float("nan"), float("nan"), True),
        (float("inf"), float("inf"), True),
        (float("-inf"), float("-inf"), True),
        (1.0000001, 1.0, True),
        (1.01, 1.0, False),
    ],
)
def test_compare_cell_ladder(
    excel: Any,
    mvp: Any,
    expected_pass: bool,
) -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        excel,
        mvp,
        atol=ATOL,
    )
    assert comparison.passed is expected_pass


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


def _matched_error_comparison(harness):
    return harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        "#N/A",
        atol=ATOL,
    )


def test_write_txt_summary_fails_on_flagged_matched_errors(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _sample_config(tmp_path)
    comparisons = [_matched_error_comparison(harness)]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, config=config)

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            FAIL" in text
    assert "MATCHED ERROR VALUES" in text


def test_write_txt_summary_passes_when_matched_errors_allowed(tmp_path: Path) -> None:
    import dataclasses

    harness = _load_harness_module()
    config = dataclasses.replace(_sample_config(tmp_path), allow_matched_errors=True)
    comparisons = [_matched_error_comparison(harness)]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, config=config)

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            PASS" in text
    assert "MATCHED ERROR VALUES" in text


def test_parse_args_accepts_allow_matched_errors() -> None:
    harness = _load_harness_module()
    args = harness.parse_args(["--allow-matched-errors"])
    assert args.allow_matched_errors is True
    assert harness.parse_args([]).allow_matched_errors is False


def test_parse_args_rejects_removed_warn_flag() -> None:
    harness = _load_harness_module()
    with pytest.raises(SystemExit):
        harness.parse_args(["--warn-on-error-values"])


def test_crash_comparisons_marks_every_output_cell_failed() -> None:
    harness = _load_harness_module()
    scenario = Scenario(id="scenario:crash", inputs={})
    cell_labels = (
        ("result[year=1]", "Outputs!B1"),
        ("result[year=2]", "Outputs!B2"),
    )
    exc = RuntimeError("oracle crashed")

    comparisons = harness.crash_comparisons(scenario, cell_labels, exc)

    assert len(comparisons) == len(cell_labels)
    err_repr = f"<exception: {type(exc).__name__}: {exc}>"
    for comparison, (cell_label, cell_address) in zip(
        comparisons, cell_labels, strict=True
    ):
        assert comparison.scenario_id == scenario.id
        assert comparison.cell_address == cell_address
        assert comparison.cell_label == cell_label
        assert comparison.excel_value == err_repr
        assert comparison.mvp_value == err_repr
        assert comparison.abs_diff is None
        assert comparison.rel_diff is None
        assert comparison.passed is False


def test_write_csv_report_round_trip(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = _sample_comparisons(harness)
    report_path = tmp_path / "parity_report.csv"

    harness.write_csv_report(comparisons, report_path)

    with report_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(harness.CSV_COLUMNS)
    assert len(rows) == len(comparisons) + 1


def test_write_txt_summary_pass(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _sample_config(tmp_path)
    comparisons = [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=ATOL,
        )
    ]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, config=config)

    text = report_path.read_text(encoding="utf-8")
    assert "Acceptance bar:    100.00%" in text
    assert "Result:            PASS" in text
    assert "First divergence:" not in text


def test_write_txt_summary_fail_includes_first_divergence(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _sample_config(tmp_path)
    comparisons = _sample_comparisons(harness)
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, config=config)

    text = report_path.read_text(encoding="utf-8")
    assert "Acceptance bar:    100.00%" in text
    assert "Result:            FAIL" in text
    assert "First divergence:" in text
    assert "Outputs!B2" in text
    assert "result[year=2]" in text


def test_harness_conforms_to_standard() -> None:
    harness = _load_harness_module()

    assert harness.ATOL == 1e-6
    assert hasattr(harness, "compare_cell")
    assert hasattr(harness, "crash_comparisons")
    assert hasattr(harness, "write_csv_report")
    assert hasattr(harness, "write_txt_summary")
    assert harness.CSV_COLUMNS == (
        "scenario_id",
        "cell_address",
        "cell_label",
        "excel_value",
        "mvp_value",
        "abs_diff",
        "rel_diff",
        "passed",
        "matched_error",
        "flagged_matched_error",
    )

    both_blank = harness.compare_cell("s", "A1", "label", None, None, atol=ATOL)
    assert both_blank.passed is True

    one_blank = harness.compare_cell("s", "A1", "label", None, 1.0, atol=ATOL)
    assert one_blank.passed is False

    nan_vs_number = harness.compare_cell(
        "s",
        "A1",
        "label",
        float("nan"),
        1.0,
        atol=ATOL,
    )
    assert nan_vs_number.passed is False


def test_run_differential_test_requires_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
