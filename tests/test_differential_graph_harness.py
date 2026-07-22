"""Unit tests for the graph-oracle differential harness."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from excel_grapher import XlError
from excel_grapher.core.address_keys import normalize_key, parse_address


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


def _graph_config(tmp_path: Path, **overrides):
    harness = _load_harness_module()
    kwargs = dict(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        report_dir=tmp_path / "reports",
        targets=("Outputs!B1",),
        constraints={"Inputs!A1": float},
        library_name="Example",
    )
    kwargs.update(overrides)
    return harness.GraphDifferentialConfig(**kwargs)


def _matched_error_trial(harness):
    return harness.Trial(
        axis="scenarios",
        point_label="a",
        scenario_id="scenario:a",
        output_label="result[year=1]",
        cell="Outputs!B1",
        golden=XlError.NA,
        mvp=XlError.NA,
        match=True,
        abs_diff=None,
        rel_diff=None,
        note="both error: #N/A",
        matched_error=True,
        flagged_matched_error=True,
    )


def test_txt_summary_fails_on_flagged_matched_errors(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _graph_config(tmp_path)
    report_path = tmp_path / "differential_report.txt"

    harness.write_txt_summary(
        [_matched_error_trial(harness)],
        report_path,
        config=config,
        missing_inputs_in_graph=[],
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            FAIL" in text
    assert "MATCHED ERROR VALUES" in text


def test_txt_summary_passes_when_matched_errors_allowed(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _graph_config(tmp_path, allow_matched_errors=True)
    report_path = tmp_path / "differential_report.txt"

    harness.write_txt_summary(
        [_matched_error_trial(harness)],
        report_path,
        config=config,
        missing_inputs_in_graph=[],
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            PASS" in text
    assert "MATCHED ERROR VALUES" in text


def test_csv_report_includes_flagged_matched_error_column(tmp_path: Path) -> None:
    import csv

    harness = _load_harness_module()
    report_path = tmp_path / "differential_report.csv"

    harness.write_csv_report([_matched_error_trial(harness)], report_path)

    with report_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert "flagged_matched_error" in rows[0]
    assert rows[1][rows[0].index("flagged_matched_error")] == "True"


def test_parse_args_accepts_allow_matched_errors() -> None:
    harness = _load_harness_module()
    args = harness.parse_args(["--allow-matched-errors"])
    assert args.allow_matched_errors is True
    assert harness.parse_args([]).allow_matched_errors is False


def test_parse_args_rejects_removed_warn_flag() -> None:
    harness = _load_harness_module()
    with pytest.raises(SystemExit):
        harness.parse_args(["--warn-on-error-values"])


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


def test_parse_address_after_normalize_key_handles_spaced_sheet_names() -> None:
    assert parse_address(normalize_key("Discrete Risks!H2")) == ("Discrete Risks", "H2")
    assert parse_address(normalize_key("'Discrete Risks'!H2")) == (
        "Discrete Risks",
        "H2",
    )


def test_mvp_graph_driver_normalizes_sheet_names_with_spaces(tmp_path: Path) -> None:
    harness = _load_harness_module()
    canonical_key = normalize_key("Discrete Risks!H2")
    mock_graph = MagicMock()
    mock_graph.leaf_keys.return_value = [canonical_key]
    mock_graph.formula_keys.return_value = []
    mock_node = MagicMock()
    mock_node.value = 0
    mock_graph.get_node.return_value = mock_node

    with patch(
        "tests.differential.differential_test_graph.create_dependency_graph",
        return_value=mock_graph,
    ):
        driver = harness.MvpGraphDriver(
            tmp_path / "workbook.xlsx",
            targets=("Outputs!B1",),
            constraints={},
        )

    driver.set_inputs({"Discrete Risks!H2": 42})
    assert "Discrete Risks!H2" not in driver.missing_cells
    mock_graph.set_node_value.assert_called_once_with(canonical_key, 42)


def test_absent_input_preflight_uses_normalized_keys() -> None:
    canonical_key = normalize_key("Discrete Risks!H2")
    known_keys = frozenset({canonical_key})
    all_input_cells = frozenset({"Discrete Risks!H2", "Missing!A1"})

    missing = sorted(
        cell for cell in all_input_cells if normalize_key(cell) not in known_keys
    )
    assert missing == ["Missing!A1"]
