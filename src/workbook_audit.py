"""Quantitative pre-extraction workbook audit CLI."""

from __future__ import annotations

import argparse
import importlib
import re
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import fastpyxl

DYNAMIC_REF_FUNCTIONS = frozenset(
    {
        "OFFSET",
        "INDEX",
        "MATCH",
        "CHOOSE",
        "INDIRECT",
        "XLOOKUP",
        "HLOOKUP",
        "VLOOKUP",
        "LOOKUP",
    }
)
EXTERNAL_FORMULA_RE = re.compile(r"\[[^\]]+\]")
FUNCTION_CALL_RE = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_.]*)\(")
FORMULA_IDENTIFIER_RE = re.compile(
    r"(?<![A-Z0-9_])([A-Za-z_][A-Za-z0-9_.]*)(?![A-Z0-9_])"
)
NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

DEFAULT_AUDIT_TITLE = "Workbook Audit"


@dataclass(frozen=True)
class SheetStats:
    name: str
    state: str
    sheet_type: str
    non_empty: int
    value_cells: int
    formula_cells: int
    error_cells: int


@dataclass(frozen=True)
class NamedRangeRecord:
    name: str
    target: str
    external: bool
    broken: bool


@dataclass(frozen=True)
class ExternalLinkRecord:
    index: int
    target_path: str
    sheet_names: tuple[str, ...]


@dataclass(frozen=True)
class AutomationAudit:
    vba_project: bool
    macro_sheets: tuple[str, ...]
    dialog_sheets: tuple[str, ...]
    workbook_connections: tuple[str, ...]
    external_links: tuple[ExternalLinkRecord, ...]
    form_controls: tuple[str, ...]
    calc_chain_present: bool


@dataclass
class WorkbookAuditReport:
    workbook_path: Path
    generated_at: str
    audit_title: str
    cell_totals: dict[str, int]
    sheet_stats: list[SheetStats]
    function_call_sites: Counter[str]
    function_formula_counts: Counter[str]
    dynamic_ref_call_sites: Counter[str]
    arithmetic_only_formulas: int
    external_formula_refs: list[tuple[str, str, str]]
    named_ranges: list[NamedRangeRecord]
    named_range_formula_usage: Counter[str]
    automation: AutomationAudit
    chart_xml_files: int
    drawing_files: int
    index_match_by_sheet: Counter[str]
    public_inputs: tuple[tuple[str, str, str], ...] = ()
    guide_use_cases: tuple[tuple[str, str, str], ...] = ()
    public_input_reference_counts: dict[str, int] = field(default_factory=dict)


def count_function_call_sites(formula: str) -> Counter[str]:
    """Count every function invocation in a formula, including nested calls."""
    return Counter(
        match.group(1) for match in FUNCTION_CALL_RE.finditer(formula.upper())
    )


def _defined_name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Z0-9_]){re.escape(name)}(?![A-Z0-9_])",
        re.IGNORECASE,
    )


def count_named_range_formula_usage(
    formulas: Iterable[str],
    defined_names: Iterable[str],
) -> Counter[str]:
    """Count formulas that reference each defined name with word-boundary semantics."""
    names = list(defined_names)
    if not names:
        return Counter()

    formulas_list = list(formulas)
    if not formulas_list:
        return Counter()

    usage: Counter[str] = Counter()
    if len(names) <= len(formulas_list):
        for name in names:
            pattern = _defined_name_pattern(name)
            count = sum(1 for formula in formulas_list if pattern.search(formula))
            if count:
                usage[name] = count
        return usage

    name_by_upper = {name.upper(): name for name in names}
    for formula in formulas_list:
        seen_in_formula: set[str] = set()
        for match in FORMULA_IDENTIFIER_RE.finditer(formula):
            canonical = name_by_upper.get(match.group(1).upper())
            if canonical is not None and canonical not in seen_in_formula:
                seen_in_formula.add(canonical)
                usage[canonical] += 1
    return usage


