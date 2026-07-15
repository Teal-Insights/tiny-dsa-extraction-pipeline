"""Workbook-specific configuration for the Tiny DSA extraction project.

Edit values here before running the pipeline. See README.md for the
configure → extract → export → test → document → refactor workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from excel_grapher.core.cell_types import Between, RealBetween

from src.graph_dependency_audit import GraphAuditCase
from src.pipeline_config import DistProjectMetadata
from src.internal_binding_coverage import InternalBindingValidationMode
from src.workbook_addresses import ProjectionColumnLayout

REPO_ROOT = Path(__file__).resolve().parent

WORKBOOK_PATH = REPO_ROOT / "data" / "tiny-dsa.xlsx"
GUIDE_PATH = REPO_ROOT / "data" / "tiny-dsa-guide.md"
BINDINGS_PATH = REPO_ROOT / "bindings"

TARGETS: list[str] = ["output_baseline", "output_shocked", "output_delta"]

_cols = ("C", "D", "E", "F", "G")

CONSTRAINTS: dict[str, object] = {
    "Inputs!A10": Literal["Borvelia"],
    "Inputs!A11": Literal["Litellia"],
    "Inputs!A12": Literal["Aurelium"],
    "Inputs!B22": Literal[1, 2, 3],
    "Inputs!B5": Literal["Borvelia", "Litellia", "Aurelium"],
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

# Optional: exact dropdown label literals for enum public inputs, keyed by the
# public input cell address. Populate when the workbook uses IF/MATCH/CHOOSE
# guards that compare against reference label cells. Used by configure tests and
# differential harness label resolution (see tests/differential/workbook_labels.py).
REFERENCE_LABEL_CELLS: dict[str, tuple[str, ...]] = {}

# Optional: scenario logical values per enum public input, keyed by input cell.
# Used by tests/test_workbook_labels.py when REFERENCE_LABEL_CELLS is populated.
REFERENCE_LABEL_SCENARIO_VALUES: dict[str, tuple[str, ...]] = {}

DIST_METADATA = DistProjectMetadata(
    project_name="tiny-dsa",
    package_name="tiny_dsa",
    library_name="Tiny DSA",
    description=(
        "A Python implementation of the Tiny-DSA Excel workbook, a stylized "
        "debt-sustainability tool for computing the debt-to-GDP ratio over a "
        "five-year horizon with one configurable shock."
    ),
    documentation_url="https://teal-insights.github.io/py-tiny-dsa/",
    repository_url="https://github.com/Teal-Insights/py-tiny-dsa",
    attribution=(
        "Created by Teal Insights.\n\n![Teal Insights logo](README_files/logo.png)"
    ),
)

DOCSTRING_CALLBACK_NAME = "series_docs"

PROJECTION_LAYOUT = ProjectionColumnLayout(
    engine_sheet="Engine",
    engine_columns=("C", "D", "E", "F", "G"),
    outputs_sheet="Outputs",
    outputs_column_to_engine={
        "B": "C",
        "C": "D",
        "D": "E",
        "E": "F",
        "F": "G",
    },
    time_period_to_engine_column={
        1: "C",
        2: "D",
        3: "E",
        4: "F",
        5: "G",
    },
    # Effective dimension id for the projection axis (defaults to TIME_PERIOD).
    # Use an explicit id when bindings distinguish projection from other
    # TIME_PERIOD dimensions, e.g. projection_dimension_id="PROJECTION_PERIOD".
)

DIFFERENTIAL_WORKBOOK_REL = Path("data/tiny-dsa.xlsx")
DIFFERENTIAL_REPORT_DIR_REL = Path("data/differential/exported_library")
DIFFERENTIAL_GRAPH_REPORT_DIR_REL = Path("data/differential/graph")

# Optional per-parent formula cells for LLM direct-dependency graph audits
# (``pytest tests/test_extraction_graph_accuracy.py --run-skipped``). Loaded
# through :func:`src.pipeline_config.load_pipeline_config` as
# ``PipelineConfig.graph_audit_cases``. Provider and model come from
# ``LLM_GRAPH_AUDIT_MODEL`` (see ``.env.example``).
GRAPH_AUDIT_CASES: tuple[GraphAuditCase, ...] = ()

# Optional extra target bundles for ``scripts/regenerate_graph_cache.py``.
# Each entry is ``(label, targets)``. The primary bundle always uses TARGETS.
GRAPH_CACHE_TARGET_BUNDLES: tuple[tuple[str, tuple[str, ...]], ...] = ()

# Internal binding coverage validation for formula graph cells (see README.md).
# off: disabled; warn: pipeline logs warnings; error: pipeline raises before export.
INTERNAL_BINDING_VALIDATION_MODE: InternalBindingValidationMode = "warn"
# Sheet-qualified formula addresses reviewed and intentionally allowed to remain unbound.
INTERNAL_BINDING_EXEMPT_CELLS: frozenset[str] = frozenset()

# Formula-cluster variation mode for internals refactor (independent or dominant_key_only).
# Override per run with ``--variation-mode`` on ``src.extraction_pipeline``.
VARIATION_MODE = "independent"

# Formula-cluster base mode for internals refactor (series, series_ast, or ast).
# Override per run with ``--clustering-mode`` on ``src.extraction_pipeline``.
CLUSTERING_MODE = "series_ast"

# Optional hooks for ``uv run python -m src.workbook_audit`` (pre-extraction audit).
AUDIT_TITLE = "Tiny DSA Workbook Audit"
AUDIT_PUBLIC_INPUTS: tuple[tuple[str, str, str], ...] = ()
AUDIT_GUIDE_USE_CASES: tuple[tuple[str, str, str], ...] = ()
