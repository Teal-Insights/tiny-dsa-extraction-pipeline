"""Synthetic workbook and pipeline config for graph-backed smoke tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

import fastpyxl
from excel_grapher.core.cell_types import RealBetween
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher import DependencyGraph
from excel_grapher.series_bindings import WorkbookSeriesBindings, load_series_bindings

from src.extraction_pipeline import build_pipeline_graph
from src.graph_dependency_audit import GraphAuditCase
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.subgraph_projection import build_refactor_projection

FIXTURES_ROOT = Path(__file__).resolve().parent / "synthetic"
BINDINGS_PATH = FIXTURES_ROOT

TARGETS: tuple[str, ...] = ("Outputs!B1", "Outputs!C1")

CONSTRAINTS: dict[str, object] = {
    "Inputs!A1": Annotated[float, RealBetween(0.0, 100.0)],
    "Inputs!B1": Literal[0],
}

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


def build_synthetic_pipeline_graph(
    config: PipelineConfig,
) -> tuple[
    DependencyGraph,
    WorkbookSeriesBindings,
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
]:
    """Build the dependency graph through the same path as production export."""
    graph_result = build_pipeline_graph(config)
    return (
        graph_result.graph,
        graph_result.series_bindings,
        graph_result.input_series,
        graph_result.output_series,
        graph_result.internal_series,
        graph_result.constant_series,
    )


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
    *,
    series_bindings: WorkbookSeriesBindings | None = None,
    bindings_workbook: Path | None = None,
) -> ProjectionResult:
    return build_refactor_projection(
        graph,
        series_bindings=series_bindings,
        bindings_workbook=bindings_workbook,
    )


def load_synthetic_series_bindings(
    bindings_path: Path = BINDINGS_PATH,
) -> WorkbookSeriesBindings:
    return load_series_bindings(bindings_path)


def synthetic_pipeline_config(
    *,
    workbook_path: Path,
    repo_root: Path | None = None,
    dist_root: Path | None = None,
) -> PipelineConfig:
    root = repo_root or Path(__file__).resolve().parents[2]
    if not workbook_path.is_file():
        write_synthetic_workbook(workbook_path)
    templates_root = root / "templates"
    return PipelineConfig(
        repo_root=root,
        workbook_path=workbook_path,
        guide_path=FIXTURES_ROOT / "guide.md",
        bindings_path=FIXTURES_ROOT,
        dist_root=dist_root if dist_root is not None else root / "dist",
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
        binding_authoring_prompt_path=templates_root / "binding-authoring-prompt.txt",
        user_guide_agent_prompt_path=templates_root / "user-guide-agent.txt",
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=root / "artifacts" / "dependency-graph",
        graph_audit_cases=GRAPH_AUDIT_CASES,
        internal_binding_validation_mode="off",
        internal_binding_exempt_cells=frozenset(),
    )