def load_audit_config() -> tuple[
    str, tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]
]:
    """Load optional workbook-specific audit hooks from ``workbook_config``."""
    user_config = importlib.import_module("workbook_config")
    audit_title = str(getattr(user_config, "AUDIT_TITLE", DEFAULT_AUDIT_TITLE))
    public_inputs = tuple(getattr(user_config, "AUDIT_PUBLIC_INPUTS", ()))
    guide_use_cases = tuple(getattr(user_config, "AUDIT_GUIDE_USE_CASES", ()))
    return audit_title, public_inputs, guide_use_cases


def _sheet_records(
    workbook_path: Path,
) -> tuple[list[dict[str, str]], AutomationAudit, int, int, list[str]]:
    automation_links: list[ExternalLinkRecord] = []
    macro_sheets: list[str] = []
    dialog_sheets: list[str] = []
    connections: list[str] = []
    form_controls: list[str] = []
    chart_files = 0
    drawing_files = 0
    calc_chain_present = False

    with zipfile.ZipFile(workbook_path) as archive:
        names = set(archive.namelist())
        calc_chain_present = "xl/calcChain.xml" in names
        chart_files = sum(
            1 for name in names if "/charts/" in name or "/chartsheets/" in name
        )
        drawing_files = sum(1 for name in names if "/drawings/" in name)
        form_controls = [
            name
            for name in names
            if "ctrlProp" in name or "vmlDrawing" in name or "activex" in name.lower()
        ]

        workbook_xml = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_xml = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {
            rel.get("Id"): rel.get("Target", "")
            for rel in rels_xml.findall("rel:Relationship", NS)
        }

        sheets: list[dict[str, str]] = []
        for sheet in workbook_xml.findall("main:sheets/main:sheet", NS):
            rid = sheet.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            target = rid_to_target.get(rid, "")
            sheet_type = "worksheet"
            if "chartsheets/" in target:
                sheet_type = "chartsheet"
            elif "macrosheets/" in target:
                sheet_type = "macro"
                macro_sheets.append(sheet.get("name", ""))
            elif "dialogsheets/" in target:
                sheet_type = "dialog"
                dialog_sheets.append(sheet.get("name", ""))
            sheets.append(
                {
                    "name": sheet.get("name", ""),
                    "state": sheet.get("state", "visible"),
                    "type": sheet_type,
                }
            )

        if "xl/connections.xml" in names:
            conn_xml = ET.fromstring(archive.read("xl/connections.xml"))
            for conn in conn_xml.findall(".//main:connection", NS):
                connections.append(conn.get("name") or conn.get("id") or "unnamed")

        for index in range(1, 20):
            link_path = f"xl/externalLinks/externalLink{index}.xml"
            if link_path not in names:
                continue
            link_xml = ET.fromstring(archive.read(link_path))
            sheet_names: list[str] = []
            for book in link_xml.findall(".//main:externalBook", NS):
                sheet_names.extend(
                    sheet.get("val", "")
                    for sheet in book.findall("main:sheetNames/main:sheetName", NS)
                )
            rels_path = f"xl/externalLinks/_rels/externalLink{index}.xml.rels"
            target_path = ""
            if rels_path in names:
                rels = ET.fromstring(archive.read(rels_path))
                for rel in rels.findall("rel:Relationship", NS):
                    if rel.get("Type", "").endswith("externalLinkPath"):
                        target_path = rel.get("Target", "")
            automation_links.append(
                ExternalLinkRecord(
                    index=index,
                    target_path=target_path,
                    sheet_names=tuple(sheet_names),
                )
            )

    automation = AutomationAudit(
        vba_project="xl/vbaProject.bin" in names,
        macro_sheets=tuple(macro_sheets),
        dialog_sheets=tuple(dialog_sheets),
        workbook_connections=tuple(connections),
        external_links=tuple(automation_links),
        form_controls=tuple(form_controls),
        calc_chain_present=calc_chain_present,
    )
    return sheets, automation, chart_files, drawing_files, form_controls


