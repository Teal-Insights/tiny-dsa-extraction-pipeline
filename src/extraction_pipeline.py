from __future__ import annotations

import argparse
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence, cast, get_args, get_origin

from excel_grapher.core.cell_types import normalize_cell_type_env_key
from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
)
from excel_grapher.exporter import CodeGenerator
from excel_grapher.exporter.codegen import GraphLike
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_internal_series,
    derive_output_series,
    load_series_bindings,
    validate_series_bindings,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.dependency_graph_viz import (
    constant_keys_from_leaf_classification,
    series_cell_keys,
    write_dependency_graph_site,
)
from src.internal_bindings import binding_node_labels, build_internal_binding_index
from src.internal_binding_coverage import enforce_internal_binding_coverage
from src.docstring_callback import configure_docstring_callback
from src.export_validation_assets import export_validation_assets
from src.logging_config import configure_logging
from src.pipeline_config import (
    PipelineConfig,
    add_variation_mode_argument,
    apply_variation_mode_cli_override,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.pipeline_context import activate_pipeline_config
from src.pipeline_monitor import (
    StageTimer,
    monitor_pipeline_stage,
    profile_if_enabled,
    resolve_stall_log_path,
)
from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    VALIDATION_BASELINE_DEV_DEPS,
    render_dist_pyproject_toml,
    write_dist_readme,
)
from src.graph_cache import get_or_build_dependency_graph
from src.subgraph_projection import build_refactor_projection

SeriesResolutionList = Sequence[Mapping[str, Any]]

EXTRACTION_SUMMARY_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class PipelineGraphResult:
    graph: DependencyGraph
    series_bindings: WorkbookSeriesBindings
    input_series: SeriesResolutionList
    output_series: SeriesResolutionList
    internal_series: SeriesResolutionList
    graph_cache_key: str


@dataclass(frozen=True)
class DependencyGraphExtraction:
    """Graph build result plus timing diagnostics for the extract-only stage."""

    graph: DependencyGraph
    series_bindings: WorkbookSeriesBindings
    input_series: SeriesResolutionList
    output_series: SeriesResolutionList
    internal_series: SeriesResolutionList
    timer: StageTimer
    elapsed_seconds: float


def count_provenance_edges(graph: DependencyGraph) -> int:
    """Count dependency edges that carry extraction provenance metadata."""
    count = 0
    for key in graph:
        for dependency in graph.get_dependencies(key):
            if graph.get_edge_attrs(key, dependency).provenance is not None:
                count += 1
    return count


def _graph_edge_count(graph: DependencyGraph) -> int:
    return sum(len(graph.get_dependencies(key)) for key in graph)


