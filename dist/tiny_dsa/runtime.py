"""Standalone runtime for generated Excel formula code."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from fastpyxl.utils.cell import column_index_from_string, coordinate_from_string
from fastpyxl.utils.exceptions import CellCoordinatesException
from typing import Any, NoReturn, TypeAlias, cast

import fastpyxl.utils.cell
import re

class CircularReferenceWarning(RuntimeWarning):
    """Warning emitted when a circular reference is encountered (default Excel mode)."""

HelperCacheKey: TypeAlias = tuple[Hashable, tuple[tuple[str, Hashable], ...]]

MISSING: object = object()

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

Scalar: TypeAlias = float | int | str | bool | XlError | None

class XlErrorException(Exception):
    """Exception form of an Excel error code.

    The exported runtime raises Excel errors as exceptions; the evaluator keeps
    `XlError` sentinel values and never raises this type.
    """

    code: XlError

    def __init__(self, code: XlError) -> None:
        """Initialize the exception with an Excel error code."""
        if not isinstance(code, XlError):
            raise TypeError(f"Expected XlError, got {type(code).__name__}")
        self.code = code
        super().__init__(code.value)

_A1_CELL_COORD_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")

_EXCEL_EPOCH = datetime(1899, 12, 30)

_WHOLE_COL_COORD_RE = re.compile(r"^\$?([A-Za-z]{1,3})$")

_WHOLE_ROW_COORD_RE = re.compile(r"^\$?(\d+)$")

def _escape_sheet_for_formula(sheet: str) -> str:
    """Escape apostrophes for use inside quoted sheet names."""
    return sheet.replace("'", "''")

def _format_general_number(value: float | int) -> str:
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return str(f)

def _looks_like_leaf_store(values: Mapping[object, object]) -> bool:
    """True when values look like `sheet -> {(row, col): value}`."""
    for sample in values.values():
        if not isinstance(sample, Mapping):
            return False
        for coord in sample:
            return (
                isinstance(coord, tuple)
                and len(coord) == 2
                and isinstance(coord[0], int)
                and isinstance(coord[1], int)
            )
        return True
    return False

def _ndarray_grid_shape(value: object) -> tuple[int, int] | None:
    """Read a 1-D/2-D ndarray-like shape as ``(nrows, ncols)`` without converting it.

    Lets `Grid` hold the array itself instead of eagerly copying every cell into
    nested lists. 1-D buffers read as single-column grids, matching
    `_as_nested_rows_from_ndarray`. Returns `None` for anything else (including
    3-D buffers) so callers keep the nested-list path.
    """
    ndim = getattr(value, "ndim", None)
    if ndim not in (1, 2) or not callable(getattr(value, "tolist", None)):
        return None
    shape = getattr(value, "shape", None)
    if not isinstance(shape, tuple) or len(shape) != ndim:
        return None
    if not all(isinstance(extent, int) for extent in shape):
        return None
    if ndim == 1:
        return (shape[0], 1)
    return (shape[0], shape[1])

def _raise_error(code: XlError) -> XlErrorException:
    """Build the exception for an Excel error code (callers raise the result)."""
    return XlErrorException(code)

def _raise_if_error_value(value: CellValue) -> CellValue:
    """Surface Excel error values as raised exceptions at the cell boundary."""
    if isinstance(value, XlError):
        raise XlErrorException(value)
    return value

def _require_coord(coord: object) -> tuple[int, int]:
    if not (isinstance(coord, tuple) and len(coord) == 2):
        raise TypeError(f"Leaf store keys must be (row, col) tuples; got {coord!r}")
    row, col = coord
    if not isinstance(row, int) or not isinstance(col, int):
        raise TypeError(f"Leaf store keys must be (row, col) ints; got {coord!r}")
    if row < 1 or col < 1:
        raise ValueError(f"Leaf store coordinates must be 1-based; got {coord!r}")
    return row, col

def canonical_cell_coord(cell: str) -> str:
    """Canonicalize an A1 / whole-column / whole-row coordinate fragment.

    Strips `$` markers, uppercases column letters, and normalizes row numbers
    (`01` -> `1`). Non-matching fragments are returned unchanged.
    """
    m = _A1_CELL_COORD_RE.fullmatch(cell)
    if m is not None:
        return f"{m.group(1).upper()}{int(m.group(2))}"
    m_col = _WHOLE_COL_COORD_RE.fullmatch(cell)
    if m_col is not None:
        return m_col.group(1).upper()
    m_row = _WHOLE_ROW_COORD_RE.fullmatch(cell)
    if m_row is not None:
        return str(int(m_row.group(1)))
    return cell

def _parse_a1_cell(cell: str) -> tuple[str, int]:
    """Parse an A1 cell coordinate into uppercase column letters and row."""
    try:
        column, row = coordinate_from_string(canonical_cell_coord(cell))
    except CellCoordinatesException as exc:
        raise ValueError(f"Expected A1 cell coordinate, got: {cell!r}") from exc
    return str(column).upper(), int(row)

def datetime_to_excel_serial(value: datetime) -> float:
    """Convert a naive datetime to an Excel day serial (1900 date system)."""
    naive = value.replace(tzinfo=None) if value.tzinfo is not None else value
    delta = naive - _EXCEL_EPOCH
    return delta.days + (delta.seconds + delta.microseconds / 1_000_000) / 86_400.0

def _try_parse_iso_date_serial(text: str) -> float | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        if "T" in stripped or " " in stripped:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
        else:
            parsed = datetime.combine(date.fromisoformat(stripped), datetime.min.time())
        return datetime_to_excel_serial(parsed)
    except ValueError:
        return None

def excel_casefold(value: str) -> str:
    return value.casefold()

def leaf(store: LeafStore, sheet: str, row: int, col: int) -> object:
    """Return the stored leaf at `(sheet, row, col)`, or `MISSING` if absent."""
    sheet_map = store.get(sheet)
    if sheet_map is None:
        return MISSING
    return sheet_map.get((row, col), MISSING)

def needs_quoting(sheet: str) -> bool:
    """Return True if a sheet name must be wrapped in single quotes in a formula."""
    return " " in sheet or "-" in sheet or "'" in sheet

def parse_address(address: str) -> tuple[str, str]:
    """Parse a sheet-qualified address into `(sheet, cell_coord)`.

    The returned sheet name has any surrounding single quotes stripped and any
    escaped apostrophes (`''`) unescaped to a single apostrophe.

    Examples:
        >>> parse_address("Sheet1!A1")
        ('Sheet1', 'A1')
        >>> parse_address("'My Sheet'!B2")
        ('My Sheet', 'B2')
        >>> parse_address("'It''s Data'!C3")
        ("It's Data", 'C3')
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
        sheet = address[1:i].replace("''", "'")
        rest = address[i + 1 :]
        if rest.startswith("!"):
            return sheet, rest[1:]
        raise ValueError(f"Invalid address format: {address}")

    if "!" in address:
        sheet, cell = address.rsplit("!", 1)
        return sheet, cell

    raise ValueError(f"Address must be sheet-qualified: {address}")

