from pathlib import Path
from typing import Annotated, Iterable, Literal, Mapping, get_args, get_origin

from excel_grapher.core.cell_types import Between, RealBetween
from excel_grapher.exporter import CodeGenerator
from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
    create_dependency_graph,
)
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
    validate_series_bindings,
)

from src.docstring_callback import available_docstring_callback
from src.qmd_python_validation import (
    render_dist_pyproject_toml,
    DOCUMENTATION_BASELINE_DEV_DEPS,
)

repo_root = Path(__file__).resolve().parents[1]
workbook_path = repo_root / "data/tiny-dsa.xlsx"
bindings_path = repo_root / "bindings"
dist_root = repo_root / "dist"
package_root = dist_root / "tiny_dsa"

targets = ["output_baseline", "output_shocked", "output_delta"]
series_bindings = load_series_bindings(bindings_path)

# ------------------------------------------------------------
# Constraint configuration
# ------------------------------------------------------------

_cols = ("C", "D", "E", "F", "G")

required_constraints = {
    "Inputs!A10": Literal["Borvelia"],
    "Inputs!A11": Literal["Litellia"],
    "Inputs!A12": Literal["Aurelium"],
    "Inputs!B22": Literal[1, 2, 3],
    "Inputs!B5": Literal["Borvelia", "Litellia", "Aurelium"],
}

constraints = required_constraints | {
    "Engine!C5": Literal[1],
    "Engine!D5": Literal[2],
    "Engine!E5": Literal[3],
    "Engine!F5": Literal[4],
    "Engine!G5": Literal[5],
    "Inputs!B10": Annotated[float, RealBetween(0.0, 200.0)],
    "Inputs!B11": Annotated[float, RealBetween(0.0, 200.0)],
    "Inputs!B12": Annotated[float, RealBetween(0.0, 200.0)],
    "Inputs!B21": Annotated[int, Between(1, 5)],
    "Inputs!B26": Annotated[float, RealBetween(-30.0, 30.0)],
    "Inputs!C26": Annotated[float, RealBetween(-30.0, 30.0)],
    "Inputs!D26": Annotated[float, RealBetween(-30.0, 30.0)],
    **{f"Inputs!{c}16": Annotated[float, RealBetween(-10.0, 15.0)] for c in _cols},
    **{f"Inputs!{c}17": Annotated[float, RealBetween(0.0, 20.0)] for c in _cols},
    **{f"Inputs!{c}18": Annotated[float, RealBetween(-15.0, 15.0)] for c in _cols},
}

LeafKind = Literal["input", "constant"]


def is_constant_constraint(constraint: object) -> bool:
    """True when the constraint fixes a single value (lookup/structural data)."""
    return get_origin(constraint) is Literal and len(get_args(constraint)) == 1


def classify_leaves_from_constraints(
    constraint_map: Mapping[str, object],
    leaf_keys: Iterable[str],
) -> dict[str, str]:
    """Classify graph leaves as inputs or constants from their constraints."""
    keys = list(leaf_keys)
    missing = [key for key in keys if key not in constraint_map]
    if missing:
        raise KeyError(f"missing constraints for leaf cells: {missing}")
    return {
        key: "constant" if is_constant_constraint(constraint_map[key]) else "input"
        for key in keys
    }


# ------------------------------------------------------------
# Extract the graph
# ------------------------------------------------------------

config = DynamicRefConfig.from_constraints(constraints, {})

graph: DependencyGraph = create_dependency_graph(
    workbook_path,
    targets,
    load_values=True,
    dynamic_refs=config,
)

binding_validation_report = validate_series_bindings(
    graph,
    series_bindings,
    workbook=workbook_path,
)
if not binding_validation_report["ok"]:
    raise ValueError(
        f"Invalid series bindings: {binding_validation_report['issues']!r}"
    )

input_series = derive_input_series(graph, series_bindings, workbook=workbook_path)
output_series = derive_output_series(graph, series_bindings, workbook=workbook_path)


# ------------------------------------------------------------
# Export the graph to Python code
# ------------------------------------------------------------

leaf_classification = classify_leaves_from_constraints(constraints, graph.leaf_keys())
graph.leaf_classification = leaf_classification

with CodeGenerator(graph) as generator:
    modules = generator.generate_modules(
        targets,
        series_bindings=series_bindings,
        bindings_workbook=workbook_path,
        series_docstring_callback=available_docstring_callback(),
        docstring_renderer="google",
    )

package_root.mkdir(parents=True, exist_ok=True)

GENERATED_MODULE_NAMES = frozenset(
    {"__init__.py", "api.py", "data.py", "runtime.py", "internals.py"}
)

for filepath, code in modules.items():
    output_path = package_root / filepath
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(code, encoding="utf-8")

for stale_module in GENERATED_MODULE_NAMES:
    stale_path = dist_root / stale_module
    if stale_path.is_file():
        stale_path.unlink()

gitignore_content = """
*.egg-info/
*.pyc
__pycache__/
.venv/
_validate_user_guide_cells.py
"""

(dist_root / ".gitignore").write_text(gitignore_content, encoding="utf-8")
(dist_root / "pyproject.toml").write_text(
    render_dist_pyproject_toml(
        dev_dependencies=list(DOCUMENTATION_BASELINE_DEV_DEPS),
    ),
    encoding="utf-8",
)


def main() -> None:
    from src.documentation_pipeline import run_documentation_pipeline

    run_documentation_pipeline()


if __name__ == "__main__":
    main()
