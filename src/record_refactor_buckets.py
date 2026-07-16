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
    add_clustering_mode_argument,
    add_variation_mode_argument,
    apply_clustering_mode_cli_override,
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
    build_address_to_series_id,
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
from src.refactor_order import compute_refactor_schedule
from src.semantic_naming import (
    allocate_schedule_helper_names,
    collect_semantic_helper_names,
)
from src.subgraph_projection import build_refactor_projection
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address

REFACTOR_BUCKETS_SCHEMA_VERSION = "1.4.0"

SERIES_PARTITION_NOTE = (
    "Refactor partitions use internal series ids first, then public output/input "
    "binding series ids. Cells with no internal-series owner are not automatically "
    "singleton refactor units: a multi-member public binding series (for example an "
    "output time sweep) remains one cluster refactor unit so collapse can emit "
    "_ADDRESS_DISPATCH entries keyed by binding parameters such as TIME_PERIOD."
)
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
    refactor_group_id: int
    cluster_id: int
    kind: RefactorKind
    eligible: bool
    skip_reason: str | None
    contract: ClusterRefactorContract | None
    row: int | None
    member_count: int
    members: tuple[str, ...]
    function_names: tuple[str, ...]
    series_ids: tuple[str, ...]
    canonical_template: str
    external_dependencies: tuple[str, ...]
    fingerprint_group_count: int | None = None
    relation_tiers: tuple[str, ...] | None = None
    fingerprint_fallback_reason: str | None = None
    legacy_token_estimate: int | None = None
    fingerprint_token_estimate: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "refactor_order": self.refactor_order,
            "refactor_group_id": self.refactor_group_id,
            "cluster_id": self.cluster_id,
            "kind": self.kind,
            "eligible": self.eligible,
            "skip_reason": self.skip_reason,
            "contract": self.contract,
            "row": self.row,
            "member_count": self.member_count,
            "members": list(self.members),
            "function_names": list(self.function_names),
            "series_ids": list(self.series_ids),
            "canonical_template": self.canonical_template,
            "external_dependencies": list(self.external_dependencies),
        }
        if self.kind == "cluster":
            payload["fingerprint_group_count"] = self.fingerprint_group_count
            payload["relation_tiers"] = (
                list(self.relation_tiers) if self.relation_tiers is not None else None
            )
            payload["fingerprint_fallback_reason"] = self.fingerprint_fallback_reason
            payload["legacy_token_estimate"] = self.legacy_token_estimate
            payload["fingerprint_token_estimate"] = self.fingerprint_token_estimate
        return payload


def _defined_function_names(internals_source: str) -> set[str]:
    module = ast.parse(internals_source)
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def _member_engine_column(
    address: str,
    layout: ProjectionColumnLayout | None,
) -> str | None:
    """Mirror ``internals_refactor._member_engine_column`` for eligibility checks.

    When no projection layout is configured, fall back to the address column letter
    so non-engine public series (scenario sheets, etc.) are not false-skipped.
    """
    if layout is not None:
        column = layout.logical_engine_column(address)
        if column is not None:
            return column
    _, column, _row = parse_workbook_address(address)
    return column


