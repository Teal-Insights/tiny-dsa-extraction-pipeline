"""Standalone runtime for generated Excel formula code."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias, cast

import fastpyxl.utils.cell
import numpy as np

class CircularReferenceWarning(RuntimeWarning):
    """Warning emitted when a circular reference is encountered (default Excel mode)."""

@dataclass(slots=True)
class EvalContext:
    """Per-run evaluation state for generated spreadsheets.

    The exported-code path needs a mutable inputs mapping and a cache that is scoped
    to a single compute call, so callers can run many scenarios without global state.
    """

    inputs: dict[str, CellValue]
    resolver: Callable[[str], Callable[[EvalContext], CellValue] | None]
    cache: dict[str, CellValue] = field(default_factory=dict)
    computing: set[str] = field(default_factory=set)
    deps: dict[str, set[str]] = field(default_factory=dict)
    reverse_deps: dict[str, set[str]] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    iterative_enabled: bool = False
    iterate_count: int = 100
    iterate_delta: float = 0.001
    iteration_values: dict[str, CellValue] = field(default_factory=dict)

    def _record_dependency(self, parent: str, child: str) -> None:
        if parent == child:
            return
        self.deps.setdefault(parent, set()).add(child)
        self.reverse_deps.setdefault(child, set()).add(parent)

    def invalidate(self, addresses: Iterable[str]) -> None:
        """Invalidate cached values for the given addresses and their dependents."""
        to_visit = list(addresses)
        seen: set[str] = set()
        while to_visit:
            addr = to_visit.pop()
            if addr in seen:
                continue
            seen.add(addr)

            self.cache.pop(addr, None)
            self.computing.discard(addr)

            dependents = list(self.reverse_deps.get(addr, set()))
            to_visit.extend(dependents)

            for dep in self.deps.get(addr, set()):
                parents = self.reverse_deps.get(dep)
                if parents is not None:
                    parents.discard(addr)
                    if not parents:
                        self.reverse_deps.pop(dep, None)

            self.deps.pop(addr, None)
            self.reverse_deps.pop(addr, None)

    def set_inputs(self, inputs: dict[str, CellValue]) -> None:
        """Update input values and invalidate dependent cached results."""
        changed = [k for k, v in inputs.items() if self.inputs.get(k) != v]
        self.inputs.update(inputs)
        if changed:
            self.invalidate(changed)

NormalizedAddress: TypeAlias = str

class XlError(StrEnum):
    VALUE = "#VALUE!"
    REF = "#REF!"
    DIV = "#DIV/0!"
    NA = "#N/A"
    NAME = "#NAME?"
    NUM = "#NUM!"
    NULL = "#NULL!"

    @classmethod
    def from_text(cls, value: str) -> XlError | None:
        upper = value.strip().upper()
        for err in cls:
            if err.value == upper:
                return err
        return None

def _escape_sheet_for_formula(sheet: str) -> str:
    """Escape apostrophes for use inside quoted sheet names."""
    return sheet.replace("'", "''")

def _format_general_number(value: float | int) -> str:
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return str(f)

def _quote_sheet_if_needed(sheet: str) -> str:
    if " " in sheet or "-" in sheet or "'" in sheet:
        return f"'{sheet}'"
    return sheet

def _format_address(sheet: str, row: int, col: int) -> str:
    sheet_name = _quote_sheet_if_needed(sheet)
    col_letter = fastpyxl.utils.cell.get_column_letter(col)
    return f"{sheet_name}!{col_letter}{row}"

def excel_casefold(value: str) -> str:
    return value.casefold()

def needs_quoting(sheet: str) -> bool:
    """Return True if a sheet name must be wrapped in single quotes in a formula."""
    return " " in sheet or "-" in sheet or "'" in sheet

def quote_sheet_if_needed(sheet: str) -> str:
    """Return a sheet name quoted for formulas when quoting is required."""
    if not needs_quoting(sheet):
        return sheet
    return "'" + _escape_sheet_for_formula(sheet) + "'"

def format_key(sheet: str, cell: str) -> NormalizedAddress:
    """Format a sheet and A1 cell coordinate into a canonical address string."""
    return f"{quote_sheet_if_needed(sheet)}!{cell}"

@dataclass(frozen=True, slots=True)
class ExcelRange:
    sheet: str
    start_row: int
    start_col: int
    end_row: int
    end_col: int

    @property
    def shape(self) -> tuple[int, int]:
        return (self.end_row - self.start_row + 1, self.end_col - self.start_col + 1)

    def cell_addresses(self) -> Iterator[str]:
        for r in range(self.start_row, self.end_row + 1):
            for c in range(self.start_col, self.end_col + 1):
                col = fastpyxl.utils.cell.get_column_letter(c)
                yield format_key(self.sheet, f"{col}{r}")

    def resolve(self, evaluate_fn: Callable[[str], CellValue]) -> np.ndarray:
        values: list[CellValue] = [evaluate_fn(addr) for addr in self.cell_addresses()]
        rows, cols = self.shape
        return np.array(values, dtype=object).reshape((rows, cols))

CellValue: TypeAlias = float | int | str | bool | XlError | ExcelRange | np.ndarray | None

def coerce_inputs_dict(values: Mapping[str, object]) -> dict[str, CellValue]:
    """Widen inferred default-input dicts to ``dict[str, CellValue]`` for :class:`EvalContext`."""
    return cast(dict[str, CellValue], dict(values))

def split_sheet_qualified_address(address: str) -> tuple[str, str] | None:
    """Split ``sheet!coord`` into ``(sheet_name, coord)``.

    Handles quoted sheet names, including Excel's doubled-single-quote escape
    (``'O''Neil'!A1`` → sheet ``O'Neil``).

    Returns ``None`` when *address* has no sheet qualifier (plain ``A1``).
    """
    if address.startswith("'"):
        i = 1
        while i < len(address):
            if address[i] == "'":
                if i + 1 < len(address) and address[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        if i >= len(address):
            return None
        sheet = address[1:i].replace("''", "'")
        rest = address[i + 1 :]
        if not rest.startswith("!"):
            return None
        return sheet, rest[1:]

    if "!" not in address:
        return None
    sheet, cell = address.rsplit("!", 1)
    return sheet, cell

def _parse_sheet_address(address: str) -> tuple[str, str] | None:
    return split_sheet_qualified_address(address)

def _parse_range_address(address: str) -> tuple[str, str, str] | XlError:
    if ":" not in address:
        return XlError.VALUE
    start_text, end_text = address.split(":", 1)
    start = _parse_sheet_address(start_text)
    if start is None:
        return XlError.VALUE
    sheet, start_cell = start
    if "!" in end_text:
        end = _parse_sheet_address(end_text)
        if end is None:
            return XlError.VALUE
        end_sheet, end_cell = end
        if end_sheet != sheet:
            return XlError.VALUE
    else:
        end_cell = end_text
    return sheet, start_cell, end_cell

def to_bool(value: CellValue) -> bool | XlError:
    if value is None:
        return False
    if isinstance(value, XlError):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    if isinstance(value, str):
        s = value.strip().upper()
        if s == "":
            return False
        if s == "TRUE":
            return True
        if s == "FALSE":
            return False
        return XlError.VALUE
    if isinstance(value, ExcelRange):
        return XlError.VALUE
    return XlError.VALUE

def to_number(value: CellValue) -> float | XlError:
    if value is None:
        return 0.0
    if isinstance(value, XlError):
        return value
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return XlError.VALUE
    if isinstance(value, ExcelRange):
        return XlError.VALUE
    return XlError.VALUE

def _compare_values(a: CellValue, b: CellValue) -> int:
    an = to_number(a)
    bn = to_number(b)
    if not isinstance(an, XlError) and not isinstance(bn, XlError):
        return -1 if an < bn else 1 if an > bn else 0
    if isinstance(a, str) and isinstance(b, str):
        af = excel_casefold(a)
        bf = excel_casefold(b)
        return -1 if af < bf else 1 if af > bf else 0
    return 0

def _values_match(a: CellValue, b: CellValue) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return excel_casefold(a) == excel_casefold(b)
    an = to_number(a)
    bn = to_number(b)
    if not isinstance(an, XlError) and not isinstance(bn, XlError):
        return an == bn
    return a == b

def index_excel_range(
    base: ExcelRange,
    row_num: CellValue | None,
    col_num: CellValue | None,
) -> ExcelRange | XlError:
    """Map INDEX(row,col) over *base* to an absolute range (single cell or slice).

    Mirrors :func:`excel_grapher.runtime.lookup.xl_index` geometry
    so OFFSET(INDEX(...), ...) receives a true cell reference.
    """
    nrows = base.end_row - base.start_row + 1
    ncols = base.end_col - base.start_col + 1
    row_omitted = row_num is None
    col_omitted = col_num is None

    def abs_cell(r0: int, c0: int) -> ExcelRange:
        r = base.start_row + r0
        c = base.start_col + c0
        return ExcelRange(base.sheet, r, c, r, c)

    if row_omitted and col_omitted:
        if nrows == 1 and ncols == 1:
            return abs_cell(0, 0)
        if nrows == 1:
            return abs_cell(0, ncols - 1)
        if ncols == 1:
            return abs_cell(nrows - 1, 0)
        return XlError.VALUE

    if row_omitted:
        cn = to_number(col_num)
        if isinstance(cn, XlError):
            return cn
        col = int(cn)
        if col < 1 or col > ncols:
            return XlError.REF
        if nrows == 1:
            return abs_cell(0, col - 1)
        c0 = base.start_col + col - 1
        return ExcelRange(base.sheet, base.start_row, c0, base.end_row, c0)

    rn = to_number(row_num)
    if isinstance(rn, XlError):
        return rn
    row = int(rn)

    if col_omitted:
        if nrows == 1:
            if row < 1 or row > ncols:
                return XlError.REF
            return abs_cell(0, row - 1)
        if ncols == 1:
            if row < 1 or row > nrows:
                return XlError.REF
            return abs_cell(row - 1, 0)
        if row < 1 or row > nrows:
            return XlError.REF
        r0 = base.start_row + row - 1
        return ExcelRange(base.sheet, r0, base.start_col, r0, base.end_col)

    cn = to_number(col_num)
    if isinstance(cn, XlError):
        return cn
    col = int(cn)
    if nrows == 1:
        if row < 1 or row > ncols:
            return XlError.REF
        return abs_cell(0, row - 1)
    if ncols == 1:
        if row < 1 or row > nrows:
            return XlError.REF
        return abs_cell(row - 1, 0)
    if row < 1 or row > nrows:
        return XlError.REF
    if col < 1 or col > ncols:
        return XlError.REF
    return abs_cell(row - 1, col - 1)

def to_int(value: CellValue) -> int | XlError:
    """Coerce a CellValue to an integer using Excel-style numeric coercion.

    For functions that operate on integer indices (e.g. CHOOSE/INDEX/MATCH)
    while propagating Excel errors.
    """
    n = to_number(value)
    if isinstance(n, XlError):
        return n
    return int(n)

def to_string(value: CellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, XlError):
        return value.value
    if isinstance(value, (int, float)):
        return _format_general_number(float(value))
    if isinstance(value, ExcelRange):
        return XlError.VALUE.value
    return str(value)

def _xl_compare(op: str, left: CellValue, right: CellValue) -> bool | XlError:
    if isinstance(left, XlError):
        return left
    if isinstance(right, XlError):
        return right

    def _cmp_str(a: str, b: str) -> bool:
        if op == "=":
            return a == b
        if op == "<>":
            return a != b
        if op == "<":
            return a < b
        if op == ">":
            return a > b
        if op == "<=":
            return a <= b
        if op == ">=":
            return a >= b
        raise ValueError(f"Unknown comparison operator: {op}")

    def _cmp_float(a: float, b: float) -> bool:
        if op == "=":
            return a == b
        if op == "<>":
            return a != b
        if op == "<":
            return a < b
        if op == ">":
            return a > b
        if op == "<=":
            return a <= b
        if op == ">=":
            return a >= b
        raise ValueError(f"Unknown comparison operator: {op}")

    if isinstance(left, str) and isinstance(right, str):
        return _cmp_str(excel_casefold(left), excel_casefold(right))

    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError) or isinstance(rn, XlError):
        return _cmp_str(excel_casefold(to_string(left)), excel_casefold(to_string(right)))

    return _cmp_float(float(ln), float(rn))

def xl_add(left: CellValue, right: CellValue) -> float | XlError:
    if isinstance(left, XlError):
        return left
    if isinstance(right, XlError):
        return right
    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError):
        return ln
    if isinstance(rn, XlError):
        return rn
    return ln + rn

def xl_circular_reference() -> CellValue:
    """Excel default behavior for circular references (non-iterative calculation)."""
    warnings.warn(
        "Circular reference detected; returning 0 (iterative calculation is disabled).",
        CircularReferenceWarning,
        stacklevel=2,
    )
    return 0

def xl_cell(ctx: EvalContext, address: str) -> CellValue:
    """Evaluate a single cell address under the given context.

    Resolution order:
    - cached value (per ctx)
    - user-provided inputs
    - exported formula implementation (via resolver)
    - missing cell raises KeyError
    """
    if ctx.stack:
        ctx._record_dependency(ctx.stack[-1], address)

    if address in ctx.cache:
        return ctx.cache[address]

    if address in ctx.computing:
        if ctx.iterative_enabled:
            return ctx.iteration_values.get(address, 0)
        return xl_circular_reference()

    if address in ctx.inputs:
        v = ctx.inputs[address]
        ctx.cache[address] = v
        return v

    fn = ctx.resolver(address)
    if fn is None:
        raise KeyError(f"Cell {address} not found in graph")

    ctx.computing.add(address)
    ctx.stack.append(address)
    try:
        v = fn(ctx)
        # Excel treats "empty" formula results as 0 in most numeric contexts; the evaluator
        # normalizes those Nones to 0. Structural blank-range cells intentionally stay None
        # so INDEX/MATCH (and similar) see true empty cells in object arrays.
        if v is None and not getattr(fn, "__structural_blank__", False):
            v = 0
        ctx.cache[address] = v
        return v
    finally:
        ctx.computing.discard(address)
        if ctx.stack and ctx.stack[-1] == address:
            ctx.stack.pop()

def xl_div(left: CellValue, right: CellValue) -> float | XlError:
    if isinstance(left, XlError):
        return left
    if isinstance(right, XlError):
        return right
    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError):
        return ln
    if isinstance(rn, XlError):
        return rn
    if rn == 0:
        return XlError.DIV
    return ln / rn

def xl_eval(
    ctx: EvalContext,
    address: str,
    fn: Callable[[EvalContext], CellValue],
) -> CellValue:
    """Evaluate a known formula implementation under the given context."""
    if ctx.stack:
        ctx._record_dependency(ctx.stack[-1], address)

    if address in ctx.cache:
        return ctx.cache[address]

    if address in ctx.computing:
        if ctx.iterative_enabled:
            return ctx.iteration_values.get(address, 0)
        return xl_circular_reference()

    if address in ctx.inputs:
        v = ctx.inputs[address]
        ctx.cache[address] = v
        return v

    ctx.computing.add(address)
    ctx.stack.append(address)
    try:
        v = fn(ctx)
        if v is None:
            v = 0
        ctx.cache[address] = v
        return v
    finally:
        ctx.computing.discard(address)
        if ctx.stack and ctx.stack[-1] == address:
            ctx.stack.pop()

def xl_ge(left: CellValue, right: CellValue) -> bool | XlError:
    return _xl_compare(">=", left, right)

def xl_index_ref(
    ref: ExcelRange | tuple[str, int, int] | tuple[str, int, int, int, int],
    row_num: CellValue | None,
    col_num: CellValue | None,
) -> ExcelRange | tuple[str, int, int] | tuple[str, int, int, int, int] | XlError:
    """INDEX semantics that return a reference suitable for OFFSET."""
    if isinstance(ref, ExcelRange):
        base = ref
    else:
        match ref:
            case (sheet, r1, c1):
                base = ExcelRange(sheet=sheet, start_row=r1, start_col=c1, end_row=r1, end_col=c1)
            case (sheet, r1, c1, r2, c2):
                base = ExcelRange(sheet=sheet, start_row=r1, start_col=c1, end_row=r2, end_col=c2)
            case _:
                return XlError.VALUE

    out = index_excel_range(base, row_num, col_num)
    if isinstance(out, XlError):
        return out
    if out.start_row == out.end_row and out.start_col == out.end_col:
        return (out.sheet, out.start_row, out.start_col)
    return (out.sheet, out.start_row, out.start_col, out.end_row, out.end_col)

def xl_match(
    lookup_value: CellValue, lookup_array: CellValue, match_type: CellValue = 1
) -> int | XlError:
    mt = to_number(match_type)
    if isinstance(mt, XlError):
        return mt
    match_type_int = int(mt)
    if isinstance(lookup_array, XlError):
        return lookup_array
    if isinstance(lookup_array, np.ndarray):
        flat = np.ravel(lookup_array)
    elif isinstance(lookup_array, (list, tuple)):
        flat = np.ravel(np.array(lookup_array, dtype=object))
    else:
        flat = np.array([lookup_array], dtype=object)
    if match_type_int == 0:
        for i, val in enumerate(flat):
            if _values_match(lookup_value, val):
                return i + 1
        return XlError.NA
    if match_type_int == 1:
        last_match = None
        for i, val in enumerate(flat):
            if _compare_values(val, lookup_value) <= 0:
                last_match = i + 1
            else:
                break
        return XlError.NA if last_match is None else last_match
    if match_type_int == -1:
        last_match = None
        for i, val in enumerate(flat):
            if _compare_values(val, lookup_value) >= 0:
                last_match = i + 1
            else:
                break
        return XlError.NA if last_match is None else last_match
    return XlError.VALUE

def xl_mul(left: CellValue, right: CellValue) -> float | XlError:
    if isinstance(left, XlError):
        return left
    if isinstance(right, XlError):
        return right
    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError):
        return ln
    if isinstance(rn, XlError):
        return rn
    return ln * rn

def xl_offset(
    ctx: EvalContext,
    ref_info: tuple[str, int, int] | tuple[str, int, int, int, int] | XlError,
    rows: CellValue,
    cols: CellValue,
    height: CellValue | None = None,
    width: CellValue | None = None,
) -> CellValue:
    rr = to_number(rows)
    if isinstance(rr, XlError):
        return rr
    cc = to_number(cols)
    if isinstance(cc, XlError):
        return cc

    if isinstance(ref_info, XlError):
        return ref_info

    match ref_info:
        case (sheet, base_row, base_col):
            base_end_row, base_end_col = base_row, base_col
        case (sheet, base_row, base_col, base_end_row, base_end_col):
            pass
        case _:
            return XlError.VALUE

    base_h = int(base_end_row - base_row + 1)
    base_w = int(base_end_col - base_col + 1)

    if height is None:
        h = base_h
    else:
        hh = to_number(height)
        if isinstance(hh, XlError):
            return hh
        h = int(hh)

    if width is None:
        w = base_w
    else:
        ww = to_number(width)
        if isinstance(ww, XlError):
            return ww
        w = int(ww)

    target_row = int(base_row + int(rr))
    target_col = int(base_col + int(cc))

    if target_row < 1 or target_col < 1:
        return XlError.REF
    if h <= 0 or w <= 0:
        return XlError.VALUE

    if h == 1 and w == 1:
        addr = _format_address(sheet, target_row, target_col)
        return xl_cell(ctx, addr)

    result: list[list[CellValue]] = []
    for r in range(target_row, target_row + h):
        row_values: list[CellValue] = []
        for c in range(target_col, target_col + w):
            addr = _format_address(sheet, r, c)
            row_values.append(xl_cell(ctx, addr))
        result.append(row_values)
    return np.array(result, dtype=object)

def xl_range(ctx: EvalContext, address: str) -> CellValue:
    """Evaluate a sheet-qualified range and return a 2D numpy array of values."""
    parsed = _parse_range_address(address)
    if isinstance(parsed, XlError):
        return parsed
    sheet, start_cell, end_cell = parsed
    try:
        start_col, start_row = fastpyxl.utils.cell.coordinate_from_string(start_cell)
        end_col, end_row = fastpyxl.utils.cell.coordinate_from_string(end_cell)
        start_col_idx = fastpyxl.utils.cell.column_index_from_string(start_col)
        end_col_idx = fastpyxl.utils.cell.column_index_from_string(end_col)
    except ValueError:
        return XlError.VALUE

    if start_row > end_row:
        start_row, end_row = end_row, start_row
    if start_col_idx > end_col_idx:
        start_col_idx, end_col_idx = end_col_idx, start_col_idx

    rng = ExcelRange(sheet, start_row, start_col_idx, end_row, end_col_idx)
    return rng.resolve(lambda addr: xl_cell(ctx, addr))

def xl_sub(left: CellValue, right: CellValue) -> float | XlError:
    if isinstance(left, XlError):
        return left
    if isinstance(right, XlError):
        return right
    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError):
        return ln
    if isinstance(rn, XlError):
        return rn
    return ln - rn

# --- Default inputs (leaf cells) ---
DEFAULT_INPUTS = {
    'Inputs!B10': 60,
    'Inputs!B11': 80,
    'Inputs!B12': 40,
    'Inputs!B21': 2,
    'Inputs!B22': 1,
    'Inputs!B26': -2,
    'Inputs!B5': 'Borvelia',
    'Inputs!C16': 3.5,
    'Inputs!C17': 4,
    'Inputs!C18': -1,
    'Inputs!C26': 2,
    'Inputs!D16': 3.5,
    'Inputs!D17': 4,
    'Inputs!D18': -0.5,
    'Inputs!D26': -1,
    'Inputs!E16': 3.5,
    'Inputs!E17': 4,
    'Inputs!E18': 0,
    'Inputs!F16': 3.5,
    'Inputs!F17': 4,
    'Inputs!F18': 0.5,
    'Inputs!G16': 3.5,
    'Inputs!G17': 4,
    'Inputs!G18': 1,
}

# --- Constant leaf values ---
CONSTANTS = {
    'Engine!C5': 1,
    'Engine!D5': 2,
    'Engine!E5': 3,
    'Engine!F5': 4,
    'Engine!G5': 5,
    'Inputs!A10': 'Borvelia',
    'Inputs!A11': 'Litellia',
    'Inputs!A12': 'Aurelium',
}


# --- Formula cell functions ---

def cell_inputs_b6(ctx):
    '''Formula: =INDEX($A$10:$C$12,MATCH($B$5,$A$10:$A$12,0),2)'''
    return xl_offset(ctx, xl_index_ref(('Inputs', 10, 1, 12, 3), xl_match(xl_cell(ctx, 'Inputs!B5'), np.array(np.array([[xl_cell(ctx, 'Inputs!A10')], [xl_cell(ctx, 'Inputs!A11')], [xl_cell(ctx, 'Inputs!A12')]], dtype=object), dtype=object), 0.0), 2.0), 0.0, 0.0)


def cell_engine_b6(ctx):
    '''Formula: =Inputs!B6'''
    return xl_eval(ctx, 'Inputs!B6', cell_inputs_b6)


def cell_engine_c6(ctx):
    '''Formula: =B6*(1+Inputs!C17/100)/(1+Inputs!C16/100)-Inputs!C18'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!B6', cell_engine_b6), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!C17'), 100.0))), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!C16'), 100.0))), xl_cell(ctx, 'Inputs!C18'))


def cell_outputs_b12(ctx):
    '''Formula: =Engine!C6'''
    return xl_eval(ctx, 'Engine!C6', cell_engine_c6)


def cell_engine_d6(ctx):
    '''Formula: =C6*(1+Inputs!D17/100)/(1+Inputs!D16/100)-Inputs!D18'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!C6', cell_engine_c6), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!D17'), 100.0))), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!D16'), 100.0))), xl_cell(ctx, 'Inputs!D18'))


def cell_outputs_c12(ctx):
    '''Formula: =Engine!D6'''
    return xl_eval(ctx, 'Engine!D6', cell_engine_d6)


def cell_engine_e6(ctx):
    '''Formula: =D6*(1+Inputs!E17/100)/(1+Inputs!E16/100)-Inputs!E18'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!D6', cell_engine_d6), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!E17'), 100.0))), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!E16'), 100.0))), xl_cell(ctx, 'Inputs!E18'))


def cell_outputs_d12(ctx):
    '''Formula: =Engine!E6'''
    return xl_eval(ctx, 'Engine!E6', cell_engine_e6)


def cell_engine_f6(ctx):
    '''Formula: =E6*(1+Inputs!F17/100)/(1+Inputs!F16/100)-Inputs!F18'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!E6', cell_engine_e6), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!F17'), 100.0))), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!F16'), 100.0))), xl_cell(ctx, 'Inputs!F18'))


def cell_outputs_e12(ctx):
    '''Formula: =Engine!F6'''
    return xl_eval(ctx, 'Engine!F6', cell_engine_f6)


def cell_engine_g6(ctx):
    '''Formula: =F6*(1+Inputs!G17/100)/(1+Inputs!G16/100)-Inputs!G18'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!F6', cell_engine_f6), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!G17'), 100.0))), xl_add(1.0, xl_div(xl_cell(ctx, 'Inputs!G16'), 100.0))), xl_cell(ctx, 'Inputs!G18'))


def cell_outputs_f12(ctx):
    '''Formula: =Engine!G6'''
    return xl_eval(ctx, 'Engine!G6', cell_engine_g6)


def cell_engine_c10(ctx):
    '''Formula: =IF(C5>=Inputs!$B$21,1,0)'''
    return (_t2 if isinstance((_t2 := to_bool((_t1 := xl_ge(xl_cell(ctx, 'Engine!C5'), xl_cell(ctx, 'Inputs!B21'))))), XlError) else ((1.0) if _t2 else (0.0)))


def cell_engine_b9(ctx):
    '''Formula: =OFFSET(Inputs!$B$26,0,Inputs!$B$22-1)'''
    return xl_offset(ctx, ('Inputs', 26, 2), 0.0, xl_sub(xl_cell(ctx, 'Inputs!B22'), 1.0), None, None)


def cell_engine_c15(ctx):
    '''Formula: =Inputs!C17+CHOOSE(Inputs!$B$22,0,$B$9,0)*C10'''
    return xl_add(xl_cell(ctx, 'Inputs!C17'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!C10', cell_engine_c10)))


def cell_engine_c14(ctx):
    '''Formula: =Inputs!C16+CHOOSE(Inputs!$B$22,$B$9,0,0)*C10'''
    return xl_add(xl_cell(ctx, 'Inputs!C16'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!C10', cell_engine_c10)))


def cell_engine_c16(ctx):
    '''Formula: =Inputs!C18+CHOOSE(Inputs!$B$22,0,0,$B$9)*C10'''
    return xl_add(xl_cell(ctx, 'Inputs!C18'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!C10', cell_engine_c10)))


def cell_engine_b20(ctx):
    '''Formula: =Inputs!B6'''
    return xl_eval(ctx, 'Inputs!B6', cell_inputs_b6)


def cell_engine_c20(ctx):
    '''Formula: =B20*(1+C15/100)/(1+C14/100)-C16'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!B20', cell_engine_b20), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!C15', cell_engine_c15), 100.0))), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!C14', cell_engine_c14), 100.0))), xl_eval(ctx, 'Engine!C16', cell_engine_c16))


def cell_outputs_b13(ctx):
    '''Formula: =Engine!C20'''
    return xl_eval(ctx, 'Engine!C20', cell_engine_c20)


def cell_engine_d10(ctx):
    '''Formula: =IF(D5>=Inputs!$B$21,1,0)'''
    return (_t2 if isinstance((_t2 := to_bool((_t1 := xl_ge(xl_cell(ctx, 'Engine!D5'), xl_cell(ctx, 'Inputs!B21'))))), XlError) else ((1.0) if _t2 else (0.0)))


def cell_engine_d14(ctx):
    '''Formula: =Inputs!D16+CHOOSE(Inputs!$B$22,$B$9,0,0)*D10'''
    return xl_add(xl_cell(ctx, 'Inputs!D16'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!D10', cell_engine_d10)))


def cell_engine_d16(ctx):
    '''Formula: =Inputs!D18+CHOOSE(Inputs!$B$22,0,0,$B$9)*D10'''
    return xl_add(xl_cell(ctx, 'Inputs!D18'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!D10', cell_engine_d10)))


def cell_engine_d15(ctx):
    '''Formula: =Inputs!D17+CHOOSE(Inputs!$B$22,0,$B$9,0)*D10'''
    return xl_add(xl_cell(ctx, 'Inputs!D17'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!D10', cell_engine_d10)))


def cell_engine_d20(ctx):
    '''Formula: =C20*(1+D15/100)/(1+D14/100)-D16'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!C20', cell_engine_c20), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!D15', cell_engine_d15), 100.0))), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!D14', cell_engine_d14), 100.0))), xl_eval(ctx, 'Engine!D16', cell_engine_d16))


def cell_outputs_c13(ctx):
    '''Formula: =Engine!D20'''
    return xl_eval(ctx, 'Engine!D20', cell_engine_d20)


def cell_engine_e10(ctx):
    '''Formula: =IF(E5>=Inputs!$B$21,1,0)'''
    return (_t2 if isinstance((_t2 := to_bool((_t1 := xl_ge(xl_cell(ctx, 'Engine!E5'), xl_cell(ctx, 'Inputs!B21'))))), XlError) else ((1.0) if _t2 else (0.0)))


def cell_engine_e15(ctx):
    '''Formula: =Inputs!E17+CHOOSE(Inputs!$B$22,0,$B$9,0)*E10'''
    return xl_add(xl_cell(ctx, 'Inputs!E17'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!E10', cell_engine_e10)))


def cell_engine_e16(ctx):
    '''Formula: =Inputs!E18+CHOOSE(Inputs!$B$22,0,0,$B$9)*E10'''
    return xl_add(xl_cell(ctx, 'Inputs!E18'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!E10', cell_engine_e10)))


def cell_engine_e14(ctx):
    '''Formula: =Inputs!E16+CHOOSE(Inputs!$B$22,$B$9,0,0)*E10'''
    return xl_add(xl_cell(ctx, 'Inputs!E16'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!E10', cell_engine_e10)))


def cell_engine_e20(ctx):
    '''Formula: =D20*(1+E15/100)/(1+E14/100)-E16'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!D20', cell_engine_d20), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!E15', cell_engine_e15), 100.0))), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!E14', cell_engine_e14), 100.0))), xl_eval(ctx, 'Engine!E16', cell_engine_e16))


def cell_outputs_d13(ctx):
    '''Formula: =Engine!E20'''
    return xl_eval(ctx, 'Engine!E20', cell_engine_e20)


def cell_engine_f10(ctx):
    '''Formula: =IF(F5>=Inputs!$B$21,1,0)'''
    return (_t2 if isinstance((_t2 := to_bool((_t1 := xl_ge(xl_cell(ctx, 'Engine!F5'), xl_cell(ctx, 'Inputs!B21'))))), XlError) else ((1.0) if _t2 else (0.0)))


def cell_engine_f15(ctx):
    '''Formula: =Inputs!F17+CHOOSE(Inputs!$B$22,0,$B$9,0)*F10'''
    return xl_add(xl_cell(ctx, 'Inputs!F17'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!F10', cell_engine_f10)))


def cell_engine_f16(ctx):
    '''Formula: =Inputs!F18+CHOOSE(Inputs!$B$22,0,0,$B$9)*F10'''
    return xl_add(xl_cell(ctx, 'Inputs!F18'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!F10', cell_engine_f10)))


def cell_engine_f14(ctx):
    '''Formula: =Inputs!F16+CHOOSE(Inputs!$B$22,$B$9,0,0)*F10'''
    return xl_add(xl_cell(ctx, 'Inputs!F16'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!F10', cell_engine_f10)))


def cell_engine_f20(ctx):
    '''Formula: =E20*(1+F15/100)/(1+F14/100)-F16'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!E20', cell_engine_e20), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!F15', cell_engine_f15), 100.0))), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!F14', cell_engine_f14), 100.0))), xl_eval(ctx, 'Engine!F16', cell_engine_f16))


def cell_outputs_e13(ctx):
    '''Formula: =Engine!F20'''
    return xl_eval(ctx, 'Engine!F20', cell_engine_f20)


def cell_engine_g10(ctx):
    '''Formula: =IF(G5>=Inputs!$B$21,1,0)'''
    return (_t2 if isinstance((_t2 := to_bool((_t1 := xl_ge(xl_cell(ctx, 'Engine!G5'), xl_cell(ctx, 'Inputs!B21'))))), XlError) else ((1.0) if _t2 else (0.0)))


def cell_engine_g16(ctx):
    '''Formula: =Inputs!G18+CHOOSE(Inputs!$B$22,0,0,$B$9)*G10'''
    return xl_add(xl_cell(ctx, 'Inputs!G18'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((0.0) if _t2 == 2 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!G10', cell_engine_g10)))


def cell_engine_g14(ctx):
    '''Formula: =Inputs!G16+CHOOSE(Inputs!$B$22,$B$9,0,0)*G10'''
    return xl_add(xl_cell(ctx, 'Inputs!G16'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 1 else (((0.0) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!G10', cell_engine_g10)))


def cell_engine_g15(ctx):
    '''Formula: =Inputs!G17+CHOOSE(Inputs!$B$22,0,$B$9,0)*G10'''
    return xl_add(xl_cell(ctx, 'Inputs!G17'), xl_mul((_t1 if isinstance((_t1 := xl_cell(ctx, 'Inputs!B22')), XlError) else (_t2 if isinstance((_t2 := to_int(_t1)), XlError) else XlError.VALUE if _t2 < 1 or _t2 > 3 else ((0.0) if _t2 == 1 else (((xl_eval(ctx, 'Engine!B9', cell_engine_b9)) if _t2 == 2 else (((0.0) if _t2 == 3 else (XlError.VALUE)))))))), xl_eval(ctx, 'Engine!G10', cell_engine_g10)))


def cell_engine_g20(ctx):
    '''Formula: =F20*(1+G15/100)/(1+G14/100)-G16'''
    return xl_sub(xl_div(xl_mul(xl_eval(ctx, 'Engine!F20', cell_engine_f20), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!G15', cell_engine_g15), 100.0))), xl_add(1.0, xl_div(xl_eval(ctx, 'Engine!G14', cell_engine_g14), 100.0))), xl_eval(ctx, 'Engine!G16', cell_engine_g16))


def cell_outputs_f13(ctx):
    '''Formula: =Engine!G20'''
    return xl_eval(ctx, 'Engine!G20', cell_engine_g20)


def cell_outputs_b14(ctx):
    '''Formula: =B13-B12'''
    return xl_sub(xl_eval(ctx, 'Outputs!B13', cell_outputs_b13), xl_eval(ctx, 'Outputs!B12', cell_outputs_b12))


def cell_outputs_c14(ctx):
    '''Formula: =C13-C12'''
    return xl_sub(xl_eval(ctx, 'Outputs!C13', cell_outputs_c13), xl_eval(ctx, 'Outputs!C12', cell_outputs_c12))


def cell_outputs_d14(ctx):
    '''Formula: =D13-D12'''
    return xl_sub(xl_eval(ctx, 'Outputs!D13', cell_outputs_d13), xl_eval(ctx, 'Outputs!D12', cell_outputs_d12))


def cell_outputs_e14(ctx):
    '''Formula: =E13-E12'''
    return xl_sub(xl_eval(ctx, 'Outputs!E13', cell_outputs_e13), xl_eval(ctx, 'Outputs!E12', cell_outputs_e12))


def cell_outputs_f14(ctx):
    '''Formula: =F13-F12'''
    return xl_sub(xl_eval(ctx, 'Outputs!F13', cell_outputs_f13), xl_eval(ctx, 'Outputs!F12', cell_outputs_f12))


# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
def _address_to_func_name(address):
    name = []
    prev_underscore = False
    for ch in address.lower():
        if ch == "'":
            continue
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            name.append(ch)
            prev_underscore = False
        else:
            if not prev_underscore:
                name.append("_")
                prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{base}"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn

def make_context(inputs=None):
    """Create an EvalContext with merged inputs."""
    merged = dict(DEFAULT_INPUTS)
    merged.update(CONSTANTS)
    if inputs is not None:
        merged.update(inputs)
    return EvalContext(inputs=coerce_inputs_dict(merged), resolver=_resolve_formula, iterative_enabled=False, iterate_count=100, iterate_delta=0.001)


# --- Series binding setters (Records API) ---

Record = dict[str, object]
Records = list[Record]

_LEAF_INDEX_COUNTRY_NAME = {
    (('PARAMETER', 'country_name'),): 'Inputs!B5',
}

def set_country_name(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Selected country name from the country profile table."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'OBS_VALUE', 'PARAMETER'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_country_name (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_COUNTRY_NAME.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_GROWTH_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C16',
    (('TIME_PERIOD', 2),): 'Inputs!D16',
    (('TIME_PERIOD', 3),): 'Inputs!E16',
    (('TIME_PERIOD', 4),): 'Inputs!F16',
    (('TIME_PERIOD', 5),): 'Inputs!G16',
}

def set_growth_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Baseline real GDP growth rates for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'TIME_PERIOD', 'OBS_VALUE', 'INDICATOR', 'UNIT_MEASURE'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_growth_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_GROWTH_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_INTEREST_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C17',
    (('TIME_PERIOD', 2),): 'Inputs!D17',
    (('TIME_PERIOD', 3),): 'Inputs!E17',
    (('TIME_PERIOD', 4),): 'Inputs!F17',
    (('TIME_PERIOD', 5),): 'Inputs!G17',
}

def set_interest_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Baseline real interest rates for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'TIME_PERIOD', 'OBS_VALUE', 'INDICATOR', 'UNIT_MEASURE'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_interest_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_INTEREST_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_PRIMARY_BALANCE_BASELINE = {
    (('TIME_PERIOD', 1),): 'Inputs!C18',
    (('TIME_PERIOD', 2),): 'Inputs!D18',
    (('TIME_PERIOD', 3),): 'Inputs!E18',
    (('TIME_PERIOD', 4),): 'Inputs!F18',
    (('TIME_PERIOD', 5),): 'Inputs!G18',
}

def set_primary_balance_baseline(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Baseline primary balance path for projection years 1 through 5."""
    key_fields = ('TIME_PERIOD',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'TIME_PERIOD', 'OBS_VALUE', 'INDICATOR', 'UNIT_MEASURE'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_primary_balance_baseline (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_PRIMARY_BALANCE_BASELINE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_SHOCK_YEAR = {
    (('PARAMETER', 'shock_year'),): 'Inputs!B21',
}

def set_shock_year(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """First projection year in which the selected shock applies."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'OBS_VALUE', 'PARAMETER'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_shock_year (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_YEAR.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_SHOCK_TYPE = {
    (('PARAMETER', 'shock_type'),): 'Inputs!B22',
}

def set_shock_type(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Shock type code: 1 for growth, 2 for interest, 3 for primary balance."""
    key_fields = ('PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'OBS_VALUE', 'PARAMETER'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_shock_type (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_TYPE.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

_LEAF_INDEX_SHOCK_MAGNITUDES = {
    (('SHOCK_PARAMETER', 'Growth'),): 'Inputs!B26',
    (('SHOCK_PARAMETER', 'Interest'),): 'Inputs!C26',
    (('SHOCK_PARAMETER', 'Primary balance'),): 'Inputs!D26',
}

def set_shock_magnitudes(
    ctx: EvalContext,
    records: Records,
    *,
    strict: bool = True,
) -> None:
    """Shock magnitudes keyed by affected parameter."""
    key_fields = ('SHOCK_PARAMETER',)
    allow_address = False
    requires_address = False
    measure_field = 'OBS_VALUE'
    allowed_fields = {'SHOCK_PARAMETER', 'OBS_VALUE', 'UNIT_MEASURE', 'PARAMETER'}
    updates: dict[str, object] = {}
    for index, record in enumerate(records):
        if strict:
            unknown = set(record) - allowed_fields
            if unknown:
                raise ValueError(f"record[{index}]: unknown fields {sorted(unknown)!r}")
        if measure_field not in record:
            raise ValueError(f"record[{index}]: missing required field {measure_field!r}")
        address = None
        if allow_address or requires_address:
            address = record.get("address") or record.get("cell_address")
        if requires_address and address is None:
            raise ValueError(f"record[{index}]: address required for set_shock_magnitudes (duplicate keys in binding)")
        if address is None:
            missing = [field for field in key_fields if field not in record]
            if missing:
                raise ValueError(f"record[{index}]: missing key fields {missing!r}")
            key_tuple = tuple((field, record[field]) for field in key_fields)
            address = _LEAF_INDEX_SHOCK_MAGNITUDES.get(key_tuple)
            if address is None:
                raise ValueError(f"record[{index}]: no leaf matches key {dict(key_tuple)!r}")
        updates[address] = record[measure_field]
    if updates:
        ctx.set_inputs(coerce_inputs_dict(updates))

# --- Series binding output compute (Records API) ---

_OUTPUT_LEAVES_OUTPUT_BASELINE = [
    ('Outputs!B12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F12', {'SCENARIO': 'baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_baseline(inputs=None, *, ctx=None) -> Records:
    """Baseline debt-to-GDP path for projection years 1 through 5."""
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_BASELINE:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records

_OUTPUT_LEAVES_OUTPUT_SHOCKED = [
    ('Outputs!B13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!C13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!D13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!E13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PC_GDP'}),
    ('Outputs!F13', {'SCENARIO': 'shocked', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PC_GDP'}),
]

def compute_output_shocked(inputs=None, *, ctx=None) -> Records:
    """Shocked debt-to-GDP path for projection years 1 through 5."""
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_SHOCKED:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records

_OUTPUT_LEAVES_OUTPUT_DELTA = [
    ('Outputs!B14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 1, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!C14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 2, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!D14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 3, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!E14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 4, 'UNIT_MEASURE': 'PP'}),
    ('Outputs!F14', {'SCENARIO': 'shocked_minus_baseline', 'TIME_PERIOD': 5, 'UNIT_MEASURE': 'PP'}),
]

def compute_output_delta(inputs=None, *, ctx=None) -> Records:
    """Difference between the shocked and baseline paths in percentage points."""
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    measure_field = 'OBS_VALUE'
    include_address = False
    records: Records = []
    for address, static_record in _OUTPUT_LEAVES_OUTPUT_DELTA:
        record = dict(static_record)
        record[measure_field] = xl_cell(ctx, address)
        if include_address:
            record["address"] = address
        records.append(record)
    return records


TARGETS = {
    'Outputs!B12:Outputs!F12': xl_range,
    'Outputs!B13:Outputs!F13': xl_range,
    'Outputs!B14:Outputs!F14': xl_range,
}


def compute_all(inputs=None, *, ctx=None):
    """Compute all target cells and return results."""
    if ctx is None:
        ctx = make_context(inputs)
    elif inputs is not None:
        warnings.warn("inputs will be ignored because ctx was provided", UserWarning, stacklevel=2)
    return {target: handler(ctx, target) for target, handler in TARGETS.items()}
