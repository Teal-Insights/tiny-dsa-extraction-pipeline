import os
import random
from typing import Annotated, Any, Literal, get_args, get_origin

from dotenv import load_dotenv
import fastpyxl
import fastpyxl.utils.cell
import pytest

from excel_grapher.core.cell_types import Between, RealBetween
from excel_grapher.core.address_keys import normalize_key
from excel_grapher.evaluator import FormulaEvaluator
from excel_grapher.grapher import to_mermaid
from excel_grapher.grapher.resolver import NamedRangeMaps, build_named_range_map
from excel_grapher.grapher.parser import expand_range

from src.extraction_pipeline import (
    required_constraints,
    constraints,
    graph,
    targets,
    workbook_path,
)

load_dotenv()


def test_all_leaf_cells_are_constrained():
    assert all([key in constraints.keys() for key in graph.leaf_keys()])
    assert all([key in graph.leaf_keys() for key in constraints.keys()])


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_that_graph_is_correct():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not set")
    openai = pytest.importorskip(
        "openai", reason="requires openai for LLM-based testing"
    )

    client = openai.OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com"
    )

    targets = ["Outputs!B12:F12", "Outputs!B13:F13", "Outputs!B14:F14"]
    prompt = """
Given these targets in an Excel workbook, we want to extract a graph
of all possible dependencies of the targets:

{targets}

We set the following constraints on user inputs affecting dependency
resolution:

{required_constraints}

Give these constraints, is the following graph correct? Does it contain
all nodes and edges it should, and none that it shouldn't? Just say
CORRECT or INCORRECT.

Graph:
```mermaid
{mermaid_graph}
```
""".format(
        targets=targets,
        required_constraints=required_constraints,
        mermaid_graph=to_mermaid(graph),
    )

    response = client.chat.completions.create(
        model="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": prompt},
        ],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )

    assert response.choices[0].message.content.strip().upper() == "CORRECT"


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

    xw = pytest.importorskip("xlwings", reason="requires xlwings for Excel automation")

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

    app: Any | None = None
    book: Any | None = None
    sheet_cache: dict[str, Any] = {}
    try:
        try:
            app = xw.App(visible=False, add_book=False)
            app.display_alerts = False
            app.screen_updating = False
            book = app.books.open(str(workbook_path.resolve()))
        except Exception as exc:  # pragma: no cover - environment-specific
            pytest.skip(f"Excel is not available for xlwings automation: {exc}")

        def _sheet(name: str) -> Any:
            worksheet = sheet_cache.get(name)
            if worksheet is None:
                assert book is not None
                worksheet = book.sheets[name]
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
                    _sheet(sheet_name).range(cell).value = value

                app.calculate()
                for output_address in output_addresses:
                    sheet_name, cell = output_address.split("!", 1)
                    expected = _sheet(sheet_name).range(cell).value
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
        sheet_cache.clear()
        if book is not None:
            book.close()
        if app is not None:
            app.quit()
