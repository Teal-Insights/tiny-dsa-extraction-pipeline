"""Lock-in tests for committed differential evidence layout and oracles (issue #54)."""

from __future__ import annotations

import csv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GRAPH_REPORT_DIR = REPO_ROOT / "data" / "differential" / "graph"
LIBRARY_REPORT_DIR = REPO_ROOT / "data" / "differential" / "exported_library"
DIFFERENTIAL_ROOT = REPO_ROOT / "data" / "differential"
EXPECTED_COMPARISONS = 118 * 15


def _csv_row_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return len(rows)


def test_graph_reports_live_under_graph_dir_not_differential_root() -> None:
    assert (GRAPH_REPORT_DIR / "differential_report.csv").is_file()
    assert (GRAPH_REPORT_DIR / "differential_report.txt").is_file()
    leftover_csv = DIFFERENTIAL_ROOT / "differential_report.csv"
    leftover_txt = DIFFERENTIAL_ROOT / "differential_report.txt"
    assert not leftover_csv.exists()
    assert not leftover_txt.exists()


def test_committed_graph_report_matches_118_by_15_matrix() -> None:
    csv_path = GRAPH_REPORT_DIR / "differential_report.csv"
    txt = (GRAPH_REPORT_DIR / "differential_report.txt").read_text(encoding="utf-8")
    assert _csv_row_count(csv_path) == EXPECTED_COMPARISONS
    assert f"Total comparisons: {EXPECTED_COMPARISONS}" in txt
    assert "FormulaEvaluator" in txt
    assert "Excel via xlwings" in txt


def test_committed_library_reports_use_formula_evaluator_oracle() -> None:
    csv_path = LIBRARY_REPORT_DIR / "parity_report.csv"
    txt = (LIBRARY_REPORT_DIR / "parity_report.txt").read_text(encoding="utf-8")
    with csv_path.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert "graph_value" in header
    assert "excel_value" not in header
    assert _csv_row_count(csv_path) == EXPECTED_COMPARISONS
    assert f"Total comparisons: {EXPECTED_COMPARISONS}" in txt
    assert "FormulaEvaluator" in txt
    assert "compute_*" in txt
    assert "vs Excel" not in txt
    assert "xlwings" not in txt
    assert "excel-grapher:" in txt
    assert "Excel:" not in txt
