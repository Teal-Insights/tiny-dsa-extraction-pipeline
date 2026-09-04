"""Opt-in inverted-tree codegen on the pipeline graph (excel-grapher #597).

Default ``paradigm="ctx"`` export is unchanged. This path calls
``generate_modules(..., paradigm="inverted_tree")`` on the graph already built
with ``CONSTRAINTS`` / ``DynamicRefConfig``. It does not go through
``excel-grapher bindings validate`` (that CLI does not apply pipeline
constraints) and does not apply ctx-only ``dist/`` rewrites.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from excel_grapher.exporter import CodeGenerator
from excel_grapher.grapher import DependencyGraph
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.codegen_cache import write_generated_modules
from src.pipeline_config import PipelineConfig


def inverted_tree_package_root(config: PipelineConfig) -> Path:
    """Sibling of the ctx package: ``dist/<package>_inverted/``."""
    return config.dist_root / f"{config.dist_metadata.package_name}_inverted"


def generate_inverted_tree_modules(
    config: PipelineConfig,
    *,
    graph: DependencyGraph,
    series_bindings: WorkbookSeriesBindings,
) -> dict[str, str]:
    """Emit inverted-tree modules from an already-constrained pipeline graph."""
    with CodeGenerator(graph) as generator:
        return generator.generate_modules(
            list(config.targets),
            series_bindings=series_bindings,
            bindings_workbook=config.workbook_path,
            paradigm="inverted_tree",
        )


def write_inverted_tree_package(
    config: PipelineConfig, modules: Mapping[str, str]
) -> Path:
    """Write inverted-tree modules without ctx ``apply_export_rewrites``."""
    package_root = inverted_tree_package_root(config)
    write_generated_modules(package_root, modules)
    return package_root