def _series_ids_for_members(
    members: Sequence[str],
    address_to_series_id: Mapping[str, str] | None,
) -> tuple[str, ...]:
    if address_to_series_id is None:
        return ()
    series_ids = {
        address_to_series_id[address]
        for address in members
        if address in address_to_series_id
    }
    return tuple(sorted(series_ids))


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

    if workbook_path is None:
        return None, None

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

    with CodeGenerator(
        cast(GraphLike, refactor_projection), unpack_return=True
    ) as generator:
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
    address_to_series_id: Mapping[str, str] | None = None,
) -> tuple[RefactorBucketRecord, ...]:
    """Classify formula clusters into singleton and cluster refactor target buckets."""
    resolved_bound_keys = _require_bound_address_keys(bound_address_keys)
    key_vocabulary = load_key_concept_vocabulary(config.bindings_path)
    resolved_address_to_series_id = (
        address_to_series_id if address_to_series_id is not None else {}
    )
    clusters = cluster_graph_formulas(
        graph,
        bound_address_keys=resolved_bound_keys,
        variation_mode=config.variation_mode,
        clustering_mode=config.clustering_mode,
        address_to_series_id=address_to_series_id,
        workbook_path=config.workbook_path,
        layout=layout,
    )
    ordered_units = compute_refactor_schedule(graph, clusters)

    internals_source = (
        internals_path.read_text(encoding="utf-8") if internals_path is not None else ""
    )
    use_refactor_context = (
        compression == "optimal"
        and refactor_graph is not None
        and internals_path is not None
    )
    existing_helper_names = (
        collect_semantic_helper_names(internals_source)
        if use_refactor_context
        else frozenset()
    )
    allocated_helper_names: tuple[str, ...] | None = None
    if use_refactor_context and resolved_address_to_series_id:
        allocated_helper_names = allocate_schedule_helper_names(
            tuple(unit.members for unit in ordered_units),
            resolved_address_to_series_id,
            existing_names=existing_helper_names,
        )

    records: list[RefactorBucketRecord] = []
    for refactor_order, unit in enumerate(ordered_units):
        cluster = unit.as_formula_cluster()
        kind: RefactorKind = "singleton" if len(cluster.members) == 1 else "cluster"
        contract: ClusterRefactorContract | None = None
        helper_name = (
            allocated_helper_names[refactor_order]
            if allocated_helper_names is not None
            else None
        )
        reserved_for_others = (
            (frozenset(allocated_helper_names) | existing_helper_names)
            - ({helper_name} if helper_name is not None else set())
            if allocated_helper_names is not None
            else None
        )
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
                    address_to_series_id=resolved_address_to_series_id,
                    expected_helper_name=helper_name,
                    existing_helper_names=reserved_for_others,
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
                    bound_address_keys=resolved_bound_keys,
                    address_to_series_id=resolved_address_to_series_id,
                    expected_helper_name=helper_name,
                    existing_helper_names=reserved_for_others,
                )
            )

        function_names = tuple(
            address_to_function_name(address) for address in cluster.members
        )
        eligible = compression == "none" or ctx is not None
        fingerprint_group_count = None
        relation_tiers = None
        fingerprint_fallback_reason = None
        legacy_token_estimate = None
        fingerprint_token_estimate = None
        if (
            kind == "cluster"
            and isinstance(ctx, ClusterRefactorContext)
            and ctx.fingerprint_summary is not None
        ):
            summary = ctx.fingerprint_summary
            fingerprint_group_count = summary.fingerprint_group_count
            relation_tiers = summary.relation_tiers
            fingerprint_fallback_reason = summary.fallback_reason
            legacy_token_estimate = summary.legacy_token_estimate
            fingerprint_token_estimate = summary.fingerprint_token_estimate
        records.append(
            RefactorBucketRecord(
                refactor_order=refactor_order,
                refactor_group_id=unit.refactor_group_id,
                cluster_id=unit.parent_cluster_id,
                kind=kind,
                eligible=eligible,
                skip_reason=skip_reason,
                contract=contract,
                row=cluster.row,
                member_count=len(cluster.members),
                members=cluster.members,
                function_names=function_names,
                series_ids=_series_ids_for_members(
                    cluster.members, resolved_address_to_series_id
                ),
                canonical_template=cluster.canonical_template,
                external_dependencies=_external_dependencies(ctx),
                fingerprint_group_count=fingerprint_group_count,
                relation_tiers=relation_tiers,
                fingerprint_fallback_reason=fingerprint_fallback_reason,
                legacy_token_estimate=legacy_token_estimate,
                fingerprint_token_estimate=fingerprint_token_estimate,
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
    formula_cluster_count = len({record.cluster_id for record in records})
    refactor_unit_count = len(records)
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
        "series_partition_note": SERIES_PARTITION_NOTE,
        "formula_cluster_count": formula_cluster_count,
        "refactor_unit_count": refactor_unit_count,
        "cluster_count": refactor_unit_count,
        "refactor_target_count": len(eligible_records),
        "skipped_target_count": len(records) - len(eligible_records),
        "buckets": [record.to_dict() for record in records],
        "summary_by_kind": _summary_by_kind(records),
        "fingerprint_summary": _fingerprint_coverage_summary(records),
    }


def _fingerprint_coverage_summary(
    records: Sequence[RefactorBucketRecord],
) -> dict[str, Any]:
    cluster_records = [record for record in records if record.kind == "cluster"]
    with_summary = [
        record
        for record in cluster_records
        if record.fingerprint_group_count is not None
        or record.fingerprint_fallback_reason is not None
    ]
    fallback_count = sum(
        1 for record in with_summary if record.fingerprint_fallback_reason is not None
    )
    tier_counts: dict[str, int] = {}
    for record in with_summary:
        if record.relation_tiers is None:
            continue
        for tier in record.relation_tiers:
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
    legacy_tokens = sum(record.legacy_token_estimate or 0 for record in with_summary)
    fingerprint_tokens = sum(
        record.fingerprint_token_estimate or 0
        for record in with_summary
        if record.fingerprint_fallback_reason is None
    )
    return {
        "cluster_bucket_count": len(cluster_records),
        "summarized_cluster_count": len(with_summary),
        "fallback_cluster_count": fallback_count,
        "relation_tier_counts": dict(sorted(tier_counts.items())),
        "legacy_token_estimate_total": legacy_tokens,
        "fingerprint_token_estimate_total": fingerprint_tokens,
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
            "## Series partition notes",
            "",
            str(report.get("series_partition_note", SERIES_PARTITION_NOTE)),
            "",
            "## Summary",
            "",
            f"- Fingerprint formula clusters: **{report['formula_cluster_count']}**",
            f"- Refactor schedule units: **{report['refactor_unit_count']}**",
            f"- Eligible refactor targets: **{report['refactor_target_count']}**",
            f"- Skipped refactor targets: **{report['skipped_target_count']}**",
            "",
        ]
    )
    fingerprint_summary = report.get("fingerprint_summary")
    if isinstance(fingerprint_summary, Mapping):
        tier_counts = fingerprint_summary.get("relation_tier_counts") or {}
        tier_text = (
            ", ".join(f"{tier}={count}" for tier, count in sorted(tier_counts.items()))
            or "none"
        )
        lines.extend(
            [
                "## Fingerprint prompt coverage",
                "",
                f"- Summarized cluster buckets: "
                f"**{fingerprint_summary.get('summarized_cluster_count', 0)}**",
                f"- Fingerprint dump fallbacks: "
                f"**{fingerprint_summary.get('fallback_cluster_count', 0)}**",
                f"- Relation tiers: {tier_text}",
                (
                    "- Token estimates (legacy sources vs fingerprint): "
                    f"**{fingerprint_summary.get('legacy_token_estimate_total', 0)}** vs "
                    f"**{fingerprint_summary.get('fingerprint_token_estimate_total', 0)}**"
                ),
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
            f"Group {bucket['refactor_group_id']} / cluster {bucket['cluster_id']} "
            f"({bucket['kind']}, {status})"
        )
        lines.append("")
        if bucket["contract"] is not None:
            lines.append(f"- Contract: {bucket['contract']}")
        if bucket["series_ids"]:
            lines.append(f"- Series: `{', '.join(bucket['series_ids'])}`")
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
    address_to_series_id = build_address_to_series_id(
        graph_result.internal_series,
        output_series=graph_result.output_series,
        input_series=graph_result.input_series,
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
        address_to_series_id=address_to_series_id,
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
    add_clustering_mode_argument(parser)
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

    config = apply_clustering_mode_cli_override(
        apply_variation_mode_cli_override(load_pipeline_config(), args.variation_mode),
        args.clustering_mode,
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
        f"{report['refactor_unit_count']} schedule units "
        f"({report['formula_cluster_count']} fingerprint clusters)"
    )
    for bucket in report["buckets"]:
        status = (
            "eligible" if bucket["eligible"] else f"skipped ({bucket['skip_reason']})"
        )
        print(
            f"  [{bucket['refactor_order']}] group {bucket['refactor_group_id']} "
            f"cluster {bucket['cluster_id']} "
            f"{bucket['kind']} {status}: {', '.join(bucket['members'])}"
        )


if __name__ == "__main__":
    main()
