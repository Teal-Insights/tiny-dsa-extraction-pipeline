"""Workbook-specific configuration for the Tiny DSA extraction project.

Edit values here before running the pipeline. See README.md for the
extract → export → annotate → validate → document workflow.
"""

from __future__ import annotations

from pathlib import Path

from src.graph_dependency_audit import GraphAuditCase
from src.internal_binding_coverage import InternalBindingValidationMode
from src.pipeline_config import (
    DistProjectMetadata,
    RunnableCellRule,
)

REPO_ROOT = Path(__file__).resolve().parent

WORKBOOK_PATH = REPO_ROOT / "data" / "tiny-dsa.xlsx"
GUIDE_PATH = REPO_ROOT / "data" / "tiny-dsa-guide.md"
BINDINGS_PATH = REPO_ROOT / "bindings"

TARGETS: list[str] = ["output_baseline", "output_shocked", "output_delta"]

# Sheet-qualified A1 rectangles of structurally empty cells that formulas name
# but users never fill (INDEX/MATCH padding, NPV/SUM year-window overflow,
# unused ladder copies, separator rows). Passed unchanged to graph build,
# FormulaEvaluator, and CodeGenerator. Do not put user-fillable slots here.
# Single cells are 1×1 rectangles. Never a bare string.
BLANK_RANGES: tuple[str, ...] = ()

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

DIFFERENTIAL_WORKBOOK_REL = Path("data/tiny-dsa.xlsx")
DIFFERENTIAL_REPORT_DIR_REL = Path("data/differential/exported_library")
DIFFERENTIAL_GRAPH_REPORT_DIR_REL = Path("data/differential/graph")

# Optional per-parent formula cells that steer LLM direct-dependency graph audits
# (``pytest tests/test_extraction_graph_accuracy.py --run-skipped``). Empty means
# auto-select from the warm graph. Declared cases overlay discovery: ``required``
# pins always run first; labels/focuses win for matching keys. Loaded through
# :func:`src.pipeline_config.load_pipeline_config` as
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

RUNNABLE_CELL_RULES: tuple[RunnableCellRule, ...] = (
    RunnableCellRule(
        pattern=r"\bmake_context\s*\(",
        message=(
            "inverted-tree runnable cells must call keyword-only compute_* "
            "helpers, not make_context()"
        ),
    ),
    RunnableCellRule(
        pattern=r"\bset_[A-Za-z_][A-Za-z0-9_]*\s*\(",
        message="inverted-tree runnable cells must not call set_* setters",
    ),
)

# Optional hooks for ``uv run python -m src.workbook_audit`` (pre-extraction audit).
AUDIT_TITLE = "Tiny DSA Workbook Audit"
AUDIT_PUBLIC_INPUTS: tuple[tuple[str, str, str], ...] = ()
AUDIT_GUIDE_USE_CASES: tuple[tuple[str, str, str], ...] = ()
