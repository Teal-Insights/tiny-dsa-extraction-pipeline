"""Build the Tiny-DSA toy workbook (v0.2.0).

Tiny Debt Dynamics: a 5-year debt-to-GDP path under a baseline plus an
optional shock. Three sheets (Inputs, Engine, Outputs). Cross-sheet
references in every Engine and Outputs formula. Exercises the patterns
that Q-CRAFT v2 and the LIC DSF rebuild will hit at scale: time
recursion, stress-test branching, INDEX/MATCH against a country profile
table, OFFSET against a dynamic column index, CHOOSE for categorical
dispatch.

Version 0.2.0 incorporates Council-pass-1 modifications:

- Multi-sheet structure (Inputs, Engine, Outputs) with cross-sheet
  references in every computed cell.
- INDEX/MATCH country selector added alongside the OFFSET (the OFFSET is
  retained as a deliberate forcing function for excel-grapher's
  constraint-resolution path).
- Excel-level data validation on shock_year (1-5), shock_type (1-3),
  and country_name (drawn from the profile table).
- Three golden-master scenarios in the manifest, one per CHOOSE branch.
- Schemas field in the manifest with type and unit information per
  named range.
- Byte-deterministic build (stable workbook properties + normalized ZIP
  entry timestamps).

The model:

    debt(t) = debt(t-1) * (1 + r(t)) / (1 + g(t)) - primary_balance(t)

where debt is debt-to-GDP, r is real interest rate, g is real GDP
growth, primary_balance is primary fiscal balance as percent of GDP.
The shock is a permanent additive shift in percentage points to one of
three parameters from a configurable shock-year onwards.

Run with `python3 build_tiny_dsa.py` from this folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastpyxl import Workbook
from fastpyxl.styles import Alignment, Border, Font, PatternFill, Side
from fastpyxl.utils import get_column_letter
from fastpyxl.workbook.defined_name import DefinedName
from fastpyxl.worksheet.datavalidation import DataValidation

HERE = Path(__file__).parent
OUT_XLSX = HERE / "tiny-dsa.xlsx"
OUT_MANIFEST = HERE / "tiny-dsa-manifest.json"

VERSION = "0.2.0"
RELEASE_DATE_UTC = "2026-05-04T00:00:00Z"  # fixed; bumps only on version increment
EPOCH_FOR_ZIP = (2026, 5, 4, 0, 0, 0)
EPOCH_DATETIME = datetime(2026, 5, 4, 0, 0, 0, tzinfo=UTC)

# Country profile table. Three stylized profiles; only initial debt-to-GDP
# differs across them, all other parameters share the baseline default.
COUNTRY_PROFILES = [
    ("Borvelia", 60.0, "stylized emerging market"),
    ("Litellia", 80.0, "stylized HIPC"),
    ("Aurelium", 40.0, "stylized advanced economy"),
]
DEFAULT_COUNTRY = "Borvelia"

GROWTH_BASELINE = [3.5, 3.5, 3.5, 3.5, 3.5]
INTEREST_BASELINE = [4.0, 4.0, 4.0, 4.0, 4.0]
PRIMARY_BALANCE_BASELINE = [-1.0, -0.5, 0.0, 0.5, 1.0]
SHOCK_YEAR_DEFAULT = 2
SHOCK_TYPE_DEFAULT = 1
SHOCK_MAGNITUDES = (-2.0, 2.0, -1.0)  # growth pp, interest pp, primary balance pp

# --- Styles ---
TITLE_FONT = Font(name="Calibri", size=14, bold=True)
SUBTITLE_FONT = Font(name="Calibri", size=11, italic=True, color="666666")
HEADER_FONT = Font(name="Calibri", size=11, bold=True)
LABEL_FONT = Font(name="Calibri", size=11)
SECTION_FILL = PatternFill("solid", fgColor="DDE5EE")
INPUT_FILL = PatternFill("solid", fgColor="FFF8DC")  # cornsilk for editable inputs
OUTPUT_FILL = PatternFill("solid", fgColor="E0F0E0")  # pale green for outputs
LOOKUP_FILL = PatternFill(
    "solid", fgColor="F0E8FF"
)  # pale violet for lookup-table data
CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
THIN = Side(border_style="thin", color="888888")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _set_column_widths(ws, widths: dict[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def build_inputs_sheet(ws) -> None:
    """All user-editable parameters live here.

    Layout summary:
      Row  4: Country selector header
      Rows 5-6: Country name (input) and initial debt-to-GDP (computed via INDEX/MATCH)
      Row  8: Country profile table header
      Rows 9-12: Profile table (three stylized countries)
      Row 14: Baseline parameters header
      Rows 15-18: Year header + growth + interest + primary balance
      Row 20: Shock configuration header
      Rows 21-22: Shock year and shock type
      Row 24: Shock table header
      Rows 25-26: Shock table column headers + magnitudes
    """
    _set_column_widths(
        ws, {"A": 42, "B": 14, "C": 14, "D": 24, "E": 11, "F": 11, "G": 11}
    )

    ws["A1"] = "TINY-DSA — INPUTS"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:G1")
    ws["A2"] = f"Tiny Debt Dynamics, version {VERSION}. All user-editable parameters."
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells("A2:G2")

    # Country selector
    ws["A4"] = "COUNTRY SELECTOR"
    ws["A4"].font = HEADER_FONT
    ws["A4"].fill = SECTION_FILL
    ws.merge_cells("A4:G4")

    ws["A5"] = "Country name"
    ws["B5"] = DEFAULT_COUNTRY
    ws["B5"].fill = INPUT_FILL
    ws["B5"].border = BOX
    ws["B5"].alignment = LEFT

    ws["A6"] = "Initial debt-to-GDP (% of GDP)"
    # INDEX/MATCH lookup against the profile table.
    ws["B6"] = "=INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2)"
    ws["B6"].fill = OUTPUT_FILL
    ws["B6"].border = BOX
    ws["B6"].number_format = "0.00"
    ws["B6"].alignment = RIGHT

    # Country profile table
    ws["A8"] = "COUNTRY PROFILE TABLE (lookup data)"
    ws["A8"].font = HEADER_FONT
    ws["A8"].fill = SECTION_FILL
    ws.merge_cells("A8:G8")

    ws["A9"] = "Country"
    ws["B9"] = "Initial debt (% of GDP)"
    ws["C9"] = "Description"
    for col in ("A", "B", "C"):
        ws[f"{col}9"].font = HEADER_FONT
        ws[f"{col}9"].alignment = CENTER if col == "B" else LEFT

    for i, (country, debt, desc) in enumerate(COUNTRY_PROFILES, start=10):
        ws[f"A{i}"] = country
        ws[f"A{i}"].fill = LOOKUP_FILL
        ws[f"A{i}"].border = BOX
        ws[f"B{i}"] = debt
        ws[f"B{i}"].fill = LOOKUP_FILL
        ws[f"B{i}"].border = BOX
        ws[f"B{i}"].number_format = "0.00"
        ws[f"B{i}"].alignment = RIGHT
        ws[f"C{i}"] = desc
        ws[f"C{i}"].fill = LOOKUP_FILL
        ws[f"C{i}"].border = BOX
        ws[f"C{i}"].alignment = LEFT

    # Baseline parameters
    ws["A14"] = "BASELINE PARAMETERS"
    ws["A14"].font = HEADER_FONT
    ws["A14"].fill = SECTION_FILL
    ws.merge_cells("A14:G14")

    ws["A15"] = "Year"
    ws["A15"].font = HEADER_FONT
    for k in range(1, 6):
        col = get_column_letter(2 + k)  # C..G
        ws[f"{col}15"] = k
        ws[f"{col}15"].font = HEADER_FONT
        ws[f"{col}15"].alignment = CENTER

    for label, row, values, fmt in (
        ("Real GDP growth (% per annum)", 16, GROWTH_BASELINE, "0.00"),
        ("Real interest rate (% per annum)", 17, INTEREST_BASELINE, "0.00"),
        ("Primary balance (% of GDP)", 18, PRIMARY_BALANCE_BASELINE, "0.00;-0.00"),
    ):
        ws[f"A{row}"] = label
        for k, v in enumerate(values, start=1):
            cell = ws.cell(row=row, column=2 + k, value=v)
            cell.fill = INPUT_FILL
            cell.border = BOX
            cell.alignment = RIGHT
            cell.number_format = fmt

    # Shock configuration
    ws["A20"] = "SHOCK CONFIGURATION"
    ws["A20"].font = HEADER_FONT
    ws["A20"].fill = SECTION_FILL
    ws.merge_cells("A20:G20")

    ws["A21"] = "Shock year (integer, 1 to 5)"
    ws["B21"] = SHOCK_YEAR_DEFAULT
    ws["B21"].fill = INPUT_FILL
    ws["B21"].border = BOX
    ws["B21"].alignment = CENTER

    ws["A22"] = "Shock type (1=growth, 2=interest, 3=primary balance)"
    ws["B22"] = SHOCK_TYPE_DEFAULT
    ws["B22"].fill = INPUT_FILL
    ws["B22"].border = BOX
    ws["B22"].alignment = CENTER

    # Shock table
    ws["A24"] = "SHOCK TABLE (lookup by shock type, in percentage points)"
    ws["A24"].font = HEADER_FONT
    ws["A24"].fill = SECTION_FILL
    ws.merge_cells("A24:G24")

    ws["A25"] = "Shock type"
    ws["B25"] = "Growth (pp)"
    ws["C25"] = "Interest (pp)"
    ws["D25"] = "Primary balance (pp)"
    for col in ("A", "B", "C", "D"):
        ws[f"{col}25"].font = HEADER_FONT
        ws[f"{col}25"].alignment = CENTER if col != "A" else LEFT

    ws["A26"] = "Magnitude"
    ws["A26"].font = LABEL_FONT
    for k, v in enumerate(SHOCK_MAGNITUDES, start=1):
        col = get_column_letter(1 + k)  # B..D
        cell = ws[f"{col}26"]
        cell.value = v
        cell.fill = INPUT_FILL
        cell.border = BOX
        cell.alignment = RIGHT
        cell.number_format = "+0.00;-0.00"


def add_data_validation(ws) -> None:
    """Add Excel-level data validation to the user-editable cells.

    Three constraints:
      - country_name (B5): list from the country column of the profile table
      - shock_year (B21): integer 1..5
      - shock_type (B22): integer 1..3
    """
    dv_country = DataValidation(
        type="list",
        formula1="=$A$10:$A$12",
        allow_blank=False,
        showErrorMessage=True,
        errorTitle="Invalid country",
        error="Country must be one of: Borvelia, Litellia, Aurelium.",
    )
    dv_country.add("B5")
    ws.add_data_validation(dv_country)

    dv_shock_year = DataValidation(
        type="whole",
        operator="between",
        formula1=1,
        formula2=5,
        allow_blank=False,
        showErrorMessage=True,
        errorTitle="Invalid shock year",
        error="Shock year must be an integer between 1 and 5.",
    )
    dv_shock_year.add("B21")
    ws.add_data_validation(dv_shock_year)

    dv_shock_type = DataValidation(
        type="whole",
        operator="between",
        formula1=1,
        formula2=3,
        allow_blank=False,
        showErrorMessage=True,
        errorTitle="Invalid shock type",
        error="Shock type must be 1 (growth), 2 (interest), or 3 (primary balance).",
    )
    dv_shock_type.add("B22")
    ws.add_data_validation(dv_shock_type)


def build_engine_sheet(ws) -> None:
    """Cross-sheet recursion engine.

    Every formula reads from the Inputs sheet. The baseline path uses the
    snowball recursion against the baseline parameter rows. The shocked
    path uses the same recursion against the shocked parameter rows
    constructed in this sheet via OFFSET (for the magnitude lookup) and
    CHOOSE (for parameter dispatch).
    """
    _set_column_widths(
        ws, {"A": 42, "B": 12, "C": 12, "D": 12, "E": 12, "F": 12, "G": 12}
    )

    ws["A1"] = "TINY-DSA — ENGINE"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:G1")
    ws["A2"] = (
        "All references read from the Inputs sheet. Outputs are exposed on the Outputs sheet."
    )
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells("A2:G2")

    # Baseline path
    ws["A4"] = "BASELINE PATH"
    ws["A4"].font = HEADER_FONT
    ws["A4"].fill = SECTION_FILL
    ws.merge_cells("A4:G4")

    ws["A5"] = "Year"
    ws["A5"].font = HEADER_FONT
    ws["B5"] = 0
    ws["B5"].font = HEADER_FONT
    ws["B5"].alignment = CENTER
    for k in range(1, 6):
        col = get_column_letter(2 + k)
        ws[f"{col}5"] = k
        ws[f"{col}5"].font = HEADER_FONT
        ws[f"{col}5"].alignment = CENTER

    ws["A6"] = "Debt-to-GDP (%)"
    ws["B6"] = "=Inputs!B6"  # initial debt, via cross-sheet reference
    ws["B6"].number_format = "0.00"
    ws["B6"].alignment = RIGHT
    for k in range(1, 6):
        col = get_column_letter(2 + k)  # C..G
        prev = get_column_letter(1 + k)  # B..F
        ws[f"{col}6"] = (
            f"={prev}6*(1+Inputs!{col}17/100)/(1+Inputs!{col}16/100)-Inputs!{col}18"
        )
        ws[f"{col}6"].number_format = "0.00"
        ws[f"{col}6"].alignment = RIGHT

    # Shock activation
    ws["A8"] = "SHOCK ACTIVATION"
    ws["A8"].font = HEADER_FONT
    ws["A8"].fill = SECTION_FILL
    ws.merge_cells("A8:G8")

    ws["A9"] = "Shock magnitude (pp), via OFFSET on shock_type"
    ws["B9"] = "=OFFSET(Inputs!$B$26,0,Inputs!$B$22-1)"
    ws["B9"].number_format = "+0.00;-0.00"
    ws["B9"].alignment = RIGHT
    ws["B9"].fill = OUTPUT_FILL
    ws["B9"].border = BOX

    ws["A10"] = "Shock active (1 if year >= shock_year, else 0)"
    for k in range(1, 6):
        col = get_column_letter(2 + k)
        ws[f"{col}10"] = f"=IF({col}5>=Inputs!$B$21,1,0)"
        ws[f"{col}10"].alignment = CENTER

    # Shocked parameters via CHOOSE on shock_type
    ws["A12"] = "SHOCKED PARAMETERS"
    ws["A12"].font = HEADER_FONT
    ws["A12"].fill = SECTION_FILL
    ws.merge_cells("A12:G12")

    ws["A13"] = "Year"
    ws["A13"].font = HEADER_FONT
    for k in range(1, 6):
        col = get_column_letter(2 + k)
        ws[f"{col}13"] = k
        ws[f"{col}13"].font = HEADER_FONT
        ws[f"{col}13"].alignment = CENTER

    for label, row, choose_args, fmt in (
        ("Real GDP growth, shocked (%)", 14, "$B$9,0,0", "0.00"),
        ("Real interest rate, shocked (%)", 15, "0,$B$9,0", "0.00"),
        ("Primary balance, shocked (% of GDP)", 16, "0,0,$B$9", "0.00;-0.00"),
    ):
        ws[f"A{row}"] = label
        # Map row 14 -> Inputs row 16 (growth), 15 -> 17 (interest), 16 -> 18 (PB)
        inputs_row = row + 2
        for k in range(1, 6):
            col = get_column_letter(2 + k)
            ws[f"{col}{row}"] = (
                f"=Inputs!{col}{inputs_row}+CHOOSE(Inputs!$B$22,{choose_args})*{col}10"
            )
            ws[f"{col}{row}"].number_format = fmt
            ws[f"{col}{row}"].alignment = RIGHT

    # Shocked path
    ws["A18"] = "SHOCKED PATH"
    ws["A18"].font = HEADER_FONT
    ws["A18"].fill = SECTION_FILL
    ws.merge_cells("A18:G18")

    ws["A19"] = "Year"
    ws["A19"].font = HEADER_FONT
    ws["B19"] = 0
    ws["B19"].font = HEADER_FONT
    ws["B19"].alignment = CENTER
    for k in range(1, 6):
        col = get_column_letter(2 + k)
        ws[f"{col}19"] = k
        ws[f"{col}19"].font = HEADER_FONT
        ws[f"{col}19"].alignment = CENTER

    ws["A20"] = "Debt-to-GDP (%)"
    ws["B20"] = "=Inputs!B6"  # same initial debt
    ws["B20"].number_format = "0.00"
    ws["B20"].alignment = RIGHT
    for k in range(1, 6):
        col = get_column_letter(2 + k)
        prev = get_column_letter(1 + k)
        ws[f"{col}20"] = f"={prev}20*(1+{col}15/100)/(1+{col}14/100)-{col}16"
        ws[f"{col}20"].number_format = "0.00"
        ws[f"{col}20"].alignment = RIGHT


def build_outputs_sheet(ws) -> None:
    """Outputs surface for downstream consumers.

    Reads from the Engine sheet. Provides a clean API: a stable layout
    with named ranges for baseline path, shocked path, and delta.
    """
    _set_column_widths(
        ws, {"A": 38, "B": 12, "C": 12, "D": 12, "E": 12, "F": 12, "G": 12}
    )

    ws["A1"] = "TINY-DSA — OUTPUTS"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:G1")
    ws["A2"] = "Comparison: baseline vs shocked debt-to-GDP trajectory."
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells("A2:G2")

    ws["A4"] = "SUMMARY"
    ws["A4"].font = HEADER_FONT
    ws["A4"].fill = SECTION_FILL
    ws.merge_cells("A4:G4")

    ws["A5"] = "Country"
    ws["B5"] = "=Inputs!B5"
    ws["B5"].alignment = LEFT

    ws["A6"] = "Initial debt (% of GDP)"
    ws["B6"] = "=Inputs!B6"
    ws["B6"].number_format = "0.00"
    ws["B6"].alignment = RIGHT

    ws["A7"] = "Shock year"
    ws["B7"] = "=Inputs!B21"
    ws["B7"].alignment = CENTER

    ws["A8"] = "Shock type (1=growth, 2=interest, 3=primary balance)"
    ws["B8"] = "=Inputs!B22"
    ws["B8"].alignment = CENTER

    ws["A10"] = "DEBT-TO-GDP TRAJECTORY"
    ws["A10"].font = HEADER_FONT
    ws["A10"].fill = SECTION_FILL
    ws.merge_cells("A10:G10")

    ws["A11"] = "Year"
    ws["A11"].font = HEADER_FONT
    for k in range(1, 6):
        col = get_column_letter(1 + k)  # B..F
        ws[f"{col}11"] = k
        ws[f"{col}11"].font = HEADER_FONT
        ws[f"{col}11"].alignment = CENTER

    ws["A12"] = "Baseline (% of GDP)"
    for k in range(1, 6):
        col = get_column_letter(1 + k)  # B..F (output cols)
        engine_col = get_column_letter(2 + k)  # C..G (engine cols)
        ws[f"{col}12"] = f"=Engine!{engine_col}6"
        ws[f"{col}12"].number_format = "0.00"
        ws[f"{col}12"].alignment = RIGHT
        ws[f"{col}12"].fill = OUTPUT_FILL

    ws["A13"] = "Shocked (% of GDP)"
    for k in range(1, 6):
        col = get_column_letter(1 + k)
        engine_col = get_column_letter(2 + k)
        ws[f"{col}13"] = f"=Engine!{engine_col}20"
        ws[f"{col}13"].number_format = "0.00"
        ws[f"{col}13"].alignment = RIGHT
        ws[f"{col}13"].fill = OUTPUT_FILL

    ws["A14"] = "Delta (shocked − baseline, pp)"
    for k in range(1, 6):
        col = get_column_letter(1 + k)
        ws[f"{col}14"] = f"={col}13-{col}12"
        ws[f"{col}14"].number_format = "+0.00;-0.00"
        ws[f"{col}14"].alignment = RIGHT
        ws[f"{col}14"].fill = OUTPUT_FILL


def define_named_ranges(wb: Workbook) -> None:
    """Workbook-scoped named ranges constituting the public API."""
    ranges: list[tuple[str, str]] = [
        ("country_name", "Inputs!$B$5"),
        ("initial_debt_to_gdp", "Inputs!$B$6"),
        ("country_profile_table", "Inputs!$A$10:$C$12"),
        ("growth_baseline", "Inputs!$C$16:$G$16"),
        ("interest_baseline", "Inputs!$C$17:$G$17"),
        ("primary_balance_baseline", "Inputs!$C$18:$G$18"),
        ("shock_year", "Inputs!$B$21"),
        ("shock_type", "Inputs!$B$22"),
        ("shock_table", "Inputs!$B$26:$D$26"),
        ("shock_magnitude_resolved", "Engine!$B$9"),
        ("baseline_path", "Engine!$C$6:$G$6"),
        ("shocked_path", "Engine!$C$20:$G$20"),
        ("output_baseline", "Outputs!$B$12:$F$12"),
        ("output_shocked", "Outputs!$B$13:$F$13"),
        ("output_delta", "Outputs!$B$14:$F$14"),
    ]
    for name, ref in ranges:
        wb.defined_names[name] = DefinedName(name=name, attr_text=ref)


def build_workbook() -> Workbook:
    wb = Workbook()
    # Stable workbook properties for byte-deterministic output.
    wb.properties.creator = "Teal Insights"
    wb.properties.lastModifiedBy = "Teal Insights"
    wb.properties.created = EPOCH_DATETIME.replace(tzinfo=None)
    wb.properties.modified = EPOCH_DATETIME.replace(tzinfo=None)
    wb.properties.title = f"Tiny-DSA v{VERSION}"

    # Replace the default sheet with three named sheets.
    default_ws = wb.active
    if default_ws is not None:
        wb.remove(default_ws)
    inputs_ws = wb.create_sheet("Inputs")
    engine_ws = wb.create_sheet("Engine")
    outputs_ws = wb.create_sheet("Outputs")

    build_inputs_sheet(inputs_ws)
    add_data_validation(inputs_ws)
    build_engine_sheet(engine_ws)
    build_outputs_sheet(outputs_ws)
    define_named_ranges(wb)

    return wb


def make_xlsx_byte_deterministic(path: Path) -> None:
    """Rewrite the .xlsx so two builds of the same logical workbook produce
    byte-identical files.

    Two non-deterministic sources need normalization:

    1. ZIP entry timestamps. fastpyxl writes the current time into each
       entry's creation timestamp; we reset them all to a fixed value.
    2. ``dcterms:modified`` in ``docProps/core.xml``. fastpyxl overwrites
       this on save with ``datetime.utcnow()`` regardless of what is set on
       ``wb.properties.modified``. We replace the value with the fixed
       release date.
    """
    import re

    fixed_iso = EPOCH_DATETIME.strftime("%Y-%m-%dT%H:%M:%SZ")

    with path.open("rb") as f:
        original_bytes = f.read()
    src_buf = io.BytesIO(original_bytes)
    out_buf = io.BytesIO()
    with (
        zipfile.ZipFile(src_buf, "r") as src,
        zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out,
    ):
        for info in sorted(src.infolist(), key=lambda i: i.filename):
            content = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                text = content.decode("utf-8")
                # Replace any modified timestamp with the fixed value.
                text = re.sub(
                    r"(<dcterms:modified[^>]*>)[^<]+(</dcterms:modified>)",
                    rf"\g<1>{fixed_iso}\g<2>",
                    text,
                )
                # Same for created, in case fastpyxl ever overrides it.
                text = re.sub(
                    r"(<dcterms:created[^>]*>)[^<]+(</dcterms:created>)",
                    rf"\g<1>{fixed_iso}\g<2>",
                    text,
                )
                content = text.encode("utf-8")
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=EPOCH_FOR_ZIP)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            new_info.create_system = info.create_system
            out.writestr(new_info, content)
    with path.open("wb") as f:
        f.write(out_buf.getvalue())


def _compute_path(
    initial_debt: float,
    growth: list[float],
    interest: list[float],
    pb: list[float],
) -> list[float]:
    """Apply the snowball recursion year-by-year for 5 periods."""
    path: list[float] = []
    d = initial_debt
    for t in range(5):
        d = d * (1 + interest[t] / 100) / (1 + growth[t] / 100) - pb[t]
        path.append(round(d, 6))
    return path


def _shocked_parameters(
    growth: list[float],
    interest: list[float],
    pb: list[float],
    shock_year: int,
    shock_type: int,
    shock_magnitudes: tuple[float, float, float],
) -> tuple[list[float], list[float], list[float]]:
    g = list(growth)
    r = list(interest)
    p = list(pb)
    mag = shock_magnitudes[shock_type - 1]
    for t in range(5):
        active = 1 if (t + 1) >= shock_year else 0
        if shock_type == 1:
            g[t] = g[t] + mag * active
        elif shock_type == 2:
            r[t] = r[t] + mag * active
        elif shock_type == 3:
            p[t] = p[t] + mag * active
    return g, r, p


def compute_scenario(
    country: str,
    shock_type: int,
    shock_year: int = SHOCK_YEAR_DEFAULT,
) -> dict:
    """Compute a single golden-master scenario."""
    initial_debt = next(d for c, d, _ in COUNTRY_PROFILES if c == country)
    g_base = list(GROWTH_BASELINE)
    r_base = list(INTEREST_BASELINE)
    pb_base = list(PRIMARY_BALANCE_BASELINE)
    baseline = _compute_path(initial_debt, g_base, r_base, pb_base)
    g_s, r_s, pb_s = _shocked_parameters(
        g_base, r_base, pb_base, shock_year, shock_type, SHOCK_MAGNITUDES
    )
    shocked = _compute_path(initial_debt, g_s, r_s, pb_s)
    delta = [round(s - b, 6) for s, b in zip(shocked, baseline)]
    shock_type_label = {1: "growth", 2: "interest", 3: "primary_balance"}[shock_type]
    return {
        "scenario_id": f"{country.lower()}_{shock_type_label}_shock",
        "inputs": {
            "country_name": country,
            "initial_debt_to_gdp": initial_debt,
            "growth_baseline": g_base,
            "interest_baseline": r_base,
            "primary_balance_baseline": pb_base,
            "shock_year": shock_year,
            "shock_type": shock_type,
            "shock_type_label": shock_type_label,
            "shock_magnitude_pp": SHOCK_MAGNITUDES[shock_type - 1],
        },
        "outputs": {
            "baseline_debt_to_gdp": baseline,
            "shocked_debt_to_gdp": shocked,
            "delta_pp": delta,
        },
    }


def compute_all_scenarios() -> list[dict]:
    """Three canonical scenarios, one per CHOOSE branch."""
    return [
        compute_scenario(DEFAULT_COUNTRY, shock_type=1),
        compute_scenario(DEFAULT_COUNTRY, shock_type=2),
        compute_scenario(DEFAULT_COUNTRY, shock_type=3),
    ]


def schemas() -> dict:
    """Type and unit information per named range.

    A starting point for Christopher's schemas.py; deliberately lightweight
    and library-agnostic. Pydantic / Pandera types can be derived from this.
    """
    return {
        "country_name": {
            "kind": "scalar",
            "type": "string",
            "domain": "values from country_profile_table[Country]",
            "user_editable": True,
        },
        "initial_debt_to_gdp": {
            "kind": "scalar",
            "type": "float64",
            "units": "percent of GDP",
            "computed": True,
            "computed_via": "INDEX/MATCH against country_profile_table",
            "valid_range_inclusive": [0.0, 200.0],
        },
        "country_profile_table": {
            "kind": "table",
            "rows": 3,
            "columns": [
                "Country (string)",
                "Initial debt (float64, % of GDP)",
                "Description (string)",
            ],
            "user_editable": False,
        },
        "growth_baseline": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent per annum (real)",
            "valid_range_inclusive": [-10.0, 15.0],
            "user_editable": True,
        },
        "interest_baseline": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent per annum (real)",
            "valid_range_inclusive": [0.0, 20.0],
            "user_editable": True,
        },
        "primary_balance_baseline": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent of GDP",
            "valid_range_inclusive": [-15.0, 15.0],
            "user_editable": True,
        },
        "shock_year": {
            "kind": "scalar",
            "type": "int",
            "valid_range_inclusive": [1, 5],
            "user_editable": True,
        },
        "shock_type": {
            "kind": "scalar",
            "type": "int",
            "valid_range_inclusive": [1, 3],
            "categorical_labels": {
                "1": "growth",
                "2": "interest",
                "3": "primary_balance",
            },
            "user_editable": True,
        },
        "shock_table": {
            "kind": "array",
            "type": "float64",
            "length": 3,
            "units": "percentage points",
            "ordered_by": ["growth", "interest", "primary_balance"],
            "user_editable": True,
        },
        "shock_magnitude_resolved": {
            "kind": "scalar",
            "type": "float64",
            "units": "percentage points",
            "computed": True,
            "computed_via": "OFFSET against shock_table on shock_type",
        },
        "baseline_path": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent of GDP",
            "computed": True,
        },
        "shocked_path": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent of GDP",
            "computed": True,
        },
        "output_baseline": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent of GDP",
            "computed": True,
            "alias_of": "baseline_path",
        },
        "output_shocked": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percent of GDP",
            "computed": True,
            "alias_of": "shocked_path",
        },
        "output_delta": {
            "kind": "array",
            "type": "float64",
            "length": 5,
            "units": "percentage points",
            "computed": True,
        },
    }


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    wb = build_workbook()
    wb.save(OUT_XLSX)
    make_xlsx_byte_deterministic(OUT_XLSX)

    scenarios = compute_all_scenarios()

    manifest = {
        "tool": "tiny-dsa",
        "version": VERSION,
        "release_date_utc": RELEASE_DATE_UTC,
        "build_script": "build_tiny_dsa.py",
        "purpose": (
            "Smaller-than-Q-CRAFT toy that exercises time recursion, stress-test "
            "branching, INDEX/MATCH country lookup, OFFSET against a dynamic column, "
            "CHOOSE for categorical dispatch, and cross-sheet references. Bridge "
            "between excel-grapher's micro-workbook (mechanics) and Q-CRAFT v2 "
            "(the first real integration). Cookie-cutter v0.1 input bundle prototype."
        ),
        "model": {
            "equation": (
                "debt(t) = debt(t-1) * (1 + r(t)) / (1 + g(t)) - primary_balance(t)"
            ),
            "recursion": "additive percentage-point shock applied from shock_year onwards to one selected parameter",
            "horizon_years": 5,
        },
        "patterns_exercised": [
            "time recursion (debt(t) depends on debt(t-1))",
            "stress-test branching (parallel baseline vs shocked paths)",
            "cross-sheet references (Engine reads from Inputs; Outputs reads from Engine)",
            "INDEX/MATCH against country_profile_table to look up initial_debt_to_gdp",
            "CHOOSE on shock_type (which variable receives the shock)",
            "OFFSET on shock_type (dynamic-column lookup of shock magnitude)",
            "Excel-logic vs economic-logic translation (snowball factor)",
        ],
        "out_of_scope": [
            "merged cells, hard-pasted-over formulas, orphan cells",
            "iterative or quasi-iterative calculation",
            "non-linear functional forms (e.g. logistic productivity convergence)",
            "time-window aggregations across historical periods",
            "many-to-one selection from large preloaded datasets (Q-CRAFT-scale)",
            "long-horizon compounding (5 years vs Q-CRAFT's 75)",
            "indicator-and-threshold output and risk classification",
        ],
        "files": {
            "tiny-dsa.xlsx": {
                "sha256": sha256_of(OUT_XLSX),
                "size_bytes": OUT_XLSX.stat().st_size,
            },
        },
        "schemas": schemas(),
        "golden_master_scenarios": scenarios,
    }

    OUT_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {OUT_XLSX}")
    print(f"Wrote {OUT_MANIFEST}")
    print(f"SHA-256: {manifest['files']['tiny-dsa.xlsx']['sha256']}")
    print(f"Scenarios: {len(scenarios)}")
    for s in scenarios:
        outs = s["outputs"]
        print(
            f"  {s['scenario_id']:40s}  "
            f"baseline_y5={outs['baseline_debt_to_gdp'][-1]:.2f}  "
            f"shocked_y5={outs['shocked_debt_to_gdp'][-1]:.2f}  "
            f"delta_y5={outs['delta_pp'][-1]:+.2f}"
        )


if __name__ == "__main__":
    main()
