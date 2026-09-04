from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args, get_origin

from excel_grapher.core.cell_types import normalize_cell_type_env_key
from excel_grapher.exporter import CodeGenerator, ProjectionResult
from excel_grapher.grapher import (
    DependencyGraph,
    DynamicRefConfig,
)
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.bindings_validation_cache import get_or_build_bindings_validation
from src.codegen_cache import (
    get_or_build_codegen_modules,
    guide_fingerprint,
)
from src.dependency_graph_viz import (
    constant_keys_from_leaf_classification,
    series_cell_keys,
    write_dependency_graph_site,
)
from src.graph_cache import get_or_build_dependency_graph, load_dependency_graph
from src.internal_binding_coverage import InternalBindingCoverageReport
from src.internal_bindings import (
    InternalBindingIndex,
    binding_node_labels,
)
from src.logging_config import configure_logging
from src.package_materialize import (
    materialize_package,
    try_materialize_refactored_package_from_cache,
)
from src.pipeline_config import (
    PipelineConfig,
    add_clustering_mode_argument,
    add_variation_mode_argument,
    apply_clustering_mode_cli_override,
    apply_variation_mode_cli_override,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.pipeline_monitor import (
    StageTimer,
    monitor_pipeline_stage,
    profile_if_enabled,
    resolve_stall_log_path,
)
from src.projection_cache import (
    load_projection_payload,
    projection_cache_key,
    rehydrate_projection_result,
)
from src.refactor_bindings import BindingKeyValue
from src.refactor_types import unwrap_annotation
from src.series_derived_cache import (
    get_or_build_series_derived,
    load_series_derived_payload,
    series_derived_cache_key,
)
from src.series_resolution_cache import get_or_build_series_resolution
from src.stage_manifest import (
    StageManifest,
    StageManifestError,
    compute_input_fingerprints,
    require_upstream_manifest,
    write_stage_manifest,
)
from src.stage_timings import (
    PipelineTimings,
    record_cache_outcome,
    record_cache_result,
    stage_span,
    stage_timings_path,
)

SeriesResolutionList = Sequence[Mapping[str, Any]]

EXTRACTION_SUMMARY_SCHEMA_VERSION = "1.0.0"

logger = logging.getLogger(__name__)

PipelineStageName = Literal[
    "extract",
    "export",
    "annotate",
    "validate",
    "document",
]
PIPELINE_STAGES: tuple[PipelineStageName, ...] = (
    "extract",
    "export",
    "annotate",
    "validate",
    "document",
)


class DocumentStageError(RuntimeError):
    """Raised when the document stage fails after earlier artifacts are written."""


@dataclass(frozen=True)
class PipelineGraphResult:
    graph: DependencyGraph
    series_bindings: WorkbookSeriesBindings
    input_series: SeriesResolutionList
    output_series: SeriesResolutionList
    internal_series: SeriesResolutionList
    constant_series: SeriesResolutionList
    graph_cache_key: str
    leaf_classification: dict[str, str]
    internal_binding_index: InternalBindingIndex
    bound_address_keys: dict[str, dict[str, BindingKeyValue]]
    address_to_series_id: dict[str, str]
    coverage_report: InternalBindingCoverageReport | None


@dataclass(frozen=True)
class DependencyGraphBuild:
    """Graph construction result without series-binding post-processing."""

    graph: DependencyGraph
    graph_cache_key: str


@dataclass(frozen=True)
class DependencyGraphExtraction:
    """Graph-first extract result plus timing diagnostics for review artifacts."""

    graph: DependencyGraph
    graph_cache_key: str
    leaf_classification: dict[str, str]
    internal_binding_index: InternalBindingIndex
    input_series: SeriesResolutionList
    output_series: SeriesResolutionList
    timer: StageTimer
    elapsed_seconds: float


@dataclass(frozen=True)
class ExtractStageResult:
    """Extract stage outputs: review summary plus the live graph for export."""

    summary: dict[str, Any]
    graph: DependencyGraph
    graph_cache_key: str


@dataclass(frozen=True)
class ExportStageState:
    """Cache-key references produced by export for later pipeline stages."""

    config: PipelineConfig
    graph_cache_key: str
    projection_cache_key: str
    series_derived_cache_key: str
    codegen_cache_key: str
    package_root: Path


@dataclass(frozen=True)
class AnnotateStageState:
    """Cache-key references produced by annotate for validation / document."""

    config: PipelineConfig
    codegen_cache_key: str


@dataclass(frozen=True)
class RefactorStageState:
    """Cache-key references produced by refactor for validation / document."""

    config: PipelineConfig
    codegen_cache_key: str
    internals_cache_key: str | None
    clusters_cache_key: str | None = None


@dataclass(frozen=True)
class ExportStageArtifacts:
    """Live objects resolved from export-stage cache keys."""

    graph: DependencyGraph
    refactor_projection: Any
    internal_binding_index: InternalBindingIndex
    bound_address_keys: dict[str, dict[str, BindingKeyValue]]
    address_to_series_id: dict[str, str]


@dataclass(frozen=True)
class RefactorLabOptions:
    """Optional lab controls for isolated refactor iteration."""

    dry_run: bool = False
    parity_gate: bool = True
    prompt_observer: Any | None = None
    cluster_context_observer: Any | None = None
    singleton_context_observer: Any | None = None

    def requires_refactor_run(self) -> bool:
        """True when these options only take effect if Pass 1 / Pass 2 actually run.

        Every warm-cache short-circuit in :func:`run_refactor_stage` returns
        before ``refactor_internals_all_clusters``, so answering a lab run from
        cache would silently drop the observers and make ``--dry-run`` /
        ``--no-parity-gate`` no-ops.
        """
        return (
            self.dry_run
            or not self.parity_gate
            or self.prompt_observer is not None
            or self.cluster_context_observer is not None
            or self.singleton_context_observer is not None
        )


class StageCacheMissingError(StageManifestError):
    """Raised when a manifest key has no corresponding cache payload."""


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


def _best_effort_leaf_classification(
    constraints: Mapping[str, object],
    leaf_keys: Iterable[str],
) -> dict[str, str]:
    """Classify constrained leaves; return {} when any leaf is still unconstrained."""
    try:
        return classify_leaves_from_constraints(constraints, leaf_keys)
    except KeyError:
        return {}


def extract_dependency_graph_result(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
    timer: StageTimer | None = None,
    profile: bool = True,
) -> DependencyGraphExtraction:
    """Build the dependency graph without loading or validating series bindings.

    Extract is graph-first so bootstrap runs can produce reviewable artifacts
    while binding shards are still empty placeholders. Binding load / validate /
    derive / coverage run later in export (or via ``build_pipeline_graph``).

    When ``timer`` is supplied (e.g. from ``stage_span``), spans accumulate on
    that timer. ``profile=False`` skips the local ``profile_if_enabled`` wrap so
    a caller that already profiles the surrounding stage is not double-wrapped.
    """
    stage_timer = timer if timer is not None else StageTimer()
    stall_log_path = resolve_stall_log_path(config.graph_output_dir)
    started = time.perf_counter()

    def _build() -> DependencyGraphBuild:
        return build_dependency_graph(
            config,
            timer=stage_timer,
            stall_log_path=stall_log_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
        )

    if profile:
        with profile_if_enabled(config.graph_output_dir, basename="extract"):
            graph_build = _build()
    else:
        graph_build = _build()
    elapsed_seconds = time.perf_counter() - started
    leaf_classification = _best_effort_leaf_classification(
        config.constraints,
        graph_build.graph.leaf_keys(),
    )
    return DependencyGraphExtraction(
        graph=graph_build.graph,
        graph_cache_key=graph_build.graph_cache_key,
        leaf_classification=leaf_classification,
        internal_binding_index={},
        input_series=(),
        output_series=(),
        timer=stage_timer,
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
    leaf_classification = extraction.leaf_classification
    output_dir = config.graph_output_dir
    artifact_started = time.perf_counter()
    internal_binding_index = extraction.internal_binding_index
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
    timings: PipelineTimings | None = None,
) -> ExtractStageResult:
    """Build the dependency graph, write review artifacts, and return the live graph."""
    with (
        profile_if_enabled(config.graph_output_dir, basename="extract"),
        stage_span(timings, "extract") as timer,
    ):
        extraction = extract_dependency_graph_result(
            config,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
            timer=timer,
            profile=False,
        )
        summary = write_dependency_graph_artifacts(extraction, config)
        write_stage_manifest(
            config,
            stage="extract",
            cache_keys={
                "graph_cache_key": extraction.graph_cache_key,
            },
            upstream_keys={},
            fingerprints=compute_input_fingerprints(config),
        )
        timer.print_summary(header="Extract stage timings")
        stall_log_path = resolve_stall_log_path(config.graph_output_dir)
        if stall_log_path.is_file():
            print(f"Stall diagnostics: {stall_log_path}")
        print(
            f"Wrote dependency graph artifacts to {config.graph_output_dir.resolve()}/"
        )
        return ExtractStageResult(
            summary=summary,
            graph=extraction.graph,
            graph_cache_key=extraction.graph_cache_key,
        )


def load_export_stage_artifacts(state: ExportStageState) -> ExportStageArtifacts:
    """Resolve live export artifacts from cache keys; fail if any payload is missing."""
    graph = load_dependency_graph(state.graph_cache_key)
    if graph is None:
        raise StageCacheMissingError(
            f"missing dependency-graph cache payload for key "
            f"{state.graph_cache_key[:12]}"
        )
    derived = load_series_derived_payload(state.series_derived_cache_key)
    if derived is None:
        raise StageCacheMissingError(
            f"missing series-derived cache payload for key "
            f"{state.series_derived_cache_key[:12]}"
        )
    (
        _leaf_classification,
        internal_binding_index,
        bound_address_keys,
        address_to_series_id,
        _coverage_report,
    ) = derived
    projection_payload = load_projection_payload(state.projection_cache_key)
    if projection_payload is None:
        raise StageCacheMissingError(
            f"missing projection cache payload for key "
            f"{state.projection_cache_key[:12]}"
        )
    projected_graph, manifest = projection_payload
    refactor_projection = rehydrate_projection_result(
        original_graph=graph,
        projected_graph=projected_graph,
        manifest=manifest,
    )
    return ExportStageArtifacts(
        graph=graph,
        refactor_projection=refactor_projection,
        internal_binding_index=internal_binding_index,
        bound_address_keys=dict(bound_address_keys),
        address_to_series_id=dict(address_to_series_id),
    )


def export_stage_state_from_manifest(
    config: PipelineConfig,
    manifest: StageManifest,
) -> ExportStageState:
    """Build ``ExportStageState`` from an export-stage (or compatible) manifest."""
    keys = manifest.cache_keys
    required = (
        "graph_cache_key",
        "projection_cache_key",
        "series_derived_cache_key",
        "codegen_cache_key",
    )
    missing = [name for name in required if name not in keys]
    if missing:
        raise StageManifestError(
            f"manifest for {manifest.stage!r} missing cache keys: {missing}"
        )
    return ExportStageState(
        config=config,
        graph_cache_key=keys["graph_cache_key"],
        projection_cache_key=keys["projection_cache_key"],
        series_derived_cache_key=keys["series_derived_cache_key"],
        codegen_cache_key=keys["codegen_cache_key"],
        package_root=config.package_root,
    )


def refactor_stage_state_from_manifest(
    config: PipelineConfig,
    manifest: StageManifest,
) -> RefactorStageState:
    """Build ``RefactorStageState`` from a refactor-stage (or compatible) manifest."""
    keys = manifest.cache_keys
    codegen_key = keys.get("codegen_cache_key")
    if not codegen_key:
        raise StageManifestError(
            f"manifest for {manifest.stage!r} missing codegen_cache_key"
        )
    return RefactorStageState(
        config=config,
        codegen_cache_key=codegen_key,
        internals_cache_key=keys.get("internals_cache_key"),
        clusters_cache_key=keys.get("clusters_cache_key"),
    )


def _materialize_from_refactor_keys(
    config: PipelineConfig,
    *,
    codegen_key: str,
    internals_key: str | None,
) -> None:
    """Rehydrate ``dist/`` for validate/document entry.

    When ``internals_key`` is absent (non-cacheable lab / ungated refactor), leave
    the on-disk package alone so rematerialization cannot overwrite lab output
    with pristine codegen.

    The manifest pins the exact content key, so adoption of a committed ``dist/``
    is gated on that key rather than on provenance. Adoption is tried first
    because ``materialize_package`` needs a warm codegen cache, which a fresh
    clone does not have.
    """
    if internals_key is None:
        print(
            "materialize: skipping package rebuild "
            "(no internals_cache_key; preserving on-disk dist/)",
            flush=True,
        )
        return
    if try_materialize_refactored_package_from_cache(
        config,
        codegen_key=codegen_key,
        expected_internals_key=internals_key,
    ):
        return
    materialize_package(
        config,
        codegen_key=codegen_key,
        internals_key=internals_key,
    )


def is_constant_constraint(constraint: object) -> bool:
    """True when the constraint fixes a single value (lookup/structural data)."""
    resolved = unwrap_annotation(constraint)
    return get_origin(resolved) is Literal and len(get_args(resolved)) == 1


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


def stamp_projection_leaf_classification(
    projection: ProjectionResult,
    leaf_classification: Mapping[str, str],
) -> None:
    """Copy constraint-derived leaf roles onto the projected graph for codegen.

    ``CodeGenerator`` splits ``DEFAULT_INPUTS`` / ``CONSTANTS`` from
    ``graph.leaf_classification``. The cached canonical graph stays unmarked so
    shared pickle hits remain classification-free; ``ProjectionResult`` writes
    this attribute onto ``projected_graph`` only.
    """
    projection.leaf_classification = dict(leaf_classification)


def build_dependency_graph(
    config: PipelineConfig,
    *,
    timer: StageTimer | None = None,
    stall_log_path: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
) -> DependencyGraphBuild:
    """Build (or load) the dependency graph without series-binding post-processing."""

    def stage(name: str):
        if timer is None:
            return nullcontext()
        return monitor_pipeline_stage(
            timer,
            name,
            stall_log_path=stall_log_path,
        )

    with stage("create_dependency_graph"):
        dynamic_ref_config = DynamicRefConfig.from_constraints(config.constraints, {})
        graph_result = get_or_build_dependency_graph(
            workbook_path=config.workbook_path,
            targets=config.targets,
            constraints=config.constraints,
            dynamic_refs=dynamic_ref_config,
            load_values=True,
            capture_dependency_provenance=True,
            blank_ranges=config.blank_ranges,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        record_cache_result(timings, "dependency-graph", graph_result)
    return DependencyGraphBuild(
        graph=graph_result.graph,
        graph_cache_key=graph_result.cache_key,
    )


def resolve_pipeline_bindings(
    config: PipelineConfig,
    *,
    graph: DependencyGraph,
    graph_cache_key: str,
    timer: StageTimer | None = None,
    stall_log_path: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
) -> PipelineGraphResult:
    """Load, validate, and derive series bindings against an existing graph."""

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

    with stage("validate_series_bindings"):
        validation_result = get_or_build_bindings_validation(
            graph,
            series_bindings,
            workbook_path=config.workbook_path,
            graph_cache_key=graph_cache_key,
            bindings_path=config.bindings_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        record_cache_result(timings, "bindings-validation", validation_result)
        binding_validation_report = validation_result.report
        if not binding_validation_report["ok"]:
            raise ValueError(
                f"Invalid series bindings: {binding_validation_report['issues']!r}"
            )

    with stage("derive_series"):
        series_result = get_or_build_series_resolution(
            graph,
            series_bindings,
            workbook_path=config.workbook_path,
            graph_cache_key=graph_cache_key,
            bindings_path=config.bindings_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        record_cache_result(timings, "series-resolution", series_result)
        input_series = series_result.input_series
        output_series = series_result.output_series
        internal_series = series_result.internal_series
        constant_series = series_result.constant_series

    with stage("series_derived"):
        derived_result = get_or_build_series_derived(
            graph,
            constraints=config.constraints,
            input_series=input_series,
            output_series=output_series,
            internal_series=internal_series,
            constant_series=constant_series,
            input_cells=series_cell_keys(input_series),
            output_cells=series_cell_keys(output_series),
            exempt_cells=config.internal_binding_exempt_cells,
            validation_mode=config.internal_binding_validation_mode,
            context="pipeline",
            graph_cache_key=graph_cache_key,
            bindings_path=config.bindings_path,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        record_cache_result(timings, "series-derived", derived_result)

    return PipelineGraphResult(
        graph=graph,
        series_bindings=series_bindings,
        input_series=input_series,
        output_series=output_series,
        internal_series=internal_series,
        constant_series=constant_series,
        graph_cache_key=graph_cache_key,
        leaf_classification=derived_result.leaf_classification,
        internal_binding_index=derived_result.internal_binding_index,
        bound_address_keys=derived_result.bound_address_keys,
        address_to_series_id=derived_result.address_to_series_id,
        coverage_report=derived_result.coverage_report,
    )


def build_pipeline_graph(
    config: PipelineConfig,
    *,
    timer: StageTimer | None = None,
    stall_log_path: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
) -> PipelineGraphResult:
    """Build the dependency graph and run the full series-binding post-processing stack."""
    graph_build = build_dependency_graph(
        config,
        timer=timer,
        stall_log_path=stall_log_path,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
        timings=timings,
    )
    return resolve_pipeline_bindings(
        config,
        graph=graph_build.graph,
        graph_cache_key=graph_build.graph_cache_key,
        timer=timer,
        stall_log_path=stall_log_path,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
        timings=timings,
    )


def run_export_stage(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
    graph: DependencyGraph | None = None,
    graph_cache_key: str | None = None,
    graph_result: PipelineGraphResult | None = None,
) -> ExportStageState:
    """Project, codegen, and materialize the package under dist/.

    When ``graph`` / ``graph_cache_key`` are supplied (full pipeline after
    extract), the graph is not rebuilt; series bindings are resolved here.
    When a full ``graph_result`` is supplied, binding post-processing is skipped.
    Standalone export (``start_from_stage=export`` / direct calls) builds the
    graph and resolves bindings inside this stage.
    """
    configure_logging()
    stall_log_path = resolve_stall_log_path(config.graph_output_dir)
    with (
        profile_if_enabled(config.graph_output_dir, basename="export"),
        stage_span(timings, "export") as timer,
    ):
        if graph_result is None:
            if graph is not None:
                if graph_cache_key is None:
                    raise ValueError(
                        "graph_cache_key is required when graph is supplied"
                    )
                graph_result = resolve_pipeline_bindings(
                    config,
                    graph=graph,
                    graph_cache_key=graph_cache_key,
                    timer=timer,
                    stall_log_path=stall_log_path,
                    no_cache=no_cache,
                    force_rebuild=force_rebuild,
                    timings=timings,
                )
            else:
                graph_result = build_pipeline_graph(
                    config,
                    timer=timer,
                    stall_log_path=stall_log_path,
                    no_cache=no_cache,
                    force_rebuild=force_rebuild,
                    timings=timings,
                )
            if stall_log_path.is_file():
                print(f"Stall diagnostics: {stall_log_path}")
        graph = graph_result.graph
        series_bindings = graph_result.series_bindings
        graph_cache_key = graph_result.graph_cache_key
        state = _generate_export_package(
            config,
            graph=graph,
            series_bindings=series_bindings,
            graph_cache_key=graph_cache_key,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
            timer=timer,
        )
        timer.print_summary(header="Export stage timings")
        return state


def _generate_export_package(
    config: PipelineConfig,
    *,
    graph: DependencyGraph,
    series_bindings: WorkbookSeriesBindings,
    graph_cache_key: str,
    no_cache: bool,
    force_rebuild: bool,
    timings: PipelineTimings | None,
    timer: StageTimer,
) -> ExportStageState:
    """Generate the inverted-tree package under dist/ and seed the harness."""
    proj_cache_key = projection_cache_key(
        graph_cache_key=graph_cache_key,
        series_bindings_preserve=True,
    )
    targets = list(config.targets)

    def _build_modules() -> dict[str, str]:
        with CodeGenerator(graph) as generator:
            return generator.generate_modules(
                targets,
                series_bindings=series_bindings,
                bindings_workbook=config.workbook_path,
                paradigm="inverted_tree",
                blank_ranges=config.blank_ranges,
            )

    codegen_started = time.perf_counter()
    codegen_result = get_or_build_codegen_modules(
        projection_cache_key=proj_cache_key,
        targets=targets,
        unpack_return=True,
        docstring_renderer="google",
        series_docstring_callback="none",
        guide_sha256=guide_fingerprint(config.guide_path),
        paradigm="inverted_tree",
        build_modules=_build_modules,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )
    timer.record("codegen", time.perf_counter() - codegen_started)
    record_cache_result(timings, "codegen", codegen_result)

    package_started = time.perf_counter()
    package_root = config.package_root
    materialize_package(
        config, codegen_key=codegen_result.cache_key, apply_rewrites=False
    )
    timer.record("write_export_package", time.perf_counter() - package_started)
    print(
        f"codegen: {len(codegen_result.modules)} modules "
        f"({codegen_result.elapsed_seconds:.1f}s)",
        flush=True,
    )

    derived_key = series_derived_cache_key(
        graph_cache_key=graph_cache_key,
        bindings_path=config.bindings_path,
        validation_mode=config.internal_binding_validation_mode,
        exempt_cells=config.internal_binding_exempt_cells,
    )
    state = ExportStageState(
        config=config,
        graph_cache_key=graph_cache_key,
        projection_cache_key=proj_cache_key,
        series_derived_cache_key=derived_key,
        codegen_cache_key=codegen_result.cache_key,
        package_root=package_root,
    )
    write_stage_manifest(
        config,
        stage="export",
        cache_keys={
            "graph_cache_key": state.graph_cache_key,
            "projection_cache_key": state.projection_cache_key,
            "series_derived_cache_key": state.series_derived_cache_key,
            "codegen_cache_key": state.codegen_cache_key,
        },
        upstream_keys={
            "graph_cache_key": state.graph_cache_key,
            "series_derived_cache_key": state.series_derived_cache_key,
        },
        fingerprints=compute_input_fingerprints(config),
    )
    return state


def _write_refactor_manifest(
    config: PipelineConfig,
    *,
    export_state: ExportStageState,
    clusters_cache_key: str | None,
    internals_cache_key: str | None,
) -> None:
    cache_keys = {
        "graph_cache_key": export_state.graph_cache_key,
        "projection_cache_key": export_state.projection_cache_key,
        "series_derived_cache_key": export_state.series_derived_cache_key,
        "codegen_cache_key": export_state.codegen_cache_key,
    }
    if clusters_cache_key is not None:
        cache_keys["clusters_cache_key"] = clusters_cache_key
    if internals_cache_key is not None:
        cache_keys["internals_cache_key"] = internals_cache_key
    write_stage_manifest(
        config,
        stage="refactor",
        cache_keys=cache_keys,
        upstream_keys={
            "graph_cache_key": export_state.graph_cache_key,
            "projection_cache_key": export_state.projection_cache_key,
            "series_derived_cache_key": export_state.series_derived_cache_key,
            "codegen_cache_key": export_state.codegen_cache_key,
        },
        fingerprints=compute_input_fingerprints(config),
    )


def run_refactor_stage(
    state: ExportStageState,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
    lab_options: RefactorLabOptions | None = None,
) -> RefactorStageState:
    """Cluster formulas and rewrite internals behind the parity gate."""
    from excel_grapher.series_bindings import load_series_bindings

    from src.cluster_cache import get_or_build_clusters_and_schedule
    from src.internals_cache import (
        consumed_refactors_digest,
        internals_cache_key,
        load_refactored_internals_payload,
        save_refactored_internals_payload,
    )
    from src.internals_refactor import (
        refactor_internals_all_clusters,
        set_cluster_context_observer,
        set_refactor_prompt_observer,
        set_singleton_context_observer,
    )
    from src.package_materialize import current_internals_inputs
    from src.refactor_bindings import key_concept_vocabulary_from_bindings

    config = state.config
    lab = lab_options or RefactorLabOptions()
    # A lab run that asked for observers, a dry run, or an ungated pass must
    # reach refactor_internals_all_clusters; answering it from cache would
    # silently do nothing at all.
    use_internals_cache = (
        not no_cache and not force_rebuild and not lab.requires_refactor_run()
    )
    with (
        profile_if_enabled(config.graph_output_dir, basename="refactor"),
        stage_span(timings, "refactor") as timer,
    ):
        # Fresh-clone / committed-dist path: trust sidecar keys (#238), but only
        # once the recorded provenance shows the module was built the way this
        # run would build it. Clustering has not run yet, so the full content key
        # is not available to compare against here.
        adopt_started = time.perf_counter()
        if use_internals_cache and try_materialize_refactored_package_from_cache(
            config,
            codegen_key=state.codegen_cache_key,
            expected_internals_inputs=current_internals_inputs(),
        ):
            from src.package_materialize import read_package_cache_keys

            package_keys = read_package_cache_keys(config.dist_root)
            internals_key = None if package_keys is None else package_keys.internals_key
            print(
                "internals_refactor: skipped "
                f"(adopted dist/ cache keys for codegen={state.codegen_cache_key[:12]})",
                flush=True,
            )
            if internals_key is not None:
                record_cache_outcome(
                    timings,
                    "internals",
                    cache_hit=True,
                    elapsed_seconds=time.perf_counter() - adopt_started,
                    cache_key=internals_key,
                )
            result = RefactorStageState(
                config=config,
                codegen_cache_key=state.codegen_cache_key,
                internals_cache_key=internals_key,
            )
            _write_refactor_manifest(
                config,
                export_state=state,
                clusters_cache_key=None,
                internals_cache_key=internals_key,
            )
            return result

        bindings_started = time.perf_counter()
        artifacts = load_export_stage_artifacts(state)
        bound_address_keys = artifacts.bound_address_keys
        address_to_series_id = artifacts.address_to_series_id
        key_vocabulary = key_concept_vocabulary_from_bindings(
            load_series_bindings(config.bindings_path)
        )
        timer.record("build_refactor_bindings", time.perf_counter() - bindings_started)
        print("clustering: partitioning formulas...", flush=True)
        clustering_started = time.perf_counter()
        cluster_result = get_or_build_clusters_and_schedule(
            artifacts.refactor_projection,
            bound_address_keys=bound_address_keys,
            address_to_series_id=address_to_series_id,
            workbook_path=config.workbook_path,
            bindings_path=config.bindings_path,
            projection_cache_key=state.projection_cache_key,
            variation_mode=config.variation_mode,
            clustering_mode=config.clustering_mode,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )
        formula_clusters = cluster_result.clusters
        clustering_seconds = time.perf_counter() - clustering_started
        timer.record("cluster_graph_formulas", clustering_seconds)
        record_cache_result(timings, "clusters", cluster_result)
        formula_count = sum(len(cluster.members) for cluster in formula_clusters)
        print(
            f"clustering: {formula_count} formulas -> {len(formula_clusters)} clusters "
            f"({clustering_seconds:.1f}s)",
            flush=True,
        )

        # Content-keyed warm hit (#239): materialize and skip Pass 1 / gate / Pass 2.
        refactor_digest = consumed_refactors_digest()
        internals_key = internals_cache_key(
            codegen_cache_key=state.codegen_cache_key,
            clusters_cache_key=cluster_result.cache_key,
            consumed_refactors_digest=refactor_digest,
        )
        if use_internals_cache:
            internals_started = time.perf_counter()
            cached_source = load_refactored_internals_payload(internals_key)
            if cached_source is not None:
                materialize_package(
                    config,
                    codegen_key=state.codegen_cache_key,
                    internals_key=internals_key,
                )
                record_cache_outcome(
                    timings,
                    "internals",
                    cache_hit=True,
                    elapsed_seconds=time.perf_counter() - internals_started,
                    cache_key=internals_key,
                )
                print(
                    "internals: cache hit "
                    f"(key={internals_key[:12]}); skipped Pass 1 / parity / Pass 2",
                    flush=True,
                )
                result = RefactorStageState(
                    config=config,
                    codegen_cache_key=state.codegen_cache_key,
                    internals_cache_key=internals_key,
                    clusters_cache_key=cluster_result.cache_key,
                )
                _write_refactor_manifest(
                    config,
                    export_state=state,
                    clusters_cache_key=cluster_result.cache_key,
                    internals_cache_key=internals_key,
                )
                return result

        print(
            f"internals_refactor: rewriting {len(formula_clusters)} clusters...",
            flush=True,
        )
        if lab.prompt_observer is not None:
            set_refactor_prompt_observer(lab.prompt_observer)
        if lab.cluster_context_observer is not None:
            set_cluster_context_observer(lab.cluster_context_observer)
        if lab.singleton_context_observer is not None:
            set_singleton_context_observer(lab.singleton_context_observer)
        refactor_started = time.perf_counter()
        try:
            run_result = refactor_internals_all_clusters(
                artifacts.refactor_projection,
                formula_clusters,
                internals_path=state.package_root / "internals.py",
                source_graph=artifacts.graph,
                internal_binding_index=artifacts.internal_binding_index,
                bound_address_keys=bound_address_keys,
                key_vocabulary=key_vocabulary,
                bindings_path=config.bindings_path,
                workbook_path=config.workbook_path,
                address_to_series_id=address_to_series_id,
                constraints=config.constraints,
                refactor_schedule=cluster_result.schedule,
                timer=timer,
                codegen_cache_key=state.codegen_cache_key,
                dry_run=lab.dry_run,
                parity_gate=lab.parity_gate,
            )
        finally:
            set_refactor_prompt_observer(None)
            set_cluster_context_observer(None)
            set_singleton_context_observer(None)
        refactor_seconds = time.perf_counter() - refactor_started
        # Do not record an ``internals_refactor`` rollup span: Pass 1 leaf spans
        # and parity/pass2/phase_c already partition that work on the same timer.
        print(
            f"internals_refactor: done ({refactor_seconds:.1f}s)",
            flush=True,
        )

        # Recompute the key after the run so newly written LLM cache entries
        # participate in the digest (first-run → second-run warm hit).
        cacheable = getattr(run_result, "cacheable", False)
        final_source = getattr(run_result, "final_source", None)
        resolved_internals_key: str | None = None
        if not no_cache and cacheable and isinstance(final_source, str):
            post_digest = consumed_refactors_digest()
            post_key = internals_cache_key(
                codegen_cache_key=state.codegen_cache_key,
                clusters_cache_key=cluster_result.cache_key,
                consumed_refactors_digest=post_digest,
            )
            save_refactored_internals_payload(
                final_source,
                cache_key=post_key,
                codegen_cache_key=state.codegen_cache_key,
                clusters_cache_key=cluster_result.cache_key,
                consumed_refactors_digest=post_digest,
            )
            materialize_package(
                config,
                codegen_key=state.codegen_cache_key,
                internals_key=post_key,
            )
            resolved_internals_key = post_key
            record_cache_outcome(
                timings,
                "internals",
                cache_hit=False,
                elapsed_seconds=refactor_seconds,
                cache_key=post_key,
            )
            print(
                f"internals: cache store (key={post_key[:12]})",
                flush=True,
            )
        result = RefactorStageState(
            config=config,
            codegen_cache_key=state.codegen_cache_key,
            internals_cache_key=resolved_internals_key,
            clusters_cache_key=cluster_result.cache_key,
        )
        _write_refactor_manifest(
            config,
            export_state=state,
            clusters_cache_key=cluster_result.cache_key,
            internals_cache_key=resolved_internals_key,
        )
        return result


def run_annotate_stage(
    state: ExportStageState,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
    timings: PipelineTimings | None = None,
) -> AnnotateStageState:
    """Write LLM docstrings onto the inverted-tree package."""
    from src.inverted_tree_docstrings import annotate_exported_package

    config = state.config
    with (
        profile_if_enabled(config.graph_output_dir, basename="annotate"),
        stage_span(timings, "annotate") as timer,
    ):
        started = time.perf_counter()
        annotate_exported_package(
            config, no_cache=no_cache, force_rebuild=force_rebuild
        )
        timer.record("annotate_docstrings", time.perf_counter() - started)
        print("annotate: inverted-tree docstrings written", flush=True)
    result = AnnotateStageState(
        config=config,
        codegen_cache_key=state.codegen_cache_key,
    )
    write_stage_manifest(
        config,
        stage="annotate",
        cache_keys={"codegen_cache_key": result.codegen_cache_key},
        upstream_keys={"codegen_cache_key": state.codegen_cache_key},
        fingerprints=compute_input_fingerprints(config),
    )
    return result


def annotate_stage_state_from_manifest(
    config: PipelineConfig,
    manifest: StageManifest,
) -> AnnotateStageState:
    """Build ``AnnotateStageState`` from an annotate-stage (or compatible) manifest."""
    codegen_key = manifest.cache_keys.get("codegen_cache_key")
    if not codegen_key:
        raise StageManifestError(
            f"manifest for {manifest.stage!r} missing codegen_cache_key"
        )
    return AnnotateStageState(config=config, codegen_cache_key=codegen_key)


def _write_downstream_manifest(
    config: PipelineConfig,
    *,
    stage: PipelineStageName,
    codegen_cache_key: str,
) -> None:
    cache_keys = {"codegen_cache_key": codegen_cache_key}
    write_stage_manifest(
        config,
        stage=stage,
        cache_keys=cache_keys,
        upstream_keys=dict(cache_keys),
        fingerprints=compute_input_fingerprints(config),
    )


def run_validate_stage(
    state: AnnotateStageState | RefactorStageState,
    *,
    no_cache: bool = False,
    timings: PipelineTimings | None = None,
) -> int | None:
    """Run FormulaEvaluator parity on the inverted-tree package.

    Returns 0 when every compared cell matches, otherwise 1. Non-zero does not
    abort the pipeline; document may still run with ``--force-document``.
    """
    from src.inverted_tree_validate import write_formula_evaluator_parity_reports

    del no_cache
    config = state.config
    with (
        profile_if_enabled(config.graph_output_dir, basename="validate"),
        stage_span(timings, "validate") as timer,
    ):
        started = time.perf_counter()
        report_dir = config.dist_root / "tests" / "results" / "reference"
        exit_code = write_formula_evaluator_parity_reports(
            config, report_dir=report_dir
        )
        timer.record("formula_evaluator_parity", time.perf_counter() - started)
        print(
            f"validate: FormulaEvaluator parity exit={exit_code}",
            flush=True,
        )
    _write_downstream_manifest(
        config, stage="validate", codegen_cache_key=state.codegen_cache_key
    )
    return exit_code


def run_document_stage(
    config: PipelineConfig,
    *,
    timings: PipelineTimings | None = None,
    annotate_state: AnnotateStageState | RefactorStageState | None = None,
    refactor_state: AnnotateStageState | RefactorStageState | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    """Rewrite the user guide against the exported package.

    Raises :class:`DocumentStageError` on failure; the export and differential
    artifacts written by earlier stages are left in place for diagnosis.
    """
    from src.documentation_pipeline import run_documentation_pipeline

    downstream = annotate_state if annotate_state is not None else refactor_state
    with (
        profile_if_enabled(config.graph_output_dir, basename="document"),
        stage_span(timings, "document"),
    ):
        try:
            run_documentation_pipeline(
                config,
                no_cache=no_cache,
                force_rebuild=force_rebuild,
            )
        except Exception as error:
            logger.exception(
                "Document stage failed after export/differential artifacts were written"
            )
            print(
                "Document stage failed; export package and differential reports under "
                f"{config.dist_root} and {config.differential_report_dir_rel} are "
                "preserved for diagnosis.",
                flush=True,
            )
            raise DocumentStageError(f"document stage failed: {error}") from error
    if downstream is not None:
        _write_downstream_manifest(
            config, stage="document", codegen_cache_key=downstream.codegen_cache_key
        )


def run_pipeline(
    config: PipelineConfig,
    *,
    start_from_stage: PipelineStageName | str = "extract",
    stop_after_stage: PipelineStageName | str = "document",
    only_stage: PipelineStageName | str | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    force_document: bool = False,
    lab_options: RefactorLabOptions | None = None,
) -> None:
    """Run pipeline stages from ``start_from_stage`` through ``stop_after_stage``.

    A full run records ``extract`` then ``export`` (handing the live graph from
    extract into export so the graph is not built twice), then ``annotate``,
    ``validate``, and ``document``. When ``only_stage`` is set it overrides both
    bounds to that single stage. Entering mid-pipeline requires a warm upstream
    stage manifest whose fingerprints still match the current inputs.
    """
    if only_stage is not None:
        if only_stage not in PIPELINE_STAGES:
            raise ValueError(
                f"unknown pipeline stage {only_stage!r}; "
                f"expected one of {list(PIPELINE_STAGES)}"
            )
        start_from_stage = only_stage
        stop_after_stage = only_stage
    if start_from_stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown pipeline stage {start_from_stage!r}; "
            f"expected one of {list(PIPELINE_STAGES)}"
        )
    if stop_after_stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown pipeline stage {stop_after_stage!r}; "
            f"expected one of {list(PIPELINE_STAGES)}"
        )
    start_index = PIPELINE_STAGES.index(start_from_stage)
    stop_index = PIPELINE_STAGES.index(stop_after_stage)
    if start_index > stop_index:
        raise ValueError(
            f"start_from_stage {start_from_stage!r} is after "
            f"stop_after_stage {stop_after_stage!r}"
        )

    timings = PipelineTimings(output_path=stage_timings_path(config.repo_root))
    try:
        _run_pipeline_stages(
            config,
            timings=timings,
            start_from_stage=start_from_stage,
            stop_after_stage=stop_after_stage,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            force_document=force_document,
            lab_options=lab_options,
        )
    finally:
        timings.flush()
        print(f"Stage timings: {timings.output_path}", flush=True)


def _stage_in_range(
    stage: PipelineStageName,
    *,
    start_from_stage: PipelineStageName,
    stop_after_stage: PipelineStageName,
) -> bool:
    start_index = PIPELINE_STAGES.index(start_from_stage)
    stop_index = PIPELINE_STAGES.index(stop_after_stage)
    stage_index = PIPELINE_STAGES.index(stage)
    return start_index <= stage_index <= stop_index


def _run_pipeline_stages(
    config: PipelineConfig,
    *,
    timings: PipelineTimings,
    start_from_stage: PipelineStageName,
    stop_after_stage: PipelineStageName,
    no_cache: bool,
    force_rebuild: bool,
    force_document: bool,
    lab_options: RefactorLabOptions | None = None,
) -> None:
    del lab_options
    export_state: ExportStageState | None = None
    annotate_state: AnnotateStageState | None = None
    extracted_graph: DependencyGraph | None = None
    extracted_graph_cache_key: str | None = None

    if start_from_stage != "extract":
        upstream = require_upstream_manifest(config, start_from_stage=start_from_stage)
        if start_from_stage == "annotate":
            export_state = export_stage_state_from_manifest(config, upstream)
            materialize_package(
                config,
                codegen_key=export_state.codegen_cache_key,
                apply_rewrites=False,
            )
        elif start_from_stage in ("validate", "document"):
            annotate_state = annotate_stage_state_from_manifest(config, upstream)
            materialize_package(
                config,
                codegen_key=annotate_state.codegen_cache_key,
                apply_rewrites=False,
            )
            from src.inverted_tree_docstrings import annotate_exported_package

            annotate_exported_package(config)

    if _stage_in_range(
        "extract",
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
    ):
        extract_result = extract_dependency_graph(
            config,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
        )
        extracted_graph = extract_result.graph
        extracted_graph_cache_key = extract_result.graph_cache_key
        if stop_after_stage == "extract":
            return

    if _stage_in_range(
        "export",
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
    ):
        export_state = run_export_stage(
            config,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
            graph=extracted_graph,
            graph_cache_key=extracted_graph_cache_key,
        )
        if stop_after_stage == "export":
            return

    if _stage_in_range(
        "annotate",
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
    ):
        if export_state is None:
            raise StageManifestError(
                "annotate stage requires export stage state or a warm export manifest"
            )
        annotate_state = run_annotate_stage(
            export_state,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
            timings=timings,
        )
        if stop_after_stage == "annotate":
            return

    differential_exit_code: int | None = None
    if _stage_in_range(
        "validate",
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
    ):
        if annotate_state is None:
            raise StageManifestError(
                "validate stage requires annotate stage state or a warm annotate "
                "manifest"
            )
        differential_exit_code = run_validate_stage(
            annotate_state,
            no_cache=no_cache,
            timings=timings,
        )
        if stop_after_stage == "validate":
            return

    if _stage_in_range(
        "document",
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
    ):
        if (
            isinstance(differential_exit_code, int)
            and differential_exit_code != 0
            and not force_document
        ):
            print(
                "Skipping document stage because FormulaEvaluator parity "
                f"exited with code {differential_exit_code}. Export artifacts "
                "are ready for diagnosis; pass --force-document to rewrite "
                "guides anyway.",
                flush=True,
            )
            return
        run_document_stage(
            config,
            timings=timings,
            annotate_state=annotate_state,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )


def export_generated_package(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    """Write the generated package under dist/ through the validate stage."""
    run_pipeline(
        config,
        stop_after_stage="validate",
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )


def main(argv: Sequence[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run the extraction pipeline.")
    entry_group = parser.add_mutually_exclusive_group()
    entry_group.add_argument(
        "--start-from-stage",
        choices=PIPELINE_STAGES,
        default=None,
        help=(
            "Resume the pipeline at the named stage using the upstream stage "
            f"manifest under artifacts/stages/. Stages: {', '.join(PIPELINE_STAGES)}."
        ),
    )
    entry_group.add_argument(
        "--only-stage",
        choices=PIPELINE_STAGES,
        default=None,
        help=(
            "Run exactly one stage from its upstream stage manifest. "
            "Mutually exclusive with --start-from-stage and --stop-after-stage."
        ),
    )
    stop_group = parser.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--stop-after-stage",
        choices=PIPELINE_STAGES,
        default=None,
        help=(
            "Run pipeline stages through the named stage and exit. "
            f"Stages in order: {', '.join(PIPELINE_STAGES)}."
        ),
    )
    stop_group.add_argument(
        "--extract-graph",
        action="store_true",
        help="Alias for --stop-after-stage extract.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Bypass on-disk graph, projection, series-resolution, series-derived, "
            "bindings-validation, codegen, cluster, refactored-internals, and "
            "exported-library differential caches for this run."
        ),
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help=(
            "Rebuild on-disk graph, projection, series-resolution, series-derived, "
            "bindings-validation, codegen, cluster, and refactored-internals caches "
            "even when a warm entry exists."
        ),
    )
    parser.add_argument(
        "--force-document",
        action="store_true",
        help=(
            "Run the document stage even when exported-library differential "
            "finished with a non-zero exit code."
        ),
    )
    add_variation_mode_argument(parser)
    add_clustering_mode_argument(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.only_stage is not None and (
        args.stop_after_stage is not None or args.extract_graph
    ):
        parser.error(
            "--only-stage cannot be combined with --stop-after-stage or --extract-graph"
        )

    config = apply_clustering_mode_cli_override(
        apply_variation_mode_cli_override(load_pipeline_config(), args.variation_mode),
        args.clustering_mode,
    )
    validate_pipeline_config(config)

    only_stage: PipelineStageName | None = None
    start_from_stage: PipelineStageName = "extract"
    stop_after_stage: PipelineStageName = "document"
    if args.only_stage is not None:
        only_stage = cast(PipelineStageName, args.only_stage)
        start_from_stage = only_stage
        stop_after_stage = only_stage
    else:
        if args.start_from_stage is not None:
            start_from_stage = cast(PipelineStageName, args.start_from_stage)
        if args.extract_graph:
            stop_after_stage = "extract"
        elif args.stop_after_stage is not None:
            stop_after_stage = cast(PipelineStageName, args.stop_after_stage)

    run_pipeline(
        config,
        start_from_stage=start_from_stage,
        stop_after_stage=stop_after_stage,
        only_stage=only_stage,
        no_cache=args.no_cache,
        force_rebuild=args.force_rebuild,
        force_document=args.force_document,
    )


if __name__ == "__main__":
    main()
