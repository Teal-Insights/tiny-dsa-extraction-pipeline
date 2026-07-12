"""Run export through codegen and record internals refactor target buckets.

Stops before the LLM refactor step. Writes a machine-readable JSON report and a
human-readable markdown summary under ``artifacts/``.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from excel_grapher.exporter import CodeGenerator, ProjectionResult
from excel_grapher.exporter.codegen import GraphLike
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.docstring_callback import configure_docstring_callback
from src.extraction_pipeline import build_pipeline_graph
from src.pipeline_config import (
    PipelineConfig,
    add_variation_mode_argument,
    apply_variation_mode_cli_override,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.pipeline_context import activate_pipeline_config
from src.formula_clustering import (
    BoundAddressKeys,
    ClusterableGraph,
    FormulaCluster,
    _require_bound_address_keys,
    cluster_graph_formulas,
    formula_nodes_for_clustering,
)
from src.refactor_bindings import (
    KeyConceptSpec,
    build_bound_address_keys,
    load_key_concept_vocabulary,
    varying_key_concepts,
)
from src.refactor_contracts import (
    ClusterRefactorContract,
    select_cluster_refactor_contract,
)
from src.internal_bindings import InternalBindingIndex, build_internal_binding_index
from src.internals_refactor import (
    ClusterRefactorContext,
    SingletonRefactorContext,
    address_to_function_name,
    build_cluster_refactor_context,
    build_singleton_refactor_context,
)
from src.logging_config import configure_logging
from src.refactor_order import compute_cluster_refactor_order
from src.subgraph_projection import build_refactor_projection
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address

REFACTOR_BUCKETS_SCHEMA_VERSION = "1.1.0"
DEFAULT_JSON_OUTPUT = Path("artifacts/refactor-buckets.json")
DEFAULT_MARKDOWN_OUTPUT = Path("artifacts/refactor-buckets.md")
DEFAULT_JSON_OUTPUT_UNCOMPRESSED = Path("artifacts/refactor-buckets-uncompressed.json")
DEFAULT_MARKDOWN_OUTPUT_UNCOMPRESSED = Path(
    "artifacts/refactor-buckets-uncompressed.md"
)
DEFAULT_CODEGEN_DIST_ROOT = Path("artifacts/refactor-bucket-codegen")

RefactorKind = Literal["singleton", "cluster"]
CompressionMode = Literal["optimal", "none"]


@dataclass(frozen=True)
class RefactorBucketRecord:
    refactor_order: int
    cluster_id: int
    kind: RefactorKind
    eligible: bool
    skip_reason: str | None
    contract: ClusterRefactorContract | None
    row: int | None
    member_count: int
    members: tuple[str, ...]
    function_names: tuple[str, ...]
    canonical_template: str
    external_dependencies: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "refactor_order": self.refactor_order,
            "cluster_id": self.cluster_id,
            "kind": self.kind,
            "eligible": self.eligible,
            "skip_reason": self.skip_reason,
            "contract": self.contract,
            "row": self.row,
            "member_count": self.member_count,
            "members": list(self.members),
            "function_names": list(self.function_names),
            "canonical_template": self.canonical_template,
            "external_dependencies": list(self.external_dependencies),
        }


def _defined_function_names(internals_source: str) -> set[str]:
    module = ast.parse(internals_source)
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def _member_engine_column(
    address: str,
    layout: ProjectionColumnLayout | None,
) -> str | None:
    if layout is None:
        return None
    return layout.logical_engine_column(address)


def _singleton_skip_reason(
    graph: ClusterableGraph,
    cluster: FormulaCluster,
    internals_source: str,
) -> str | None:
    if len(cluster.members) != 1:
        return "singleton_cluster_has_multiple_members"
    address = cluster.members[0]
    function_name = address_to_function_name(address)
    if function_name not in _defined_function_names(internals_source):
        return "missing_generated_function"
    node = graph.get_node(address)
    if node is None or node.normalized_formula is None:
        return "missing_graph_formula_node"
    return None


def _cluster_contract_and_skip_reason(
    graph: ClusterableGraph,
    cluster: FormulaCluster,
    internals_source: str,
    *,
    layout: ProjectionColumnLayout | None,
    bound_address_keys: BoundAddressKeys,
    key_vocabulary: tuple[KeyConceptSpec, ...],
    workbook_path: Path | None = None,
) -> tuple[ClusterRefactorContract | None, str | None]:
    if len(cluster.members) < 2:
        return None, "cluster_has_fewer_than_two_members"

    defined_functions = _defined_function_names(internals_source)
    eligible_members = 0
    for address in cluster.members:
        function_name = address_to_function_name(address)
        if function_name not in defined_functions:
            continue
        if _member_engine_column(address, layout) is None:
            continue
        node = graph.get_node(address)
        if node is None or node.normalized_formula is None:
            continue
        eligible_members += 1

    if eligible_members < 2:
        return None, "cluster_has_fewer_than_two_graph_formula_members"

    if workbook_path is not None and layout is not None:
        varying = varying_key_concepts(
            cluster.members,
            bound_address_keys=bound_address_keys,
            workbook_path=workbook_path,
            layout=layout,
        )
        contract = select_cluster_refactor_contract(
            cluster,
            formula_nodes_for_clustering(graph),
            bound_address_keys,
            varying,
            key_vocabulary=key_vocabulary,
            workbook_path=workbook_path,
            layout=layout,
        )
        if contract is None:
            return None, "operand_level_variation_unsupported"
        return contract, None
    return None, None


def _external_dependencies(
    ctx: SingletonRefactorContext | ClusterRefactorContext | None,
) -> tuple[str, ...]:
    if ctx is None:
        return ()
    return tuple(ctx.external_dependencies)


def export_generated_modules(
    config: PipelineConfig,
    *,
    graph: DependencyGraph,
    graph_cache_key: str,
    series_bindings: WorkbookSeriesBindings,
    no_cache: bool = False,
) -> Path:
    """Write generated package modules through codegen, stopping before refactor."""
    refactor_projection = build_refactor_projection(
        graph,
        graph_cache_key=graph_cache_key,
        no_cache=no_cache,
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
    for filepath, code in modules.items():
        output_path = package_root / filepath
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(code, encoding="utf-8", newline="\n")

    return package_root / "internals.py"


def record_refactor_buckets(
    config: PipelineConfig,
    *,
    graph: ClusterableGraph,
    internals_path: Path | None,
    internal_binding_index: InternalBindingIndex | None,
    layout: ProjectionColumnLayout | None,
    compression: CompressionMode = "optimal",
    refactor_graph: ProjectionResult | None = None,
    bound_address_keys: BoundAddressKeys | None,
) -> tuple[RefactorBucketRecord, ...]:
    """Classify formula clusters into singleton and cluster refactor target buckets."""
    resolved_bound_keys = _require_bound_address_keys(bound_address_keys)
    key_vocabulary = load_key_concept_vocabulary(config.bindings_path)
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=resolved_bound_keys,
        variation_mode=config.variation_mode,
        workbook_path=config.workbook_path,
        layout=layout,
    )
    ordered_clusters = compute_cluster_refactor_order(graph, clusters)

    internals_source = (
        internals_path.read_text(encoding="utf-8") if internals_path is not None else ""
    )
    use_refactor_context = (
        compression == "optimal"
        and refactor_graph is not None
        and internals_path is not None
    )

    records: list[RefactorBucketRecord] = []
    for refactor_order, cluster in enumerate(ordered_clusters):
        kind: RefactorKind = "singleton" if len(cluster.members) == 1 else "cluster"
        contract: ClusterRefactorContract | None = None
        if compression == "none":
            skip_reason = None
            ctx = None
        elif kind == "singleton":
            skip_reason = _singleton_skip_reason(graph, cluster, internals_source)
            ctx = (
                None
                if skip_reason is not None or not use_refactor_context
                else build_singleton_refactor_context(
                    refactor_graph,
                    cluster,
                    internals_path,
                    internal_binding_index=internal_binding_index,
                )
            )
        else:
            contract, skip_reason = _cluster_contract_and_skip_reason(
                graph,
                cluster,
                internals_source,
                layout=layout,
                bound_address_keys=resolved_bound_keys,
                key_vocabulary=key_vocabulary,
                workbook_path=config.workbook_path,
            )
            ctx = (
                None
                if skip_reason is not None or not use_refactor_context
                else build_cluster_refactor_context(
                    refactor_graph,
                    cluster,
                    internals_path,
                    internal_binding_index=internal_binding_index,
                    bindings_path=config.bindings_path,
                    workbook_path=config.workbook_path,
                    layout=layout,
                )
            )

        function_names = tuple(
            address_to_function_name(address) for address in cluster.members
        )
        eligible = compression == "none" or ctx is not None
        records.append(
            RefactorBucketRecord(
                refactor_order=refactor_order,
                cluster_id=cluster.cluster_id,
                kind=kind,
                eligible=eligible,
                skip_reason=skip_reason,
                contract=contract,
                row=cluster.row,
                member_count=len(cluster.members),
                members=cluster.members,
                function_names=function_names,
                canonical_template=cluster.canonical_template,
                external_dependencies=_external_dependencies(ctx),
            )
        )
    return tuple(records)


def _summary_by_kind(
    records: Sequence[RefactorBucketRecord],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for kind in ("singleton", "cluster"):
        kind_records = [record for record in records if record.kind == kind]
        summary[kind] = {
            "target_count": len(kind_records),
            "eligible_target_count": sum(
                1 for record in kind_records if record.eligible
            ),
            "skipped_target_count": sum(
                1 for record in kind_records if not record.eligible
            ),
            "cell_count": sum(record.member_count for record in kind_records),
            "eligible_cell_count": sum(
                record.member_count for record in kind_records if record.eligible
            ),
            "cells": sorted(
                {
                    address
                    for record in kind_records
                    if record.eligible
                    for address in record.members
                }
            ),
        }
    return summary


def build_refactor_buckets_report(
    config: PipelineConfig,
    records: Sequence[RefactorBucketRecord],
    *,
    internals_path: Path | None,
    compression: CompressionMode,
) -> dict[str, Any]:
    eligible_records = [record for record in records if record.eligible]
    return {
        "schema_version": REFACTOR_BUCKETS_SCHEMA_VERSION,
        "compression": compression,
        "workbook": config.repo_relative_posix_path(config.workbook_path),
        "targets": list(config.targets),
        "internals_path": (
            config.repo_relative_posix_path(internals_path)
            if internals_path is not None
            else None
        ),
        "cluster_count": len(records),
        "refactor_target_count": len(eligible_records),
        "skipped_target_count": len(records) - len(eligible_records),
        "buckets": [record.to_dict() for record in records],
        "summary_by_kind": _summary_by_kind(records),
    }


def render_refactor_buckets_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Refactor target buckets",
        "",
        f"Workbook: `{report['workbook']}`",
        f"Compression: `{report['compression']}`",
    ]
    if report["internals_path"] is not None:
        lines.append(f"Internals: `{report['internals_path']}`")
    lines.extend(
        [
            "",
            (
                "Formula clusters on the raw dependency graph."
                if report["compression"] == "none"
                else "Pipeline stopped after codegen and before the LLM internals refactor step."
            ),
            "",
            "## Summary",
            "",
            f"- Formula clusters: **{report['cluster_count']}**",
            f"- Eligible refactor targets: **{report['refactor_target_count']}**",
            f"- Skipped refactor targets: **{report['skipped_target_count']}**",
            "",
        ]
    )

    summary_by_kind = report["summary_by_kind"]
    for kind in ("singleton", "cluster"):
        summary = summary_by_kind[kind]
        lines.extend(
            [
                f"### {kind.title()} buckets",
                "",
                f"- Targets: {summary['target_count']} "
                f"({summary['eligible_target_count']} eligible, "
                f"{summary['skipped_target_count']} skipped)",
                f"- Cells covered by eligible targets: {summary['eligible_cell_count']} "
                f"of {summary['cell_count']}",
                "",
            ]
        )

    lines.extend(["## Refactor order", ""])
    for bucket in report["buckets"]:
        status = (
            "eligible" if bucket["eligible"] else f"skipped ({bucket['skip_reason']})"
        )
        sheet_rows = sorted(
            {
                f"{parse_workbook_address(address)[0]} row {parse_workbook_address(address)[2]}"
                for address in bucket["members"]
            }
        )
        lines.append(
            f"### {bucket['refactor_order'] + 1}. "
            f"Cluster {bucket['cluster_id']} ({bucket['kind']}, {status})"
        )
        lines.append("")
        if bucket["contract"] is not None:
            lines.append(f"- Contract: {bucket['contract']}")
        if bucket["row"] is not None:
            lines.append(f"- Representative row: {bucket['row']}")
        lines.append(
            f"- Members ({bucket['member_count']}): `{', '.join(bucket['members'])}`"
        )
        lines.append(f"- Generated functions: `{', '.join(bucket['function_names'])}`")
        if bucket["external_dependencies"]:
            lines.append(
                "- External dependencies: "
                + ", ".join(f"`{name}`" for name in bucket["external_dependencies"])
            )
        lines.append(f"- Canonical template: `{bucket['canonical_template']}`")
        lines.append(f"- Sheets/rows: {', '.join(sheet_rows)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def run_record_refactor_buckets(
    config: PipelineConfig,
    *,
    json_output: Path,
    markdown_output: Path,
    no_cache: bool = False,
    compression: CompressionMode = "optimal",
    codegen_dist_root: Path | None = None,
) -> dict[str, Any]:
    graph_result = build_pipeline_graph(config, no_cache=no_cache)
    projection = build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.graph_cache_key,
        no_cache=no_cache,
    )

    import workbook_config

    layout = getattr(workbook_config, "PROJECTION_LAYOUT", None)
    internals_path: Path | None = None
    internal_binding_index: InternalBindingIndex | None = None

    if compression == "optimal":
        codegen_root = (
            codegen_dist_root
            if codegen_dist_root is not None
            else config.repo_root / DEFAULT_CODEGEN_DIST_ROOT
        )
        codegen_config = replace(config, dist_root=codegen_root)
        internals_path = export_generated_modules(
            codegen_config,
            graph=graph_result.graph,
            graph_cache_key=graph_result.graph_cache_key,
            series_bindings=graph_result.series_bindings,
            no_cache=no_cache,
        )
        internal_binding_index = build_internal_binding_index(
            graph_result.internal_series
        )
        cluster_graph = projection
    else:
        cluster_graph = graph_result.graph

    bound_address_keys = build_bound_address_keys(
        graph_result.input_series,
        graph_result.output_series,
        graph_result.internal_series,
    )
    records = record_refactor_buckets(
        config,
        graph=cluster_graph,
        internals_path=internals_path,
        internal_binding_index=internal_binding_index,
        layout=layout,
        compression=compression,
        refactor_graph=projection if compression == "optimal" else None,
        bound_address_keys=bound_address_keys,
    )
    report = build_refactor_buckets_report(
        config,
        records,
        internals_path=internals_path,
        compression=compression,
    )

    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(
        render_refactor_buckets_markdown(report),
        encoding="utf-8",
    )
    return report


def main(argv: Sequence[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Run export through codegen and record internals refactor target buckets "
            "without invoking the LLM refactor step."
        )
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=DEFAULT_JSON_OUTPUT,
        help=f"JSON report path (default: {DEFAULT_JSON_OUTPUT.as_posix()}).",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=DEFAULT_MARKDOWN_OUTPUT,
        help=f"Markdown summary path (default: {DEFAULT_MARKDOWN_OUTPUT.as_posix()}).",
    )
    parser.add_argument(
        "--without-compression",
        action="store_true",
        help=(
            "Cluster formula cells on the raw dependency graph instead of the "
            "OptimalCompression projection."
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk graph and projection caches for this run.",
    )
    add_variation_mode_argument(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    json_output = (
        DEFAULT_JSON_OUTPUT_UNCOMPRESSED
        if args.without_compression
        else args.json_output
    )
    markdown_output = (
        DEFAULT_MARKDOWN_OUTPUT_UNCOMPRESSED
        if args.without_compression
        else args.markdown_output
    )

    config = apply_variation_mode_cli_override(
        load_pipeline_config(), args.variation_mode
    )
    validate_pipeline_config(config)
    activate_pipeline_config(config)

    report = run_record_refactor_buckets(
        config,
        json_output=json_output,
        markdown_output=markdown_output,
        no_cache=args.no_cache,
        compression="none" if args.without_compression else "optimal",
    )

    compression_label = "none (raw graph)" if args.without_compression else "optimal"
    print(f"Compression: {compression_label}")

    print(f"Wrote refactor bucket report to {json_output.resolve()}")
    print(f"Wrote refactor bucket summary to {markdown_output.resolve()}")
    print(
        "Refactor targets: "
        f"{report['refactor_target_count']} eligible / "
        f"{report['cluster_count']} clusters"
    )
    for bucket in report["buckets"]:
        status = (
            "eligible" if bucket["eligible"] else f"skipped ({bucket['skip_reason']})"
        )
        print(
            f"  [{bucket['refactor_order']}] cluster {bucket['cluster_id']} "
            f"{bucket['kind']} {status}: {', '.join(bucket['members'])}"
        )


if __name__ == "__main__":
    main()
