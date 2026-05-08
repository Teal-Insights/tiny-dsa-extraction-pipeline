from pathlib import Path
from typing import Literal

from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
    create_dependency_graph,
    list_dynamic_ref_constraint_candidates,
)


# Load the Tiny DSA workbook
workbook_path = Path("data/tiny-dsa.xlsx")

targets = ["output_baseline", "output_shocked", "output_delta"]

list_dynamic_ref_constraint_candidates(workbook_path, targets)

constraints = {
    "Inputs!A10": Literal["Borvelia"],
    "Inputs!A11": Literal["Litellia"],
    "Inputs!A12": Literal["Aurelium"],
    "Inputs!B22": Literal[1, 2, 3],
    "Inputs!B5": Literal["Borvelia", "Litellia", "Aurelium"],
}

config = DynamicRefConfig.from_constraints(constraints, {})
graph: DependencyGraph = create_dependency_graph(
    workbook_path,
    targets,
    load_values=True,
    dynamic_refs=config,
)
