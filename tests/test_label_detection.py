from __future__ import annotations

import pytest

from excel_grapher.grapher import (
    DynamicRefConfig,
    LabelDetectionConfig,
    create_dependency_graph,
)

from src.extraction_pipeline import required_constraints, targets, workbook_path


def _year_label(col: str) -> str:
    return {"C": "1", "D": "2", "E": "3", "F": "4", "G": "5", "B": "1"}[col]


@pytest.fixture(scope="module")
def graph_with_labels():
    dynamic_refs = DynamicRefConfig.from_constraints(required_constraints, {})
    return create_dependency_graph(
        workbook_path,
        targets,
        load_values=True,
        dynamic_refs=dynamic_refs,
        label_detection=LabelDetectionConfig(enabled=True),
    )


EXPECTED_LABELS: dict[str, tuple[list[str], list[str], list[str]]] = {
    # Inputs: country/profile/shock tables and baseline vectors
    "Inputs!B5": (["Country name"], [], ["COUNTRY SELECTOR"]),
    "Inputs!B6": (["Initial debt-to-GDP (% of GDP)"], [], ["COUNTRY SELECTOR"]),
    "Inputs!A10": (
        ["stylized emerging market"],
        ["Country"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!A11": (
        ["stylized HIPC"],
        ["Country"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!A12": (
        ["stylized advanced economy"],
        ["Country"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!B10": (
        ["Borvelia", "stylized emerging market"],
        ["Initial debt (% of GDP)"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!B11": (
        ["Litellia", "stylized HIPC"],
        ["Initial debt (% of GDP)"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!B12": (
        ["Aurelium", "stylized advanced economy"],
        ["Initial debt (% of GDP)"],
        ["COUNTRY PROFILE TABLE (lookup data)"],
    ),
    "Inputs!B21": (["Shock year (integer, 1 to 5)"], [], ["SHOCK CONFIGURATION"]),
    "Inputs!B22": (
        ["Shock type (1=growth, 2=interest, 3=primary balance)"],
        [],
        ["SHOCK CONFIGURATION"],
    ),
    "Inputs!B26": (
        ["Magnitude"],
        ["Growth (pp)"],
        ["SHOCK TABLE (lookup by shock type, in percentage points)"],
    ),
    "Inputs!C26": (
        ["Magnitude"],
        ["Interest (pp)"],
        ["SHOCK TABLE (lookup by shock type, in percentage points)"],
    ),
    "Inputs!D26": (
        ["Magnitude"],
        ["Primary balance (pp)"],
        ["SHOCK TABLE (lookup by shock type, in percentage points)"],
    ),
}

for col in ("C", "D", "E", "F", "G"):
    year = _year_label(col)
    EXPECTED_LABELS[f"Inputs!{col}16"] = (
        ["Real GDP growth (% per annum)"],
        [year],
        ["BASELINE PARAMETERS"],
    )
    EXPECTED_LABELS[f"Inputs!{col}17"] = (
        ["Real interest rate (% per annum)"],
        [year],
        ["BASELINE PARAMETERS"],
    )
    EXPECTED_LABELS[f"Inputs!{col}18"] = (
        ["Primary balance (% of GDP)"],
        [year],
        ["BASELINE PARAMETERS"],
    )

# Engine: baseline/shock sections
EXPECTED_LABELS["Engine!B6"] = (["Debt-to-GDP (%)"], ["0"], ["BASELINE PATH"])
EXPECTED_LABELS["Engine!B9"] = (
    ["Shock magnitude (pp), via OFFSET on shock_type"],
    [],
    ["SHOCK ACTIVATION"],
)

for col in ("C", "D", "E", "F", "G"):
    year = _year_label(col)
    EXPECTED_LABELS[f"Engine!{col}5"] = (["Year"], [], ["BASELINE PATH"])
    EXPECTED_LABELS[f"Engine!{col}6"] = (["Debt-to-GDP (%)"], [year], ["BASELINE PATH"])
    EXPECTED_LABELS[f"Engine!{col}10"] = (
        ["Shock active (1 if year >= shock_year, else 0)"],
        [year],
        ["SHOCK ACTIVATION"],
    )
    EXPECTED_LABELS[f"Engine!{col}14"] = (
        ["Real GDP growth, shocked (%)"],
        [year],
        ["SHOCKED PARAMETERS"],
    )
    EXPECTED_LABELS[f"Engine!{col}15"] = (
        ["Real interest rate, shocked (%)"],
        [year],
        ["SHOCKED PARAMETERS"],
    )
    EXPECTED_LABELS[f"Engine!{col}16"] = (
        ["Primary balance, shocked (% of GDP)"],
        [year],
        ["SHOCKED PARAMETERS"],
    )
    EXPECTED_LABELS[f"Engine!{col}20"] = (
        ["Debt-to-GDP (%)"],
        [year],
        ["SHOCKED PATH"],
    )

# Outputs: trajectory table
for col in ("B", "C", "D", "E", "F"):
    year = {
        "B": "1",
        "C": "2",
        "D": "3",
        "E": "4",
        "F": "5",
    }[col]
    EXPECTED_LABELS[f"Outputs!{col}12"] = (
        ["Baseline (% of GDP)"],
        [year],
        ["DEBT-TO-GDP TRAJECTORY"],
    )
    EXPECTED_LABELS[f"Outputs!{col}13"] = (
        ["Shocked (% of GDP)"],
        [year],
        ["DEBT-TO-GDP TRAJECTORY"],
    )
    EXPECTED_LABELS[f"Outputs!{col}14"] = (
        ["Delta (shocked − baseline, pp)"],
        [year],
        ["DEBT-TO-GDP TRAJECTORY"],
    )


@pytest.mark.parametrize("address", sorted(EXPECTED_LABELS.keys()))
def test_expected_labels_for_graph_nodes(graph_with_labels, address: str):
    expected_row_labels, expected_column_labels, expected_table_labels = (
        EXPECTED_LABELS[address]
    )
    node = graph_with_labels.get_node(address)

    assert node is not None, f"Missing graph node: {address}"
    assert node.metadata.get("row_labels", []) == expected_row_labels
    assert node.metadata.get("column_labels", []) == expected_column_labels
    assert node.metadata.get("table_labels", []) == expected_table_labels