def _count_address_references(formula: str, address: str) -> int:
    """Count how many times a sheet-qualified or local address appears in a formula."""
    sheet_prefix = ""
    cell_part = address.upper()
    if "!" in address:
        sheet_prefix, cell_part = address.split("!", maxsplit=1)
        sheet_prefix = sheet_prefix.strip("'").upper()
    cell_part = cell_part.replace("$", "")
    col_row = re.match(r"([A-Z]+)(\d+)", cell_part)
    if col_row is None:
        return len(re.findall(re.escape(address), formula, flags=re.IGNORECASE))
    col, row = col_row.group(1), col_row.group(2)
    if sheet_prefix:
        pattern = re.compile(
            rf"{re.escape(sheet_prefix)}!\$?{col}\$?{row}\b",
            re.IGNORECASE,
        )
    else:
        pattern = re.compile(rf"(?<![A-Z0-9_])\$?{col}\$?{row}\b", re.IGNORECASE)
    return len(pattern.findall(formula))


def _public_input_reference_counts(
    public_inputs: tuple[tuple[str, str, str], ...],
    formulas: Iterable[tuple[str, str]],
) -> dict[str, int]:
    counts: dict[str, int] = {label: 0 for label, _, _ in public_inputs}
    for _, formula in formulas:
        for label, address, _ in public_inputs:
            for token in re.split(r"\s*,\s*", address):
                counts[label] += _count_address_references(formula, token.strip())
    return counts


