from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from src.workbook_audit import (
    WorkbookAuditReport,
    audit_workbook,
    count_function_call_sites,
    main,
    render_audit_markdown,
)


def test_count_function_call_sites_includes_nested_ifs() -> None:
    sites = count_function_call_sites("=IF(A1,B1,IF(C1,D1,IF(E1,F1,G1)))")
    assert sites["IF"] == 3
    assert sum(sites.values()) == 3


def test_count_function_call_sites_counts_each_invocation() -> None:
    sites = count_function_call_sites("=INDEX(Z1:Z9,MATCH(A1,B1:B9,0),1)+EXP(C1)")
    assert sites == Counter({"INDEX": 1, "MATCH": 1, "EXP": 1})


@pytest.fixture(scope="module")
def synthetic_workbook_audit_report(
    synthetic_workbook_path: Path,
) -> WorkbookAuditReport:
    return audit_workbook(synthetic_workbook_path)


def test_synthetic_workbook_audit_has_no_blocking_automation(
    synthetic_workbook_audit_report: WorkbookAuditReport,
) -> None:
    report = synthetic_workbook_audit_report
    markdown = render_audit_markdown(report)

    assert not report.automation.vba_project
    assert not report.automation.macro_sheets
    assert not report.external_formula_refs
    assert report.cell_totals["formula_cells"] > 0
    assert {"Inputs", "Engine", "Outputs"} == {
        sheet.name for sheet in report.sheet_stats
    }
    assert "Proceed with the no-macro extraction pipeline." in markdown


def test_workbook_audit_writes_expected_generic_sections(
    synthetic_workbook_audit_report: WorkbookAuditReport,
    tmp_path: Path,
) -> None:
    report = synthetic_workbook_audit_report
    output = tmp_path / "audit.md"
    output.write_text(render_audit_markdown(report), encoding="utf-8")
    text = output.read_text(encoding="utf-8")

    assert "# Workbook Audit" in text
    assert "## Executive summary" in text
    assert "## Automation and external dependencies" in text
    assert "Query tables" not in text
    assert "### Formulas by sheet" in text
    assert "### Dynamic-reference functions" in text
    assert "## Named ranges" in text
    assert "## Public input inventory" not in text
    assert "## Guide-described workflows" not in text


def test_workbook_audit_cli_writes_output(
    synthetic_workbook_path: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "workbook-audit.md"
    main(["--workbook", str(synthetic_workbook_path), "--output", str(output)])
    assert output.is_file()
    text = output.read_text(encoding="utf-8")
    assert "# Tiny DSA Workbook Audit" in text