def parse_cell_coords(address: str) -> tuple[str, int, int]:
    """Parse a sheet-qualified A1 cell into `(sheet, row, col)` (1-based).

    Raises:
        ValueError: If `address` is not a sheet-qualified single cell.
    """
    sheet, cell = parse_address(address)
    col_letters, row = _parse_a1_cell(cell)
    return sheet, row, int(column_index_from_string(col_letters))

def _require_cell_coords(address: str) -> tuple[str, int, int]:
    try:
        return parse_cell_coords(address)
    except ValueError as exc:
        raise ValueError(f"Cannot round-trip input key to (sheet, row, col): {address!r}") from exc

class LeafInputs:
    """NodeKey-keyed view over a nested `LeafStore`.

    Get/set parse A1 at the boundary. Rectangle scans should call `leaf`
    with integer coordinates instead of iterating this view.
    """

    __slots__ = ("_store",)

    def __init__(self, store: LeafStore) -> None:
        self._store = store

    def __getitem__(self, address: str) -> CellValue:
        sheet, row, col = _require_cell_coords(address)
        try:
            return self._store[sheet][(row, col)]
        except KeyError:
            raise KeyError(address) from None

    def __setitem__(self, address: str, value: CellValue) -> None:
        sheet, row, col = _require_cell_coords(address)
        self._store.setdefault(sheet, {})[(row, col)] = value

    def __contains__(self, address: object) -> bool:
        if not isinstance(address, str):
            return False
        try:
            sheet, row, col = parse_cell_coords(address)
        except ValueError:
            return False
        sheet_map = self._store.get(sheet)
        if sheet_map is None:
            return False
        return (row, col) in sheet_map

    def get(self, address: str, default: CellValue | None = None) -> CellValue | None:
        try:
            return self[address]
        except KeyError:
            return default

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LeafInputs):
            return self._store == other._store
        return NotImplemented

def quote_sheet_if_needed(sheet: str) -> str:
    """Return a sheet name quoted for formulas when quoting is required."""
    if not needs_quoting(sheet):
        return sheet
    return "'" + _escape_sheet_for_formula(sheet) + "'"

def format_cell_key(sheet: str, column: str, row: int) -> NormalizedAddress:
    """Format a (sheet, column_letters, row) triple into a canonical address."""
    return f"{quote_sheet_if_needed(sheet)}!{column}{row}"