def audit_workbook(
    workbook_path: Path,
    *,
    audit_title: str = DEFAULT_AUDIT_TITLE,
    public_inputs: tuple[tuple[str, str, str], ...] = (),
    guide_use_cases: tuple[tuple[str, str, str], ...] = (),
) -> WorkbookAuditReport:
    from datetime import UTC, datetime

    sheet_meta, automation, chart_files, drawing_files, _form_controls = _sheet_records(
        workbook_path
    )
    sheet_meta_by_name = {sheet["name"]: sheet for sheet in sheet_meta}

    wb = fastpyxl.load_workbook(str(workbook_path), data_only=False)

    cell_totals = {
        "non_empty": 0,
        "value_cells": 0,
        "formula_cells": 0,
        "error_cells": 0,
    }
    sheet_counts: dict[str, dict[str, int | str]] = {}
    function_call_sites: Counter[str] = Counter()
    function_formula_counts: Counter[str] = Counter()
    dynamic_ref_call_sites: Counter[str] = Counter()
    external_formula_refs: list[tuple[str, str, str]] = []
    index_match_by_sheet: Counter[str] = Counter()
    arithmetic_only_formulas = 0
    formula_records: list[tuple[str, str]] = []

    named_ranges: list[NamedRangeRecord] = []
    for name, defined_name in wb.defined_names.items():
        target = defined_name.attr_text
        external = target.startswith(("[", "'[")) or ":\\" in target
        broken = "#REF!" in target.upper()
        named_ranges.append(
            NamedRangeRecord(name=name, target=target, external=external, broken=broken)
        )

    for sheet_name in wb.sheetnames:
        meta = sheet_meta_by_name.get(
            sheet_name, {"state": "visible", "type": "worksheet"}
        )
        counts: dict[str, int | str] = {
            "name": sheet_name,
            "state": meta["state"],
            "sheet_type": meta["type"],
            "non_empty": 0,
            "value_cells": 0,
            "formula_cells": 0,
            "error_cells": 0,
        }
        ws = wb[sheet_name]
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if value is None:
                    continue
                counts["non_empty"] = int(counts["non_empty"]) + 1
                cell_totals["non_empty"] += 1
                if isinstance(value, str) and value.startswith("="):
                    counts["formula_cells"] = int(counts["formula_cells"]) + 1
                    cell_totals["formula_cells"] += 1
                    formula = value[1:]
                    upper = formula.upper()
                    formula_records.append((sheet_name, formula))
                    if EXTERNAL_FORMULA_RE.search(formula):
                        external_formula_refs.append(
                            (sheet_name, cell.coordinate, formula[:160])
                        )
                    call_sites = count_function_call_sites(formula)
                    if call_sites:
                        function_call_sites.update(call_sites)
                        function_formula_counts.update(call_sites.keys())
                    else:
                        arithmetic_only_formulas += 1
                    for dynamic_function in DYNAMIC_REF_FUNCTIONS:
                        dynamic_ref_call_sites[dynamic_function] += call_sites.get(
                            dynamic_function, 0
                        )
                    if "INDEX(" in upper and "MATCH(" in upper:
                        index_match_by_sheet[sheet_name] += 1
                elif isinstance(value, str) and value.startswith("#"):
                    counts["error_cells"] = int(counts["error_cells"]) + 1
                    cell_totals["error_cells"] += 1
                else:
                    counts["value_cells"] = int(counts["value_cells"]) + 1
                    cell_totals["value_cells"] += 1
        sheet_counts[sheet_name] = counts

    sheet_stats = [
        SheetStats(
            name=str(counts["name"]),
            state=str(counts["state"]),
            sheet_type=str(counts["sheet_type"]),
            non_empty=int(counts["non_empty"]),
            value_cells=int(counts["value_cells"]),
            formula_cells=int(counts["formula_cells"]),
            error_cells=int(counts["error_cells"]),
        )
        for counts in sheet_counts.values()
    ]

    named_range_formula_usage = count_named_range_formula_usage(
        (formula for _, formula in formula_records),
        wb.defined_names.keys(),
    )

    return WorkbookAuditReport(
        workbook_path=workbook_path.resolve(),
        generated_at=datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC"),
        audit_title=audit_title,
        cell_totals=cell_totals,
        sheet_stats=sheet_stats,
        function_call_sites=function_call_sites,
        function_formula_counts=function_formula_counts,
        dynamic_ref_call_sites=dynamic_ref_call_sites,
        arithmetic_only_formulas=arithmetic_only_formulas,
        external_formula_refs=external_formula_refs,
        named_ranges=sorted(named_ranges, key=lambda item: item.name.lower()),
        named_range_formula_usage=named_range_formula_usage,
        automation=automation,
        chart_xml_files=chart_files,
        drawing_files=drawing_files,
        index_match_by_sheet=index_match_by_sheet,
        public_inputs=public_inputs,
        guide_use_cases=guide_use_cases,
        public_input_reference_counts=_public_input_reference_counts(
            public_inputs, formula_records
        ),
    )


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _markdown_table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header_row = list(headers)
    body = [list(row) for row in rows]
    lines = [
        "| " + " | ".join(header_row) + " |",
        "| " + " | ".join("---" for _ in header_row) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def render_audit_markdown(report: WorkbookAuditReport) -> str:
    stale_named_ranges = [
        item for item in report.named_ranges if item.external or item.broken
    ]
    used_named_ranges = {
        name for name, count in report.named_range_formula_usage.items() if count
    }
    blocking_items: list[str] = []
    watch_items: list[str] = []

    if report.automation.vba_project:
        blocking_items.append("VBA project present (`xl/vbaProject.bin`).")
    if report.automation.macro_sheets:
        blocking_items.append(
            f"Macro sheets: {', '.join(report.automation.macro_sheets)}."
        )
    if report.automation.dialog_sheets:
        blocking_items.append(
            f"Dialog sheets: {', '.join(report.automation.dialog_sheets)}."
        )
    if report.automation.workbook_connections:
        blocking_items.append(
            "Workbook data connections present: "
            + ", ".join(report.automation.workbook_connections)
            + "."
        )
    if report.external_formula_refs:
        blocking_items.append(
            f"{len(report.external_formula_refs)} live external workbook formula references."
        )

    if report.automation.external_links:
        watch_items.append(
            f"{len(report.automation.external_links)} stale external-link metadata records "
            "(no formulas reference them)."
        )
    if stale_named_ranges:
        unused_stale = [
            item.name
            for item in stale_named_ranges
            if item.name not in used_named_ranges
        ]
        if unused_stale:
            watch_items.append(
                "Stale named ranges pointing at prior workbooks/files: "
                + ", ".join(unused_stale)
                + "."
            )
    dynamic_ref_total = sum(report.dynamic_ref_call_sites.values())
    if dynamic_ref_total:
        watch_items.append(
            "Dynamic-reference functions require explicit constraints before graph extraction "
            f"({_fmt_int(dynamic_ref_total)} dynamic-ref call sites)."
        )
    largest_sheets = sorted(
        report.sheet_stats, key=lambda item: item.non_empty, reverse=True
    )[:3]
    if largest_sheets:
        watch_items.append(
            "Largest sheets by non-empty cell count: "
            + ", ".join(
                f"{sheet.name} ({_fmt_int(sheet.non_empty)} cells)"
                for sheet in largest_sheets
            )
            + "."
        )

    can_proceed = not blocking_items
    recommendation = (
        "**Proceed with the no-macro extraction pipeline.**"
        if can_proceed
        else "**Do not proceed** until blocking automation dependencies are resolved."
    )

    sheet_rows = [
        (
            sheet.name,
            sheet.state,
            sheet.sheet_type,
            _fmt_int(sheet.non_empty),
            _fmt_int(sheet.value_cells),
            _fmt_int(sheet.formula_cells),
        )
        for sheet in report.sheet_stats
    ]
    formulas_with_functions = (
        report.cell_totals["formula_cells"] - report.arithmetic_only_formulas
    )
    function_rows = [
        (
            name,
            _fmt_int(report.function_call_sites[name]),
            _fmt_int(report.function_formula_counts[name]),
        )
        for name in report.function_call_sites
    ]
    function_rows.sort(key=lambda row: int(row[1].replace(",", "")), reverse=True)
    total_call_sites = sum(report.function_call_sites.values())
    total_formulas_with_functions = _fmt_int(formulas_with_functions)
    function_rows.append(
        (
            "**Total (all functions)**",
            _fmt_int(total_call_sites),
            total_formulas_with_functions,
        )
    )
    dynamic_rows = [
        (name, _fmt_int(count))
        for name, count in report.dynamic_ref_call_sites.most_common()
        if count
    ]
    named_rows = [
        (
            item.name,
            item.target,
            "yes" if item.external else "no",
            "yes" if item.broken else "no",
            _fmt_int(report.named_range_formula_usage.get(item.name, 0)),
        )
        for item in report.named_ranges
    ]
    external_link_rows = [
        (
            str(link.index),
            link.target_path or "(missing path)",
            _fmt_int(len(link.sheet_names)),
        )
        for link in report.automation.external_links
    ]

    lines = [
        f"# {report.audit_title}",
        "",
        f"- **Workbook:** `{report.workbook_path}`",
        f"- **Generated:** {report.generated_at}",
        "- **Tooling:** `uv run python -m src.workbook_audit`",
        "",
        "## Executive summary",
        "",
        recommendation,
        "",
        "### Blocking issues",
        "",
    ]
    if blocking_items:
        lines.extend(f"- {item}" for item in blocking_items)
    else:
        lines.append("- None identified.")

    lines.extend(["", "### Watch items (non-blocking)", ""])
    if watch_items:
        lines.extend(f"- {item}" for item in watch_items)
    else:
        lines.append("- None identified.")

    formula_cell_count = report.cell_totals["formula_cells"]
    function_summary = (
        "A **call site** is one function invocation inside a formula. Nested calls count "
        "separately."
    )
    if formula_cell_count:
        arithmetic_share = report.arithmetic_only_formulas / formula_cell_count
        function_summary += (
            f" {_fmt_int(report.arithmetic_only_formulas)} of "
            f"{_fmt_int(formula_cell_count)} formulas ({arithmetic_share:.0%}) "
            "use no functions — only cell references, arithmetic, and comparisons. "
            f"{total_formulas_with_functions} formulas contain at least one function call."
        )

    lines.extend(
        [
            "",
            "## Quantitative surface",
            "",
            "### Cell totals",
            "",
            _markdown_table(
                ("Metric", "Count"),
                (
                    ("Non-empty cells", _fmt_int(report.cell_totals["non_empty"])),
                    ("Value cells", _fmt_int(report.cell_totals["value_cells"])),
                    ("Formula cells", _fmt_int(report.cell_totals["formula_cells"])),
                    ("Error cells", _fmt_int(report.cell_totals["error_cells"])),
                    (
                        "Arithmetic-only formulas",
                        _fmt_int(report.arithmetic_only_formulas),
                    ),
                ),
            ),
            "",
            "### Formulas by sheet",
            "",
            _markdown_table(
                ("Sheet", "Visibility", "Type", "Non-empty", "Values", "Formulas"),
                sheet_rows,
            ),
            "",
            "### Excel function invocations",
            "",
            function_summary,
            "",
            _markdown_table(
                ("Function", "Call sites", "Formulas containing"),
                function_rows or [("—", "0", "0")],
            ),
            "",
            "### Dynamic-reference functions",
            "",
            _markdown_table(("Function", "Call sites"), dynamic_rows or [("—", "0")]),
        ]
    )

    if report.index_match_by_sheet:
        lines.extend(
            [
                "",
                "### INDEX+MATCH formulas by sheet",
                "",
                _markdown_table(
                    ("Sheet", "INDEX+MATCH formulas"),
                    (
                        (sheet, _fmt_int(count))
                        for sheet, count in report.index_match_by_sheet.most_common()
                    ),
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Automation and external dependencies",
            "",
            _markdown_table(
                ("Feature", "Present", "Notes"),
                (
                    (
                        "VBA project",
                        "yes" if report.automation.vba_project else "no",
                        "",
                    ),
                    (
                        "Macro sheets",
                        "yes" if report.automation.macro_sheets else "no",
                        ", ".join(report.automation.macro_sheets) or "—",
                    ),
                    (
                        "Dialog sheets",
                        "yes" if report.automation.dialog_sheets else "no",
                        ", ".join(report.automation.dialog_sheets) or "—",
                    ),
                    (
                        "Workbook connections",
                        "yes" if report.automation.workbook_connections else "no",
                        ", ".join(report.automation.workbook_connections) or "—",
                    ),
                    (
                        "Form controls / ActiveX",
                        "yes" if report.automation.form_controls else "no",
                        ", ".join(report.automation.form_controls) or "—",
                    ),
                    (
                        "Calc chain",
                        "yes" if report.automation.calc_chain_present else "no",
                        "Normal for formula workbooks",
                    ),
                    (
                        "Live external formula refs",
                        "yes" if report.external_formula_refs else "no",
                        _fmt_int(len(report.external_formula_refs)),
                    ),
                ),
            ),
            "",
            "### Stale external-link metadata (not referenced by formulas)",
            "",
        ]
    )
    if external_link_rows:
        lines.append(
            _markdown_table(
                ("Link #", "Original path", "Cached sheet names"), external_link_rows
            )
        )
    else:
        lines.append("None.")

    lines.extend(
        [
            "",
            "## Named ranges",
            "",
            _markdown_table(
                ("Name", "Target", "External", "Broken", "Formula uses"),
                named_rows or [("—", "—", "no", "no", "0")],
            ),
        ]
    )

    if report.public_inputs:
        public_input_rows = [
            (
                label,
                address,
                notes,
                _fmt_int(report.public_input_reference_counts.get(label, 0)),
            )
            for label, address, notes in report.public_inputs
        ]
        lines.extend(
            [
                "",
                "## Public input inventory",
                "",
                _markdown_table(
                    ("Input", "Address", "Role", "Workbook refs"), public_input_rows
                ),
            ]
        )

    if report.guide_use_cases:
        use_case_rows = [
            (name, surfaces, inputs)
            for name, surfaces, inputs in report.guide_use_cases
        ]
        lines.extend(
            [
                "",
                "## Guide-described workflows",
                "",
                _markdown_table(
                    ("Workflow", "Workbook surfaces", "Key inputs/outputs"),
                    use_case_rows,
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Charts and drawings",
            "",
            f"- Embedded chart XML files: **{report.chart_xml_files}**",
            f"- Drawing parts: **{report.drawing_files}**",
            "",
            "Charts are presentation-only; extraction targets should focus on numeric output tables.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a quantitative pre-extraction workbook audit."
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=None,
        help="Path to workbook (defaults to workbook_config.WORKBOOK_PATH).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/workbook-audit.md"),
        help="Markdown output path.",
    )
    args = parser.parse_args(argv)

    audit_title, public_inputs, guide_use_cases = load_audit_config()

    workbook_path = args.workbook
    if workbook_path is None:
        from workbook_config import WORKBOOK_PATH

        workbook_path = WORKBOOK_PATH

    report = audit_workbook(
        workbook_path,
        audit_title=audit_title,
        public_inputs=public_inputs,
        guide_use_cases=guide_use_cases,
    )
    markdown = render_audit_markdown(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
