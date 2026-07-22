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

from tests.conftest import SyntheticConfiguredPipeline

load_dotenv()

REQUIRED_CONSTRAINT_KEYS = frozenset(
    {
        "Inputs!A10",
        "Inputs!A11",
        "Inputs!A12",
        "Inputs!B22",
        "Inputs!B5",
    }
)


def test_all_leaf_cells_are_constrained(tiny_dsa_configured_pipeline):
    pipeline = tiny_dsa_configured_pipeline
    graph = pipeline.graph
    constraints = pipeline.config.constraints
    assert all(key in constraints for key in graph.leaf_keys())
    assert all(key in graph.leaf_keys() for key in constraints)


def test_all_leaf_cells_are_classified(tiny_dsa_configured_pipeline):
    pipeline = tiny_dsa_configured_pipeline
    graph = pipeline.graph
    leaf_classification = pipeline.leaf_classification
    assert all(key in leaf_classification for key in graph.leaf_keys())
    assert all(key in graph.leaf_keys() for key in leaf_classification)


EXPECTED_CONSTANT_LEAVES = frozenset(
    {
        "Engine!C5",
        "Engine!D5",
        "Engine!E5",
        "Engine!F5",
        "Engine!G5",
        "Inputs!A10",
        "Inputs!A11",
        "Inputs!A12",
    }
)
EXPECTED_INPUT_LEAVES = frozenset(
    {
        "Inputs!B5",
        "Inputs!B10",
        "Inputs!B11",
        "Inputs!B12",
        "Inputs!B21",
        "Inputs!B22",
        "Inputs!B26",
        "Inputs!C16",
        "Inputs!C17",
        "Inputs!C18",
        "Inputs!C26",
        "Inputs!D16",
        "Inputs!D17",
        "Inputs!D18",
        "Inputs!D26",
        "Inputs!E16",
        "Inputs!E17",
        "Inputs!E18",
        "Inputs!F16",
        "Inputs!F17",
        "Inputs!F18",
        "Inputs!G16",
        "Inputs!G17",
        "Inputs!G18",
    }
)


def test_leaf_classification(tiny_dsa_configured_pipeline):
    graph = tiny_dsa_configured_pipeline.graph
    leaf_classification = tiny_dsa_configured_pipeline.leaf_classification
    assert EXPECTED_CONSTANT_LEAVES | EXPECTED_INPUT_LEAVES == frozenset(
        graph.leaf_keys()
    )
    for address in EXPECTED_CONSTANT_LEAVES:
        assert leaf_classification[address] == "constant"
    for address in EXPECTED_INPUT_LEAVES:
        assert leaf_classification[address] == "input"


@pytest.mark.skipped(reason="Opt-in test; pass --run-skipped to run")
def test_llm_judges_that_graph_is_correct(tiny_dsa_configured_pipeline):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set")
    openai = pytest.importorskip(
        "openai", reason="requires openai for LLM-based testing"
    )

    pipeline = tiny_dsa_configured_pipeline
    graph = pipeline.graph
    constraints = pipeline.config.constraints
    required_constraints = {
        key: constraints[key] for key in REQUIRED_CONSTRAINT_KEYS if key in constraints
    }
    client = openai.OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"), base_url="https://api.openai.com/v1/"
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
        model="gpt-5.5",
        messages=[
            {"role": "user", "content": prompt},
        ],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )

    assert response.choices[0].message.content.strip().upper() == "CORRECT"


def test_formula_evaluator_matches_excel_for_default_inputs(
    tiny_dsa_configured_pipeline: SyntheticConfiguredPipeline,
):
    pipeline = tiny_dsa_configured_pipeline
    graph = pipeline.graph
    targets = list(pipeline.config.targets)
    workbook_path = pipeline.config.workbook_path
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


def test_formula_evaluator_matches_excel_for_randomized_inputs(
    tiny_dsa_configured_pipeline: SyntheticConfiguredPipeline,
):
    pipeline = tiny_dsa_configured_pipeline
    graph = pipeline.graph
    constraints = pipeline.config.constraints
    targets = list(pipeline.config.targets)
    workbook_path = pipeline.config.workbook_path

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