def extract_dependency_graph_result(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> DependencyGraphExtraction:
    """Build the pipeline dependency graph and collect stage timings."""
    timer = StageTimer()
    stall_log_path = resolve_stall_log_path(config.graph_output_dir)
    started = time.perf_counter()
    with profile_if_enabled(config.graph_output_dir, basename="extract"):
        graph_result = build_pipeline_graph(
            config,
            timer=timer,
            stall_log_path=stall_log_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        graph = graph_result.graph
        series_bindings = graph_result.series_bindings
        input_series = graph_result.input_series
        output_series = graph_result.output_series
        internal_series = graph_result.internal_series
        _graph_cache_key = graph_result.graph_cache_key
    elapsed_seconds = time.perf_counter() - started
    return DependencyGraphExtraction(
        graph=graph,
        series_bindings=series_bindings,
        input_series=input_series,
        output_series=output_series,
        internal_series=internal_series,
        timer=timer,
        elapsed_seconds=elapsed_seconds,
    )


def _artifact_output_path(config: PipelineConfig, path: Path) -> str:
    resolved = path if path.is_absolute() else config.repo_root / path
    try:
        return resolved.relative_to(config.repo_root).as_posix()
    except ValueError:
        return resolved.as_posix()


def write_dependency_graph_artifacts(
    extraction: DependencyGraphExtraction,
    config: PipelineConfig,
) -> dict[str, Any]:
    """Write the interactive graph site and extraction summary JSON."""
    graph = extraction.graph
    leaf_classification = graph.leaf_classification or {}
    output_dir = config.graph_output_dir
    artifact_started = time.perf_counter()
    internal_binding_index = build_internal_binding_index(extraction.internal_series)
    write_dependency_graph_site(
        graph,
        output_dir,
        node_labels=binding_node_labels(graph, internal_binding_index),
        target_keys=set(config.targets),
        input_keys=series_cell_keys(extraction.input_series),
        output_keys=series_cell_keys(extraction.output_series),
        internal_binding_index=internal_binding_index,
        constant_keys=constant_keys_from_leaf_classification(leaf_classification),
        timer=extraction.timer,
    )
    elapsed_seconds = extraction.elapsed_seconds + (
        time.perf_counter() - artifact_started
    )

    output_paths = {
        "output_dir": _artifact_output_path(config, output_dir),
        "index_html": _artifact_output_path(config, output_dir / "index.html"),
        "dependency_graph_json": _artifact_output_path(
            config, output_dir / "dependency-graph.json"
        ),
        "dependencies_dot": _artifact_output_path(
            config, output_dir / "dependencies.dot"
        ),
        "graph_topology_json": _artifact_output_path(
            config, output_dir / "graph-topology.json"
        ),
        "extraction_summary_json": _artifact_output_path(
            config, output_dir / "extraction-summary.json"
        ),
    }
    summary: dict[str, Any] = {
        "schema_version": EXTRACTION_SUMMARY_SCHEMA_VERSION,
        "node_count": len(graph),
        "edge_count": _graph_edge_count(graph),
        "leaf_count": len(graph.leaf_keys()),
        "provenance_edge_count": count_provenance_edges(graph),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "stage_timings": extraction.timer.as_dict(),
        "output_paths": output_paths,
    }
    summary_path = output_dir / "extraction-summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def extract_dependency_graph(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Build the dependency graph, write review artifacts, and return the summary."""
    extraction = extract_dependency_graph_result(
        config,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )
    summary = write_dependency_graph_artifacts(extraction, config)
    extraction.timer.print_summary(header="Extract stage timings")
    stall_log_path = resolve_stall_log_path(config.graph_output_dir)
    if stall_log_path.is_file():
        print(f"Stall diagnostics: {stall_log_path}")
    print(f"Wrote dependency graph artifacts to {config.graph_output_dir.resolve()}/")
    return summary


def is_constant_constraint(constraint: object) -> bool:
    """True when the constraint fixes a single value (lookup/structural data)."""
    return get_origin(constraint) is Literal and len(get_args(constraint)) == 1


def classify_leaves_from_constraints(
    constraint_map: Mapping[str, object],
    leaf_keys: Iterable[str],
) -> dict[str, str]:
    """Classify graph leaves as inputs or constants from their constraints."""
    normalized_constraints = {
        normalize_cell_type_env_key(key): value for key, value in constraint_map.items()
    }
    keys = list(leaf_keys)
    missing = [
        key
        for key in keys
        if normalize_cell_type_env_key(key) not in normalized_constraints
    ]
    if missing:
        raise KeyError(f"missing constraints for leaf cells: {missing}")
    return {
        key: (
            "constant"
            if is_constant_constraint(
                normalized_constraints[normalize_cell_type_env_key(key)]
            )
            else "input"
        )
        for key in keys
    }


def build_pipeline_graph(
    config: PipelineConfig,
    *,
    timer: StageTimer | None = None,
    stall_log_path: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> PipelineGraphResult:
    def stage(name: str):
        if timer is None:
            return nullcontext()
        return monitor_pipeline_stage(
            timer,
            name,
            stall_log_path=stall_log_path,
        )

    with stage("load_series_bindings"):
        series_bindings: WorkbookSeriesBindings = load_series_bindings(
            config.bindings_path
        )
        dynamic_ref_config = DynamicRefConfig.from_constraints(config.constraints, {})

    with stage("create_dependency_graph"):
        graph_result = get_or_build_dependency_graph(
            workbook_path=config.workbook_path,
            targets=config.targets,
            constraints=config.constraints,
            bindings_path=config.bindings_path,
            dynamic_refs=dynamic_ref_config,
            load_values=True,
            capture_dependency_provenance=True,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        graph = graph_result.graph
        graph_cache_key = graph_result.cache_key

    with stage("validate_series_bindings"):
        binding_validation_report = validate_series_bindings(
            graph,
            series_bindings,
            workbook=config.workbook_path,
        )
        if not binding_validation_report["ok"]:
            raise ValueError(
                f"Invalid series bindings: {binding_validation_report['issues']!r}"
            )

    with stage("derive_input_series"):
        input_series = cast(
            SeriesResolutionList,
            derive_input_series(graph, series_bindings, workbook=config.workbook_path),
        )

    with stage("derive_output_series"):
        output_series = cast(
            SeriesResolutionList,
            derive_output_series(graph, series_bindings, workbook=config.workbook_path),
        )

    with stage("derive_internal_series"):
        internal_series = cast(
            SeriesResolutionList,
            derive_internal_series(
                graph, series_bindings, workbook=config.workbook_path
            ),
        )

    with stage("classify_leaves"):
        leaf_classification = classify_leaves_from_constraints(
            config.constraints, graph.leaf_keys()
        )
        graph.leaf_classification = leaf_classification

    input_cell_keys = series_cell_keys(input_series)
    output_cell_keys = series_cell_keys(output_series)

    with stage("validate_internal_binding_coverage"):
        enforce_internal_binding_coverage(
            graph=graph,
            internal_series=internal_series,
            input_cells=input_cell_keys,
            output_cells=output_cell_keys,
            exempt_cells=config.internal_binding_exempt_cells,
            mode=config.internal_binding_validation_mode,
            context="pipeline",
        )

    return PipelineGraphResult(
        graph=graph,
        series_bindings=series_bindings,
        input_series=input_series,
        output_series=output_series,
        internal_series=internal_series,
        graph_cache_key=graph_cache_key,
    )


def export_generated_package(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    """Write the generated package under dist/."""
    configure_logging()
    timer = StageTimer()
    stall_log_path = resolve_stall_log_path(config.graph_output_dir)
    with profile_if_enabled(config.graph_output_dir):
        graph_result = build_pipeline_graph(
            config,
            timer=timer,
            stall_log_path=stall_log_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        graph = graph_result.graph
        series_bindings = graph_result.series_bindings
        graph_cache_key = graph_result.graph_cache_key
        internal_binding_index = build_internal_binding_index(
            graph_result.internal_series
        )
    timer.print_summary()
    if stall_log_path.is_file():
        print(f"Stall diagnostics: {stall_log_path}")
    refactor_projection = build_refactor_projection(
        graph,
        graph_cache_key=graph_cache_key,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )
    callback_name = configure_docstring_callback(config)

    with CodeGenerator(cast(GraphLike, refactor_projection)) as generator:
        modules = generator.generate_modules(
            list(config.targets),
            series_bindings=series_bindings,
            bindings_workbook=config.workbook_path,
            series_docstring_callback=callback_name,
            docstring_renderer="google",
        )

    package_root = config.package_root
    package_root.mkdir(parents=True, exist_ok=True)

    generated_module_names = frozenset(
        {"__init__.py", "api.py", "data.py", "runtime.py", "internals.py"}
    )

    for filepath, code in modules.items():
        output_path = package_root / filepath
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(code, encoding="utf-8", newline="\n")

    for stale_module in generated_module_names:
        stale_path = config.dist_root / stale_module
        if stale_path.is_file():
            stale_path.unlink()

    gitignore_content = """
*.egg-info/
*.pyc
__pycache__/
.venv/
_validate_user_guide_cells.py
tests/results/local/
"""

    (config.dist_root / ".gitignore").write_text(gitignore_content, encoding="utf-8")
    (config.dist_root / "pyproject.toml").write_text(
        render_dist_pyproject_toml(
            dev_dependencies=list(DOCUMENTATION_BASELINE_DEV_DEPS),
            validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
            metadata=config.dist_metadata,
        ),
        encoding="utf-8",
    )
    write_dist_readme(config.dist_root, metadata=config.dist_metadata)

    export_validation_assets(config=config)

    from src.formula_clustering import cluster_graph_formulas
    from src.internals_refactor import refactor_internals_all_clusters
    from src.refactor_bindings import build_bound_address_keys

    bound_address_keys = build_bound_address_keys(
        graph_result.input_series,
        graph_result.output_series,
        graph_result.internal_series,
    )
    formula_clusters = cluster_graph_formulas(
        refactor_projection,
        bound_address_keys=bound_address_keys,
        variation_mode=config.variation_mode,
        workbook_path=config.workbook_path,
        layout=config.projection_layout,
    )
    refactor_internals_all_clusters(
        refactor_projection,
        formula_clusters,
        internals_path=package_root / "internals.py",
        source_graph=graph,
        internal_binding_index=internal_binding_index,
        bindings_path=config.bindings_path,
        workbook_path=config.workbook_path,
    )


def main(argv: Sequence[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run the extraction pipeline.")
    parser.add_argument(
        "--extract-graph",
        action="store_true",
        help="Build the dependency graph, write review artifacts, and exit.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk graph and projection caches for this run.",
    )
    add_variation_mode_argument(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = apply_variation_mode_cli_override(
        load_pipeline_config(), args.variation_mode
    )
    validate_pipeline_config(config)
    activate_pipeline_config(config)
    if args.extract_graph:
        extract_dependency_graph(config, no_cache=args.no_cache)
        return
    export_generated_package(config, no_cache=args.no_cache)
    from src.documentation_pipeline import run_documentation_pipeline

    run_documentation_pipeline(config)


if __name__ == "__main__":
    main()
