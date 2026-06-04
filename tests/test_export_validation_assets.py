"""Tests for exporting validation assets into dist/tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.export_validation_assets import export_validation_assets
from src.qmd_python_validation import (
    VALIDATION_DEPENDENCY_GROUP,
    render_dist_pyproject_toml,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_dist_pyproject_toml_includes_validation_dependency_group() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=["quarto>=0.1.0"],
        validation_dependencies=[
            "xlwings>=0.35.3",
            "pywin32>=311; sys_platform == 'win32'",
        ],
    )
    assert "[dependency-groups]" in text
    assert "dev = [" in text
    assert f"{VALIDATION_DEPENDENCY_GROUP} = [" in text
    assert '"xlwings>=0.35.3"' in text
    assert "pywin32" in text


def test_export_validation_assets_copies_harness_workbook_and_reference_reports(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    workbook_src = repo_root / "data" / "tiny-dsa.xlsx"
    harness_src = (
        repo_root / "tests" / "differential" / "differential_test_exported_library.py"
    )
    report_src = repo_root / "data" / "differential" / "exported_library"

    workbook_src.parent.mkdir(parents=True)
    workbook_src.write_bytes(b"workbook")
    harness_src.parent.mkdir(parents=True)
    harness_src.write_text("# harness\n", encoding="utf-8")
    report_src.mkdir(parents=True)
    (report_src / "parity_report.csv").write_text("scenario_id\n", encoding="utf-8")
    (report_src / "parity_report.txt").write_text("PASS\n", encoding="utf-8")

    dist_root.mkdir()
    (dist_root / "tiny_dsa").mkdir()

    export_validation_assets(repo_root=repo_root, dist_root=dist_root)

    tests_root = dist_root / "tests"
    assert (tests_root / "differential_test_exported_library.py").read_text(
        encoding="utf-8"
    ) == "# harness\n"
    assert (tests_root / "fixtures" / "tiny-dsa.xlsx").read_bytes() == b"workbook"
    assert (tests_root / "results" / "reference" / "parity_report.csv").exists()
    assert (tests_root / "results" / "reference" / "parity_report.txt").exists()
    assert (tests_root / "README.md").exists()


def test_export_validation_assets_raises_when_reference_reports_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    workbook_src = repo_root / "data" / "tiny-dsa.xlsx"
    harness_src = (
        repo_root / "tests" / "differential" / "differential_test_exported_library.py"
    )

    workbook_src.parent.mkdir(parents=True)
    workbook_src.write_bytes(b"workbook")
    harness_src.parent.mkdir(parents=True)
    harness_src.write_text("# harness\n", encoding="utf-8")
    dist_root.mkdir()

    with pytest.raises(FileNotFoundError, match="parity_report"):
        export_validation_assets(repo_root=repo_root, dist_root=dist_root)
