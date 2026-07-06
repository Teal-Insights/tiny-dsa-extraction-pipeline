"""Synthetic workbook and pipeline config for graph-backed smoke tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Sequence
from unittest.mock import patch

import fastpyxl
from excel_grapher.core.cell_types import RealBetween
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher import DependencyGraph
from excel_grapher.series_bindings import WorkbookSeriesBindings, load_series_bindings

from src.extraction_pipeline import build_pipeline_graph
from src.graph_dependency_audit import GraphAuditCase
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.semantic_labeling import SemanticLabelingSummary
from src.subgraph_projection import build_refactor_projection
from src.workbook_addresses import ProjectionColumnLayout

FIXTURES_ROOT = Path(__file__).resolve().parent / "synthetic"
BINDINGS_PATH = FIXTURES_ROOT

TARGETS: tuple[str, ...] = ("Outputs!B1", "Outputs!C1")

CONSTRAINTS: dict[str, object] = {
    "Inputs!A1": Annotated[float, RealBetween(0.0, 100.0)],
    "Inputs!B1": Literal[0],
}

PROJECTION_LAYOUT = ProjectionColumnLayout(
    engine_sheet="Engine",
    engine_columns=("B", "C"),
    outputs_sheet="Outputs",
    outputs_column_to_engine={"B": "B", "C": "C"},
    time_period_to_engine_column={1: "B", 2: "C"},
)

# Synthetic audit catalog; wired into synthetic_pipeline_config as graph_audit_cases.
GRAPH_AUDIT_CASES: tuple[GraphAuditCase, ...] = (
    GraphAuditCase(
        parent_key="Outputs!B1",
        label="result_a_output",
        focus="First output should depend on the Engine column B path.",
        required=True,
    ),
    GraphAuditCase(
        parent_key="Engine!B2",
        label="engine_b2_formula",
        focus="Engine B2 should depend on both scalar inputs.",
    ),
)

STUB_SEMANTIC_LABELING_SUMMARY = SemanticLabelingSummary(
    labeled_cell_count=0,
    sheet_count=0,
    candidate_cells_by_sheet={},
)


@contextmanager
def stub_semantic_labeling():
    """Patch semantic labeling during synthetic pipeline graph builds."""
    with patch(
        "src.extraction_pipeline.label_internal_graph_cells",
        return_value=STUB_SEMANTIC_LABELING_SUMMARY,
    ):
        yield


def build_synthetic_pipeline_graph(
    config: PipelineConfig,
) -> tuple[
    DependencyGraph,
    WorkbookSeriesBindings,
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
]:
    """Build the dependency graph through the same path as production export."""
    with stub_semantic_labeling():
        return build_pipeline_graph(config)


def write_synthetic_workbook(path: Path) -> Path:
    """Write a minimal multi-sheet workbook with parallel formula families."""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = fastpyxl.Workbook()
    workbook.remove(workbook.active)

    inputs = workbook.create_sheet("Inputs")
    engine = workbook.create_sheet("Engine")
    outputs = workbook.create_sheet("Outputs")

    inputs["A1"] = 10
    inputs["B1"] = 0
    engine["B2"] = "=Inputs!A1+Inputs!B1+1"
    engine["C2"] = "=Inputs!A1+Inputs!B1+1"
    engine["B3"] = "=Engine!B2*2"
    outputs["B1"] = "=Engine!B2"
    outputs["C1"] = "=Engine!C2"

    workbook.save(path)
    return path


def build_synthetic_projection(
    graph: DependencyGraph,
) -> ProjectionResult:
    return build_refactor_projection(graph)


def load_synthetic_series_bindings(
    bindings_path: Path = BINDINGS_PATH,
) -> WorkbookSeriesBindings:
    return load_series_bindings(bindings_path)


def synthetic_pipeline_config(
    *,
    workbook_path: Path,
    repo_root: Path | None = None,
) -> PipelineConfig:
    root = repo_root or Path(__file__).resolve().parents[2]
    if not workbook_path.is_file():
        write_synthetic_workbook(workbook_path)
    templates_root = root / "templates"
    return PipelineConfig(
        repo_root=root,
        workbook_path=workbook_path,
        guide_path=root / "data" / "guide.md",
        bindings_path=FIXTURES_ROOT,
        dist_root=root / "dist",
        targets=TARGETS,
        constraints=dict(CONSTRAINTS),
        dist_metadata=DistProjectMetadata(
            project_name="synthetic-model",
            package_name="synthetic_model",
            library_name="Synthetic Model",
            description="Synthetic smoke-test workbook for the extraction pipeline template.",
            documentation_url="https://example.com/synthetic-model/",
            repository_url=None,
        ),
        docstring_callback_name="series_docs",
        projection_layout=PROJECTION_LAYOUT,
        canonical_api_example_path=templates_root / "canonical-api-usage.md",
        binding_authoring_prompt_path=templates_root / "binding-authoring-prompt.txt",
        section_rewrite_introduction_focus_path=(
            templates_root / "section-rewrite-introduction-focus.txt"
        ),
        section_rewrite_functional_overview_focus_path=(
            templates_root / "section-rewrite-functional-overview-focus.txt"
        ),
        section_rewrite_illustrative_example_focus_path=(
            templates_root / "section-rewrite-illustrative-example-focus.txt"
        ),
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=root / "artifacts" / "dependency-graph",
        graph_audit_cases=GRAPH_AUDIT_CASES,
    )
