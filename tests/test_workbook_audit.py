from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import fastpyxl
import pytest
from fastpyxl.workbook.defined_name import DefinedName

from src.workbook_audit import (
    WorkbookAuditReport,
    audit_workbook,
    count_function_call_sites,
    count_named_range_formula_usage,
    main,
    render_audit_markdown,
)


def _naive_named_range_formula_usage(
    formulas: list[str],
    defined_names: list[str],
) -> Counter[str]:
    usage: Counter[str] = Counter()
    for formula in formulas:
        for name in defined_names:
            if re.search(
                rf"(?<![A-Z0-9_]){re.escape(name)}(?![A-Z0-9_])",
                formula,
                re.IGNORECASE,
            ):
                usage[name] += 1
    return usage


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
    assert "# Workbook Audit" in text


@pytest.mark.parametrize(
    ("formulas", "defined_names"),
    [
        ([], ["Alpha", "Beta"]),
        (["=A1+B1"], []),
        (["=Alpha+Beta", "=Alpha*2", "=Gamma"], ["Alpha", "Beta", "Gamma"]),
        (
            ["=Range_1+Range_2", "=Range_1*Range_10", "=MyRange+Range"],
            ["Range", "Range_1", "Range_2", "Range_10"],
        ),
        (["=alpha+ALPHA+Alpha"], ["Alpha"]),
        (["=Alpha+Alpha+Alpha"], ["Alpha"]),
    ],
)
def test_count_named_range_formula_usage_matches_naive_scan(
    formulas: list[str],
    defined_names: list[str],
) -> None:
    expected = _naive_named_range_formula_usage(formulas, defined_names)
    assert count_named_range_formula_usage(formulas, defined_names) == expected


def test_count_named_range_formula_usage_scales_for_large_workbooks() -> None:
    name_count = 500
    formula_count = 5_000
    names = [f"Range_{index}" for index in range(name_count)]
    formulas = [f"=Range_{index % name_count}+A1" for index in range(formula_count)]

    start = time.perf_counter()
    usage = count_named_range_formula_usage(formulas, names)
    elapsed = time.perf_counter() - start

    assert usage == _naive_named_range_formula_usage(formulas, names)
    assert elapsed < 2.0


def test_audit_workbook_counts_defined_name_usage(tmp_path: Path) -> None:
    workbook_path = tmp_path / "named-ranges.xlsx"
    workbook = fastpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    workbook.defined_names["GrowthRate"] = DefinedName(
        "GrowthRate", attr_text="Sheet1!$A$1"
    )
    workbook.defined_names["Discount"] = DefinedName(
        "Discount", attr_text="Sheet1!$B$1"
    )
    sheet["A1"] = 0.05
    sheet["B1"] = 0.1
    sheet["C1"] = "=GrowthRate*Discount"
    sheet["C2"] = "=GrowthRate+1"
    sheet["C3"] = "=Discount*2"
    workbook.save(workbook_path)

    report = audit_workbook(workbook_path)

    assert report.named_range_formula_usage["GrowthRate"] == 2
    assert report.named_range_formula_usage["Discount"] == 2