@dataclass(frozen=True, slots=True)
class Range:
    """Rectangular lazy range with consumer-driven cell access.

    Coordinates passed to `cell`, `row`, `column`, and `view` are 1-based and
    relative to this range, matching Excel function arguments.
    """

    sheet: str
    start_row: int
    start_col: int
    end_row: int
    end_col: int
    # Resolvers may come from evaluation contexts with their own value
    # vocabulary; values are validated/coerced at consumption time.
    _resolver: Callable[[str], FormulaValue] = field(repr=False, compare=False)
    # Optional coordinate reader: `(row, col)` absolute 1-based. When set,
    # `cell` / `value_at` use it and do not construct NodeKey strings.
    _coord_resolver: Callable[[int, int], FormulaValue] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Validate the rectangular bounds."""
        if self.start_row < 1 or self.start_col < 1:
            raise ValueError("Range coordinates must be positive")
        if self.end_row < self.start_row or self.end_col < self.start_col:
            raise ValueError("Range end must be greater than or equal to start")

    @property
    def shape(self) -> tuple[int, int]:
        """The range shape as `(rows, columns)`."""
        return (self.end_row - self.start_row + 1, self.end_col - self.start_col + 1)

    def cell_addresses(self) -> Iterator[str]:
        """Yield row-major addresses without evaluating cells."""
        for row in range(self.start_row, self.end_row + 1):
            for col in range(self.start_col, self.end_col + 1):
                yield self._address(row, col)

    def cell(self, row: int, col: int) -> FormulaValue:
        """Return a single relative cell value without evaluating siblings.

        Args:
            row: 1-based row within the range.
            col: 1-based column within the range.

        Raises:
            IndexError: If `row` or `col` is outside the range.
            XlErrorException: If the resolved cell is an Excel error.
        """
        self._validate_relative_cell(row, col)
        value = self._resolve_at(self.start_row + row - 1, self.start_col + col - 1)
        return self._raise_if_error(value)

    def row(self, row: int) -> Range:
        """Return a lazy view for one relative row."""
        nrows, _ = self.shape
        if row < 1 or row > nrows:
            raise IndexError("Range row is out of bounds")
        absolute_row = self.start_row + row - 1
        return Range(
            self.sheet,
            absolute_row,
            self.start_col,
            absolute_row,
            self.end_col,
            self._resolver,
            _coord_resolver=self._coord_resolver,
        )

    def column(self, col: int) -> Range:
        """Return a lazy view for one relative column."""
        _, ncols = self.shape
        if col < 1 or col > ncols:
            raise IndexError("Range column is out of bounds")
        absolute_col = self.start_col + col - 1
        return Range(
            self.sheet,
            self.start_row,
            absolute_col,
            self.end_row,
            absolute_col,
            self._resolver,
            _coord_resolver=self._coord_resolver,
        )

    def view(
        self,
        row_start: int = 1,
        row_end: int | None = None,
        col_start: int = 1,
        col_end: int | None = None,
    ) -> Range:
        """Return a lazy rectangular subrange view using relative coordinates."""
        nrows, ncols = self.shape
        row_end = nrows if row_end is None else row_end
        col_end = ncols if col_end is None else col_end
        self._validate_relative_cell(row_start, col_start)
        self._validate_relative_cell(row_end, col_end)
        if row_end < row_start or col_end < col_start:
            raise ValueError("Range view end must be greater than or equal to start")
        return Range(
            self.sheet,
            self.start_row + row_start - 1,
            self.start_col + col_start - 1,
            self.start_row + row_end - 1,
            self.start_col + col_end - 1,
            self._resolver,
            _coord_resolver=self._coord_resolver,
        )

    def value_at(self, row: int, col: int) -> FormulaValue:
        """Return a single relative cell value with errors as sentinels.

        Unlike `cell`, Excel errors surface as `XlError` sentinel values (raised
        `XlErrorException`s from the resolver are caught and converted). Range
        consumers that implement Excel skip semantics (lookup scans, criteria
        matching) use this accessor; `cell`/iteration raise instead.
        """
        self._validate_relative_cell(row, col)
        try:
            return self._resolve_at(self.start_row + row - 1, self.start_col + col - 1)
        except XlErrorException as exc:
            return exc.code

    def iter_raw(self) -> Iterator[FormulaValue]:
        """Yield raw values (error sentinels included) in row-major order."""
        nrows, ncols = self.shape
        for row in range(1, nrows + 1):
            for col in range(1, ncols + 1):
                yield self.value_at(row, col)

    def rows_raw(self) -> list[list[FormulaValue]]:
        """Materialize the range as nested row lists of raw values."""
        nrows, ncols = self.shape
        return [[self.value_at(r, c) for c in range(1, ncols + 1)] for r in range(1, nrows + 1)]

    def iter_values(self) -> Iterator[FormulaValue]:
        """Yield values in deterministic row-major order."""
        nrows, ncols = self.shape
        for row in range(1, nrows + 1):
            for col in range(1, ncols + 1):
                yield self.cell(row, col)

    def __iter__(self) -> Iterator[FormulaValue]:
        """Yield values in deterministic row-major order."""
        return self.iter_values()

    def _resolve_at(self, row: int, col: int) -> FormulaValue:
        if self._coord_resolver is not None:
            return self._coord_resolver(row, col)
        return self._resolver(self._address(row, col))

    def _address(self, row: int, col: int) -> str:
        col_letter = fastpyxl.utils.cell.get_column_letter(col)
        return format_cell_key(self.sheet, col_letter, row)

    def _validate_relative_cell(self, row: int, col: int) -> None:
        nrows, ncols = self.shape
        if row < 1 or row > nrows or col < 1 or col > ncols:
            raise IndexError("Range cell is out of bounds")

    @staticmethod
    def _raise_if_error(value: FormulaValue) -> FormulaValue:
        if isinstance(value, XlError):
            raise XlErrorException(value)
        return value

def _format_address(sheet: str, row: int, col: int) -> str:
    return format_cell_key(sheet, fastpyxl.utils.cell.get_column_letter(col), row)

def format_key(sheet: str, cell: str) -> NormalizedAddress:
    """Format a sheet and A1 cell coordinate into a canonical address string."""
    return f"{quote_sheet_if_needed(sheet)}!{cell}"

@dataclass(frozen=True, slots=True)
class ExcelRange:
    """Rectangular worksheet reference geometry for evaluator and export."""

    sheet: str
    start_row: int
    start_col: int
    end_row: int
    end_col: int

    @property
    def shape(self) -> tuple[int, int]:
        """The reference shape as `(rows, columns)`."""
        return (self.end_row - self.start_row + 1, self.end_col - self.start_col + 1)

    def cell_addresses(self) -> Iterator[str]:
        """Yield row-major sheet-qualified addresses without evaluating cells."""
        for r in range(self.start_row, self.end_row + 1):
            for c in range(self.start_col, self.end_col + 1):
                col = fastpyxl.utils.cell.get_column_letter(c)
                yield format_key(self.sheet, f"{col}{r}")

CellValue: TypeAlias = Scalar | ExcelRange | Range | list["CellValue"]

LeafStore: TypeAlias = dict[str, dict[tuple[int, int], CellValue]]

NestedGrid: TypeAlias = list[list[CellValue]]

FormulaValue: TypeAlias = CellValue | NestedGrid

def _as_nested_rows_from_ndarray(value: object) -> list[list[CellValue]] | None:
    """Convert an ndarray-like value to nested lists without importing NumPy.

    Duck-types via ``ndim`` / ``tolist`` so the grid module stays import-light
    for standalone exports that must remain NumPy-free.
    """
    ndim = getattr(value, "ndim", None)
    tolist = getattr(value, "tolist", None)
    if not isinstance(ndim, int) or not callable(tolist):
        return None
    if ndim == 0:
        return None
    raw = tolist()
    if ndim == 1:
        return [[cast(CellValue, cell)] for cell in raw]
    return cast("list[list[CellValue]]", raw)

class Grid:
    """Positional raw-value access over a lazy `Range`, ndarray, or nested-list array."""

    __slots__ = ("nrows", "ncols", "_range", "_rows", "_array")

    def __init__(
        self,
        nrows: int,
        ncols: int,
        rng: Range | None,
        rows: list[list[CellValue]] | None,
        array: object = None,
    ) -> None:
        self.nrows = nrows
        self.ncols = ncols
        self._range = rng
        self._rows = rows
        self._array = array

    @staticmethod
    def wrap(value: object) -> Grid | None:
        """Wrap a range/array value; return `None` for scalar values.

        ndarray operands are kept as-is: consumers that want the buffer read
        `array`, and positional consumers pay for nested rows only on first use.
        """
        if isinstance(value, Range):
            nrows, ncols = value.shape
            return Grid(nrows, ncols, value, None)
        ndarray_shape = _ndarray_grid_shape(value)
        if ndarray_shape is not None:
            nrows, ncols = ndarray_shape
            if nrows == 0:
                return Grid(1, 1, None, [[None]])
            return Grid(nrows, ncols, None, None, value)
        ndarray_rows = _as_nested_rows_from_ndarray(value)
        if ndarray_rows is not None:
            if not ndarray_rows:
                ndarray_rows = [[None]]
            return Grid(len(ndarray_rows), len(ndarray_rows[0]), None, ndarray_rows)
        if isinstance(value, (list, tuple)):
            rows = [
                list(row) if isinstance(row, (list, tuple)) else [row]
                for row in cast("list[CellValue]", value)
            ]
            if not rows:
                rows = [[None]]
            return Grid(len(rows), len(rows[0]), None, cast("list[list[CellValue]]", rows))
        return None

    @property
    def array(self) -> object:
        """The backing ndarray for ndarray operands, else `None`.

        Array consumers (vectorized operator fast paths) use this to skip the
        nested-list round trip; everything else goes through `at`.
        """
        return self._array

    def _nested_rows(self) -> list[list[CellValue]]:
        """Materialize (and cache) nested rows for positional access."""
        rows = self._rows
        if rows is None:
            rows = _as_nested_rows_from_ndarray(self._array)
            assert rows is not None
            self._rows = rows
        return rows

    def at(self, row0: int, col0: int) -> Scalar:
        """Return the raw value at a 0-based position (error sentinels included)."""
        if self._range is not None:
            return cast(Scalar, self._range.value_at(row0 + 1, col0 + 1))
        return cast(Scalar, self._nested_rows()[row0][col0])

    def at_flat(self, index0: int) -> Scalar:
        """Return the raw value at a 0-based row-major flat index."""
        row0, col0 = divmod(index0, self.ncols)
        return self.at(row0, col0)

    @property
    def size(self) -> int:
        """Total cell count."""
        return self.nrows * self.ncols

    def iter_raw(self) -> Iterator[Scalar]:
        """Yield raw values (error sentinels included) in row-major order."""
        for row0 in range(self.nrows):
            for col0 in range(self.ncols):
                yield self.at(row0, col0)

    def row_slice(self, row0: int) -> Range | list[list[CellValue]]:
        """Return one row as a lazy view (`Range` input) or nested list."""
        if self._range is not None:
            return self._range.row(row0 + 1)
        return [list(self._nested_rows()[row0])]

    def col_slice(self, col0: int) -> Range | list[list[CellValue]]:
        """Return one column as a lazy view (`Range` input) or nested list."""
        if self._range is not None:
            return self._range.column(col0 + 1)
        return [[row[col0]] for row in self._nested_rows()]

    def as_array(self) -> Range | list[list[CellValue]]:
        """Return the full grid as a lazy `Range` or nested-list copy."""
        if self._range is not None:
            return self._range
        return [list(row) for row in self._nested_rows()]

def _as_scalar(value: object) -> Scalar:
    if isinstance(value, (Range, list, tuple)):
        return XlError.VALUE
    if Grid.wrap(value) is not None:
        return XlError.VALUE
    return cast(Scalar, value)

def _raise_if_error(value: object) -> CellValue:
    if isinstance(value, XlError):
        raise _raise_error(value)
    return cast(CellValue, value)

def _range_from_ref_info(ref: ExcelRange | OffsetRefInfo) -> ExcelRange:
    """Normalize generated reference metadata into an `ExcelRange`."""
    if isinstance(ref, ExcelRange):
        return ref
    match ref:
        case (sheet, base_row, base_col):
            return ExcelRange(
                sheet=sheet,
                start_row=base_row,
                start_col=base_col,
                end_row=base_row,
                end_col=base_col,
            )
        case (sheet, base_row, base_col, base_end_row, base_end_col):
            return ExcelRange(
                sheet=sheet,
                start_row=base_row,
                start_col=base_col,
                end_row=base_end_row,
                end_col=base_end_col,
            )
        case _:
            raise XlErrorException(XlError.VALUE)

def as_scalar(value: CellValue) -> Scalar:
    """Collapse range/array values to `#VALUE!` for scalar coercion contexts.

    Keep behavior aligned with `excel_grapher.core.coercions.as_scalar`. This
    module is embedded into standalone exports and cannot import library code.
    """
    if isinstance(value, (Range, ExcelRange, list, tuple)):
        return XlError.VALUE
    return value

def _as_addressing_scalar(value: CellValue | None) -> Scalar | None:
    """Collapse export-runtime values to scalars for shared addressing helpers."""
    if value is None:
        return None
    return as_scalar(value)

def coerce_inputs_dict(values: Mapping[str, object]) -> dict[str, CellValue]:
    """Widen inferred default-input dicts to `dict[str, CellValue]` for `EvalContext`."""
    return cast(dict[str, CellValue], dict(values))

def lookup_leaf(ctx: object, address: str) -> Any:
    """Look up a leaf by NodeKey, using `ctx.leaves` when present.

    Returns `MISSING` when the address is not a stored leaf (including when it
    is not a parseable single cell). Formula cells must still go through the
    resolver.
    """
    store = getattr(ctx, "leaves", None)
    if not store:
        return MISSING
    try:
        sheet, row, col = parse_cell_coords(address)
    except ValueError:
        return MISSING
    return leaf(cast(LeafStore, store), sheet, row, col)

def overlay_leaf_inputs(store: LeafStore, overlay: Any) -> None:
    """Merge `overlay` into `store`.

    Nested coordinate stores merge sheet-by-sheet. NodeKey dicts parse A1 at
    the boundary.

    Raises:
        ValueError: If a NodeKey cannot round-trip to `(sheet, row, col)`.
        TypeError: If `overlay` is neither a leaf store nor a NodeKey mapping.
    """
    if not overlay:
        return
    if not isinstance(overlay, Mapping):
        raise TypeError(f"Expected a mapping of leaves; got {type(overlay)!r}")
    if _looks_like_leaf_store(overlay):
        for sheet, cells in overlay.items():
            if not isinstance(sheet, str):
                raise TypeError(f"Leaf store sheets must be strings; got {sheet!r}")
            if not isinstance(cells, Mapping):
                raise TypeError(f"Leaf store sheet map must be a mapping; got {cells!r}")
            sheet_map = store.setdefault(sheet, {})
            for coord, value in cells.items():
                row, col = _require_coord(coord)
                sheet_map[(row, col)] = cast(CellValue, value)
        return
    for key, value in overlay.items():
        if not isinstance(key, str):
            raise TypeError(f"Input keys must be NodeKey strings; got {key!r}")
        sheet, row, col = _require_cell_coords(key)
        store.setdefault(sheet, {})[(row, col)] = cast(CellValue, value)

def as_leaf_store(values: Any) -> LeafStore:
    """Copy `values` into a nested leaf store.

    Accepts a nested coordinate store or a NodeKey-keyed dict. Empty mappings
    become `{}`.
    """
    if isinstance(values, LeafInputs):
        return {sheet: dict(cells) for sheet, cells in values._store.items()}
    if not isinstance(values, Mapping):
        raise TypeError(f"Expected a mapping of leaves; got {type(values)!r}")
    if not values:
        return {}
    if _looks_like_leaf_store(values):
        out: LeafStore = {}
        for sheet, cells in values.items():
            if not isinstance(sheet, str):
                raise TypeError(f"Leaf store sheets must be strings; got {sheet!r}")
            if not isinstance(cells, Mapping):
                raise TypeError(f"Leaf store sheet map must be a mapping; got {cells!r}")
            sheet_map: dict[tuple[int, int], CellValue] = {}
            for coord, value in cells.items():
                row, col = _require_coord(coord)
                sheet_map[(row, col)] = cast(CellValue, value)
            out[sheet] = sheet_map
        return out
    out = {}
    overlay_leaf_inputs(out, values)
    return out

@dataclass(slots=True)
class EvalContextBase:
    """Per-run evaluation state without dependency-tracking fields."""

    inputs: Any
    resolver: Callable[[str], Callable[[EvalContext], CellValue] | None]
    cache: dict[str, CellValue] = field(default_factory=dict)
    computing: set[str] = field(default_factory=set)
    circular_warning_roots: set[str] = field(default_factory=set)
    helper_cache: dict[HelperCacheKey, CellValue] = field(default_factory=dict)
    helper_computing: set[HelperCacheKey] = field(default_factory=set)
    iterative_enabled: bool = False
    iterate_count: int = 100
    iterate_delta: float = 0.001
    iteration_values: dict[str, CellValue] = field(default_factory=dict)
    leaves: LeafStore = field(default_factory=dict)

    def __post_init__(self) -> None:
        store = as_leaf_store(self.inputs) if self.inputs else as_leaf_store(self.leaves)
        self.leaves = store
        self.inputs = LeafInputs(store)

@dataclass(slots=True)
class EvalContext(EvalContextBase):
    """Per-run evaluation state with dependency tracking for input invalidation."""

    deps: dict[str, set[str]] = field(default_factory=dict)
    reverse_deps: dict[str, set[str]] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)

    def _record_dependency(self, parent: str, child: str) -> None:
        if parent == child:
            return
        self.deps.setdefault(parent, set()).add(child)
        self.reverse_deps.setdefault(child, set()).add(parent)

    def invalidate(self, addresses: Iterable[str]) -> None:
        """Invalidate cached values for the given addresses and their dependents.

        Helper memos are not address-dep-tracked, so any address invalidation
        clears `helper_cache` and `helper_computing` entirely.
        """
        self.helper_cache.clear()
        self.helper_computing.clear()

        to_visit = list(addresses)
        seen: set[str] = set()
        while to_visit:
            addr = to_visit.pop()
            if addr in seen:
                continue
            seen.add(addr)

            self.cache.pop(addr, None)
            self.circular_warning_roots.discard(addr)
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
        """Update input values and invalidate dependent cached results.

        `inputs` is NodeKey-keyed. Keys that cannot round-trip to
        `(sheet, row, col)` raise `ValueError`.
        """
        parsed = as_leaf_store(inputs)
        changed = [k for k, v in inputs.items() if self.inputs.get(k) != v]
        overlay_leaf_inputs(self.leaves, parsed)
        if changed:
            self.invalidate(changed)

def prepare_context_inputs(
    default_inputs: Any,
    constants: Any = None,
    overlay: Any = None,
) -> LeafStore:
    """Copy defaults, then merge constants and a NodeKey overlay."""
    merged = as_leaf_store(default_inputs)
    if constants:
        overlay_leaf_inputs(merged, constants)
    if overlay is not None:
        overlay_leaf_inputs(merged, overlay)
    return merged

def split_sheet_qualified_address(address: str) -> tuple[str, str] | None:
    """Split `sheet!coord` into `(sheet_name, coord)`.

    Handles quoted sheet names, including Excel's doubled-single-quote escape
    (`'O''Neil'!A1` -> sheet `O'Neil`).

    Returns `None` when *address* has no sheet qualifier (plain `A1`).
    """
    if "!" not in address:
        return None
    try:
        return parse_address(address)
    except ValueError:
        return None

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

def to_bool(value: FormulaValue) -> bool | XlError:
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        return scalar
    value = cast(CellValue, scalar)
    if value is None:
        return False
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
    return XlError.VALUE

def to_string(value: FormulaValue) -> str:
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        return scalar.value
    value = cast(CellValue, scalar)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return _format_general_number(float(value))
    if isinstance(value, str):
        return value
    return str(value)

def try_coerce_string_to_float(text: str) -> float | None:
    """Parse one Excel numeric string; empty/whitespace text fails (`None`)."""
    stripped = text.strip()
    if stripped == "":
        return None
    try:
        return float(stripped)
    except ValueError:
        return _try_parse_iso_date_serial(stripped)

def to_number(value: FormulaValue) -> float | XlError:
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        return scalar
    value = cast(CellValue, scalar)
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        number = try_coerce_string_to_float(value)
        if number is None:
            return XlError.VALUE
        return number
    return XlError.VALUE

def _compare_values(a: object, b: object) -> int:
    a = _as_scalar(a)
    b = _as_scalar(b)
    an = to_number(a)
    bn = to_number(b)
    if not isinstance(an, XlError) and not isinstance(bn, XlError):
        return -1 if an < bn else 1 if an > bn else 0
    if isinstance(a, str) and isinstance(b, str):
        af = excel_casefold(a)
        bf = excel_casefold(b)
        return -1 if af < bf else 1 if af > bf else 0
    return 0

def _number_or_raise(value: CellValue) -> float:
    """Coerce a scalar argument to a number, raising on Excel coercion errors."""
    number = to_number(as_scalar(value))
    if isinstance(number, XlError):
        raise XlErrorException(number)
    return number

def _values_match(a: object, b: object) -> bool:
    a = _as_scalar(a)
    b = _as_scalar(b)
    if isinstance(a, str) and isinstance(b, str):
        return excel_casefold(a) == excel_casefold(b)
    an = to_number(a)
    bn = to_number(b)
    if not isinstance(an, XlError) and not isinstance(bn, XlError):
        return an == bn
    return a == b

def compare_scalars(op: str, left: FormulaValue, right: FormulaValue) -> bool | XlError:
    """Compare two scalar cell values using Excel coercion rules."""
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

    # Exact empty text compares as 0 (Excel); whitespace-only does not coerce.
    if isinstance(left, str) and left == "":
        left = 0.0
    if isinstance(right, str) and right == "":
        right = 0.0

    ln = to_number(left)
    rn = to_number(right)
    if isinstance(ln, XlError) or isinstance(rn, XlError):
        return _cmp_str(excel_casefold(to_string(left)), excel_casefold(to_string(right)))

    return _cmp_float(float(ln), float(rn))

def index_excel_range(
    base: ExcelRangeGeometry,
    row_num: FormulaValue | None,
    col_num: FormulaValue | None,
) -> ExcelRange | XlError:
    """Map INDEX(row,col) over *base* to an absolute range (single cell or slice).

    Mirrors `excel_grapher.runtime.lookup.xl_index` geometry
    so OFFSET(INDEX(...), ...) receives a true cell reference.

    A `row_num` or `col_num` of `0` selects the entire column or row. Both `0`
    returns the full `base` range.
    """
    nrows = base.end_row - base.start_row + 1
    ncols = base.end_col - base.start_col + 1
    row_omitted = row_num is None
    col_omitted = col_num is None

    def abs_cell(r0: int, c0: int) -> ExcelRange:
        r = base.start_row + r0
        c = base.start_col + c0
        return ExcelRange(base.sheet, r, c, r, c)

    def full_base() -> ExcelRange:
        return ExcelRange(base.sheet, base.start_row, base.start_col, base.end_row, base.end_col)

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
        if col == 0:
            return full_base()
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
        if row == 0:
            return full_base()
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
    if row == 0 and col == 0:
        return full_base()
    if row == 0:
        if col < 1 or col > ncols:
            return XlError.REF
        if nrows == 1:
            return abs_cell(0, col - 1)
        c0 = base.start_col + col - 1
        return ExcelRange(base.sheet, base.start_row, c0, base.end_row, c0)
    if col == 0:
        if row < 1 or row > nrows:
            return XlError.REF
        if ncols == 1:
            return abs_cell(row - 1, 0)
        r0 = base.start_row + row - 1
        return ExcelRange(base.sheet, r0, base.start_col, r0, base.end_col)
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

def match_cells(
    lookup_value: object,
    lookup_array: object,
    match_type: object = 1,
) -> int | XlError:
    """Excel MATCH over a lazy grid or nested-list array."""
    mt = to_number(cast(CellValue, match_type))
    if isinstance(mt, XlError):
        return mt
    match_type_int = int(mt)
    if isinstance(lookup_array, XlError):
        return lookup_array
    grid = Grid.wrap(lookup_array)
    if grid is None:
        grid_wrapped = Grid.wrap([[lookup_array]])
        assert grid_wrapped is not None
        grid = grid_wrapped
    if match_type_int == 0:
        for i in range(grid.size):
            if _values_match(lookup_value, grid.at_flat(i)):
                return i + 1
        return XlError.NA
    if match_type_int == 1:
        last_match = None
        for i in range(grid.size):
            if _compare_values(grid.at_flat(i), lookup_value) <= 0:
                last_match = i + 1
            else:
                break
        return XlError.NA if last_match is None else last_match
    if match_type_int == -1:
        last_match = None
        for i in range(grid.size):
            if _compare_values(grid.at_flat(i), lookup_value) >= 0:
                last_match = i + 1
            else:
                break
        return XlError.NA if last_match is None else last_match
    return XlError.VALUE

def to_int(value: FormulaValue) -> int | XlError:
    """Coerce a CellValue to an integer using Excel-style numeric coercion.

    For functions that operate on integer indices (e.g. CHOOSE/INDEX/MATCH)
    while propagating Excel errors.
    """
    n = to_number(value)
    if isinstance(n, XlError):
        return n
    return int(n)

def warn_circular_reference(*, stacklevel: int = 2) -> None:
    """Emit the standard circular-reference warning."""
    warnings.warn(
        "Circular reference detected; returning 0 (iterative calculation is disabled).",
        CircularReferenceWarning,
        stacklevel=stacklevel,
    )

def xl_bool(value: CellValue) -> bool:
    """Coerce a scalar cell value to a boolean, raising on Excel errors."""
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        raise _raise_error(scalar)
    boolean = to_bool(scalar)
    if isinstance(boolean, XlError):
        raise _raise_error(boolean)
    return boolean

def xl_circular_reference() -> CellValue:
    """Excel default behavior for circular references (non-iterative calculation)."""
    warn_circular_reference(stacklevel=2)
    return 0

def _evaluate_address(
    ctx: EvalContext,
    address: str,
    obtain_fn: Callable[[], Callable[[EvalContext], CellValue]],
    *,
    preserve_structural_blank: bool = False,
) -> CellValue:
    """Shared evaluation path for ``xl_cell`` and ``xl_eval``.

    Excel error values raise `XlErrorException`; the raising cell's error code
    is cached so re-reads raise without re-evaluating.
    """
    if ctx.stack:
        ctx._record_dependency(ctx.stack[-1], address)

    if address in ctx.cache:
        if address in ctx.circular_warning_roots:
            warn_circular_reference(stacklevel=3)
        return _raise_if_error_value(ctx.cache[address])

    if address in ctx.computing:
        if ctx.iterative_enabled:
            return ctx.iteration_values.get(address, 0)
        root = ctx.stack[0] if ctx.stack else address
        ctx.circular_warning_roots.add(root)
        return xl_circular_reference()

    found = lookup_leaf(ctx, address)
    if found is not MISSING:
        ctx.cache[address] = found
        return _raise_if_error_value(found)

    fn = obtain_fn()

    ctx.computing.add(address)
    ctx.stack.append(address)
    try:
        try:
            v = fn(ctx)
        except XlErrorException as exc:
            ctx.cache[address] = exc.code
            raise
        if v is None and not (
            preserve_structural_blank and getattr(fn, "__structural_blank__", False)
        ):
            v = 0
        ctx.cache[address] = v
        return _raise_if_error_value(v)
    finally:
        ctx.computing.discard(address)
        if ctx.stack and ctx.stack[-1] == address:
            ctx.stack.pop()

def xl_cell(ctx: EvalContext, address: str) -> CellValue:
    """Evaluate a single cell address under the given context.

    Resolution order:
    - cached value (per ctx)
    - leaf coordinate store (`ctx.leaves` / NodeKey overlays)
    - exported formula implementation (via resolver)
    - missing cell raises KeyError
    """

    def obtain_fn() -> Callable[[EvalContext], CellValue]:
        fn = ctx.resolver(address)
        if fn is None:
            raise KeyError(f"Cell {address} not found in graph")
        return fn

    return _evaluate_address(ctx, address, obtain_fn, preserve_structural_blank=True)

def _ctx_range(ctx: EvalContext, sheet: str, r1: int, c1: int, r2: int, c2: int) -> Range:
    # Leave the resolver unannotated: embed strips `excel_grapher.core` imports, so
    # aliases like `CellValue as CoreCellValue` never appear in generated runtime.py.
    def resolve(address: str):
        return xl_cell(ctx, address)

    def resolve_coord(row: int, col: int):
        found = leaf(ctx.leaves, sheet, row, col)
        if found is not MISSING:
            return found
        return xl_cell(ctx, _format_address(sheet, row, col))

    return Range(sheet, r1, c1, r2, c2, resolve, _coord_resolver=resolve_coord)

def xl_compare(op: str, left: CellValue, right: CellValue) -> bool:
    """Compare two scalar operands with Excel ordering rules."""
    result = compare_scalars(op, as_scalar(left), as_scalar(right))
    if isinstance(result, XlError):
        raise _raise_error(result)
    return result

def xl_eval(
    ctx: EvalContext,
    address: str,
    fn: Callable[[EvalContext], CellValue],
) -> CellValue:
    """Evaluate a known formula implementation under the given context."""
    return _evaluate_address(ctx, address, lambda: fn, preserve_structural_blank=False)

def xl_index_ref(
    ref: ExcelRange | OffsetRefInfo,
    row_num: CellValue | None,
    col_num: CellValue | None,
) -> OffsetRefInfo:
    """Return INDEX address metadata for OFFSET, not a cell value.

    Pass the result to `xl_offset` (or `xl_offset_ref`). Scalar INDEX reads are
    emitted as `xl_offset(ctx, xl_index_ref(...), 0, 0)`.

    Raises:
        XlErrorException: On Excel reference errors such as `#REF!`.
    """
    out = index_excel_range(
        _range_from_ref_info(ref),
        _as_addressing_scalar(row_num),
        _as_addressing_scalar(col_num),
    )
    if isinstance(out, XlError):
        raise XlErrorException(out)
    if out.start_row == out.end_row and out.start_col == out.end_col:
        return (out.sheet, out.start_row, out.start_col)
    return (out.sheet, out.start_row, out.start_col, out.end_row, out.end_col)

def xl_int(value: CellValue) -> int:
    """Coerce a scalar cell value to an integer, raising on Excel errors."""
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        raise _raise_error(scalar)
    integer = to_int(scalar)
    if isinstance(integer, XlError):
        raise _raise_error(integer)
    return integer

def xl_match(lookup_value: CellValue, lookup_array: CellValue, match_type: CellValue = 1) -> int:
    result = _raise_if_error(match_cells(lookup_value, lookup_array, match_type))
    return cast(int, result)

def xl_number(value: CellValue) -> float:
    """Coerce a scalar cell value to a number, raising on Excel errors."""
    scalar = as_scalar(value)
    if isinstance(scalar, XlError):
        raise _raise_error(scalar)
    number = to_number(scalar)
    if isinstance(number, XlError):
        raise _raise_error(number)
    return number

def xl_offset(
    ctx: EvalContext,
    ref_info: OffsetRefInfo,
    rows: CellValue,
    cols: CellValue,
    height: CellValue | None = None,
    width: CellValue | None = None,
) -> CellValue:
    """Evaluate OFFSET from `ref_info`, returning a cell value or range.

    `ref_info` is typically produced by `xl_index_ref`. For a scalar INDEX
    result use `rows=0`, `cols=0`.

    Raises:
        XlErrorException: On Excel reference or coercion errors.
    """
    rr = _number_or_raise(rows)
    cc = _number_or_raise(cols)

    match ref_info:
        case (sheet, base_row, base_col):
            base_end_row, base_end_col = base_row, base_col
        case (sheet, base_row, base_col, base_end_row, base_end_col):
            pass
        case _:
            raise XlErrorException(XlError.VALUE)

    base_h = int(base_end_row - base_row + 1)
    base_w = int(base_end_col - base_col + 1)

    h = base_h if height is None else int(_number_or_raise(height))
    w = base_w if width is None else int(_number_or_raise(width))

    target_row = int(base_row + int(rr))
    target_col = int(base_col + int(cc))

    if target_row < 1 or target_col < 1:
        raise XlErrorException(XlError.REF)
    if h <= 0 or w <= 0:
        raise XlErrorException(XlError.VALUE)

    if h == 1 and w == 1:
        addr = _format_address(sheet, target_row, target_col)
        # Scalar OFFSET results are CellValue; multi-cell returns a lazy Range.
        return cast("CellValue", xl_cell(ctx, addr))

    return _ctx_range(ctx, sheet, target_row, target_col, target_row + h - 1, target_col + w - 1)

def xl_raise(code: XlError) -> NoReturn:
    """Raise an Excel error code from an expression position."""
    raise XlErrorException(code)

def xl_range(ctx: EvalContext, address: str) -> CellValue:
    """Evaluate a sheet-qualified range address into a lazy `Range` value."""
    parsed = _parse_range_address(address)
    if isinstance(parsed, XlError):
        raise XlErrorException(parsed)
    sheet, start_cell, end_cell = parsed
    try:
        start_col, start_row = fastpyxl.utils.cell.coordinate_from_string(start_cell)
        end_col, end_row = fastpyxl.utils.cell.coordinate_from_string(end_cell)
        start_col_idx = fastpyxl.utils.cell.column_index_from_string(start_col)
        end_col_idx = fastpyxl.utils.cell.column_index_from_string(end_col)
    except ValueError:
        raise XlErrorException(XlError.VALUE) from None

    if start_row > end_row:
        start_row, end_row = end_row, start_row
    if start_col_idx > end_col_idx:
        start_col_idx, end_col_idx = end_col_idx, start_col_idx

    return _ctx_range(ctx, sheet, start_row, start_col_idx, end_row, end_col_idx)

def xl_range_rows(ctx: EvalContext, address: str) -> CellValue:
    """Evaluate a sheet-qualified range eagerly into nested row lists.

    Public boundary handler for range targets: results returned from
    `compute_all` are materialized values, not lazy range views.
    """
    rng = xl_range(ctx, address)
    if isinstance(rng, Range):
        return rng.rows_raw()
    return rng

# --- parameterized helper memoization ---

def _xl_freeze_helper_kwargs(kwargs):
    frozen = []
    for name in sorted(kwargs):
        value = kwargs[name]
        try:
            hash(value)
        except TypeError as error:
            raise TypeError(
                f"xl_helper kwargs must be hashable for memoization; "
                f"got {name}={value!r} of type {type(value).__name__}"
            ) from error
        frozen.append((name, value))
    return tuple(frozen)


_XL_SIDE_HELPER_CACHES = {}
_XL_SIDE_HELPER_COMPUTING = {}


def _xl_helper_maps(ctx):
    helper_cache = getattr(ctx, "helper_cache", None)
    helper_computing = getattr(ctx, "helper_computing", None)
    if isinstance(helper_cache, dict) and isinstance(helper_computing, set):
        return helper_cache, helper_computing
    ctx_id = id(ctx)
    return (
        _XL_SIDE_HELPER_CACHES.setdefault(ctx_id, {}),
        _XL_SIDE_HELPER_COMPUTING.setdefault(ctx_id, set()),
    )


def xl_helper(ctx, fn, /, **kwargs):
    """Evaluate a parameterized helper, memoized by ``(fn, kwargs)`` on ``ctx``."""
    key = (fn, _xl_freeze_helper_kwargs(kwargs))
    cache, computing = _xl_helper_maps(ctx)
    if key in cache:
        value = cache[key]
        if isinstance(value, XlError):
            raise XlErrorException(value)
        return value
    if key in computing:
        return xl_circular_reference()
    computing.add(key)
    try:
        try:
            value = fn(ctx, **kwargs)
        except XlErrorException as exc:
            cache[key] = exc.code
            raise
        cache[key] = value
        if isinstance(value, XlError):
            raise XlErrorException(value)
        return value
    finally:
        computing.discard(key)


def xl_memoize(fn):
    """Decorator routing a ``(ctx, **params)`` helper through :func:`xl_helper`."""
    import functools as _functools
    import inspect as _inspect

    @_functools.wraps(fn)
    def wrapper(ctx, /, *args, **kwargs):
        if args:
            bound = _inspect.signature(fn).bind(ctx, *args, **kwargs)
            bound.apply_defaults()
            param_kwargs = {
                name: value
                for name, value in bound.arguments.items()
                if name != "ctx"
            }
            return xl_helper(ctx, fn, **param_kwargs)
        return xl_helper(ctx, fn, **kwargs)

    wrapper.__wrapped__ = fn
    return wrapper


def _xl_patch_eval_context_invalidate():
    original = EvalContext.invalidate

    def invalidate(self, addresses):
        _XL_SIDE_HELPER_CACHES.pop(id(self), None)
        _XL_SIDE_HELPER_COMPUTING.pop(id(self), None)
        helper_cache = getattr(self, "helper_cache", None)
        helper_computing = getattr(self, "helper_computing", None)
        if isinstance(helper_cache, dict):
            helper_cache.clear()
        if isinstance(helper_computing, set):
            helper_computing.clear()
        return original(self, addresses)

    EvalContext.invalidate = invalidate


_xl_patch_eval_context_invalidate()
