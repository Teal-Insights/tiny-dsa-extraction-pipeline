import random
from typing import Annotated, Literal, Protocol, cast, get_args, get_origin

import fastpyxl
import fastpyxl.utils.cell
import pytest

from excel_grapher.core.cell_types import Between, RealBetween
from excel_grapher.core.address_keys import normalize_key
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher.resolver import NamedRangeMaps, build_named_range_map
from excel_grapher.grapher.parser import expand_range

from src.extraction_pipeline import constraints, graph, targets, workbook_path


class _ExcelWorksheet(Protocol):
    def Range(self, cell: str) -> object: ...


class _ExcelWorkbook(Protocol):
    def Worksheets(self, name: str) -> _ExcelWorksheet: ...
    def Close(self, save_changes: bool) -> None: ...


class _ExcelWorkbooks(Protocol):
    def Open(self, path: str) -> _ExcelWorkbook: ...


class _ExcelApplication(Protocol):
    Visible: bool
    DisplayAlerts: bool
    Workbooks: _ExcelWorkbooks

    def Calculate(self) -> None: ...
    def Quit(self) -> None: ...


class _ExcelRange(Protocol):
    Value: object


def test_all_leaf_cells_are_constrained():
    assert all([key in constraints.keys() for key in graph.leaf_keys()])
    assert all([key in graph.leaf_keys() for key in constraints.keys()])


def test_formula_evaluator_matches_excel_for_default_inputs():
    """
    For each target, we evaluate the formula in the workbook and compare
    the result to the cached value in the workbook.
    """
    wb_formulas = fastpyxl.load_workbook(workbook_path, data_only=False)
    wb_values = fastpyxl.load_workbook(workbook_path, data_only=True)
    maps = build_named_range_map(wb_formulas)

    with FormulaEvaluator(graph) as ev:
        for target in targets:
            sheet, start_a1, end_a1 = maps.range_map[target]
            start_col, start_row = fastpyxl.utils.cell.coordinate_from_string(start_a1)
            end_col, end_row = fastpyxl.utils.cell.coordinate_from_string(end_a1)
            for dep_sheet, dep_a1 in expand_range(
                sheet=sheet,
                start_col=start_col,
                start_row=int(start_row),
                end_col=end_col,
                end_row=int(end_row),
                max_cells=5000,
            ):
                expected = wb_values[dep_sheet][dep_a1].value
                assert ev.evaluate(f"{dep_sheet}!{dep_a1}") == pytest.approx(
                    expected
                ), f"Cell {dep_sheet}!{dep_a1} value mismatch"


@pytest.mark.skipped
def test_formula_evaluator_matches_excel_for_randomized_inputs():
    def _sample_constraint(rng: random.Random, constraint: object) -> object:
        origin = get_origin(constraint)
        if origin is Literal:
            return rng.choice(get_args(constraint))

        if origin is Annotated:
            _, *metadata = get_args(constraint)
            for meta in metadata:
                if isinstance(meta, Between):
                    if meta.min is None or meta.max is None:
                        raise ValueError("Between constraints must include min and max")
                    return rng.randint(meta.min, meta.max)
                if isinstance(meta, RealBetween):
                    if meta.min is None or meta.max is None:
                        raise ValueError(
                            "RealBetween constraints must include min and max"
                        )
                    value = rng.uniform(float(meta.min), float(meta.max))
                    return round(value, 6)
            raise ValueError(f"Unsupported Annotated constraint: {constraint!r}")

        raise ValueError(f"Unsupported constraint type: {constraint!r}")

    def _collect_output_addresses(maps: NamedRangeMaps) -> list[str]:
        output_addresses: list[str] = []
        for target in targets:
            sheet, start_a1, end_a1 = maps.range_map[target]
            start_col, start_row = fastpyxl.utils.cell.coordinate_from_string(start_a1)
            end_col, end_row = fastpyxl.utils.cell.coordinate_from_string(end_a1)
            for dep_sheet, dep_a1 in expand_range(
                sheet=sheet,
                start_col=start_col,
                start_row=int(start_row),
                end_col=end_col,
                end_row=int(end_row),
                max_cells=5000,
            ):
                output_addresses.append(f"{dep_sheet}!{dep_a1}")
        return output_addresses

    win32 = pytest.importorskip(
        "win32com.client", reason="requires pywin32 for Excel COM automation"
    )

    wb_formulas = fastpyxl.load_workbook(workbook_path, data_only=False)
    maps = build_named_range_map(wb_formulas)
    output_addresses = _collect_output_addresses(maps)
    rng = random.Random(0)
    graph_keys = set(graph)
    missing_constraint_addresses = sorted(
        [address for address in constraints if normalize_key(address) not in graph_keys]
    )
    assert not missing_constraint_addresses, (
        "Constrained addresses missing from graph after normalization: "
        f"{missing_constraint_addresses}"
    )
    graph_addresses = sorted(constraints)
    original_leaf_values: dict[str, object] = {}
    for address in graph_addresses:
        node = graph.get_node(address)
        assert node is not None, (
            f"Expected graph node for constrained address: {address}"
        )
        original_leaf_values[address] = node.value

    app: _ExcelApplication | None = None
    book: _ExcelWorkbook | None = None
    sheet_cache: dict[str, _ExcelWorksheet] = {}
    try:
        try:
            app = cast(_ExcelApplication, win32.DispatchEx("Excel.Application"))
            app.Visible = False
            app.DisplayAlerts = False
            book = app.Workbooks.Open(str(workbook_path.resolve()))
        except Exception as exc:  # pragma: no cover - environment-specific
            pytest.skip(f"Excel is not available for COM automation: {exc}")

        def _sheet(name: str) -> _ExcelWorksheet:
            worksheet = sheet_cache.get(name)
            if worksheet is None:
                assert book is not None
                worksheet = book.Worksheets(name)
                sheet_cache[name] = worksheet
            return worksheet

        with FormulaEvaluator(graph) as ev:
            for _ in range(5):
                sample = {
                    address: _sample_constraint(rng, constraint)
                    for address, constraint in constraints.items()
                }
                for address, value in sample.items():
                    graph.set_node_value(address, value)
                    sheet_name, cell = address.split("!", 1)
                    cast(_ExcelRange, _sheet(sheet_name).Range(cell)).Value = value

                app.Calculate()
                for output_address in output_addresses:
                    sheet_name, cell = output_address.split("!", 1)
                    expected = cast(_ExcelRange, _sheet(sheet_name).Range(cell)).Value
                    actual = ev.evaluate(output_address)
                    if isinstance(expected, (int, float)) and isinstance(
                        actual, (int, float)
                    ):
                        assert actual == pytest.approx(expected), (
                            f"Cell {output_address} mismatch; expected {expected}, got {actual}"
                        )
                    else:
                        assert actual == expected, (
                            f"Cell {output_address} mismatch; expected {expected}, got {actual}"
                        )
    finally:
        for address, value in original_leaf_values.items():
            graph.set_node_value(address, value)
        if book is not None:
            book.Close(False)
        if app is not None:
            app.Quit()
        sheet_cache.clear()
