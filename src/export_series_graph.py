"""Seed the interactive series-graph API + docs assets into ``dist/``.

Copies FormulaEvaluator-backed graph modules, static Cytoscape UI, and local
serve scripts from ``templates/series-graph/``. Workbook-specific topology
lives in ``{package}/graph_schema.py`` (scaffold + worked reference example).

Python modules and scripts carry ``__SERIES_GRAPH_*__`` placeholders for the
package name and workbook fixture filename; they are rendered on copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.pipeline_config import PipelineConfig

SERIES_GRAPH_TEMPLATE_REL = Path("templates/series-graph")
PACKAGE_MODULES = (
    "graph_api.py",
    "graph_schema.py",
    "graph_formula_evaluator.py",
)
SCRIPTS = (
    "serve_graph_api.py",
    "write_graph_bootstrap.py",
    "check_graph_eval.py",
)
PACKAGE_PLACEHOLDER = "__SERIES_GRAPH_PACKAGE__"
WORKBOOK_FIXTURE_PLACEHOLDER = "__SERIES_GRAPH_WORKBOOK_FIXTURE__"
REFERENCE_EXAMPLE = "reference_graph_schema.py"


def series_graph_template_root(config: PipelineConfig) -> Path:
    return config.repo_root / SERIES_GRAPH_TEMPLATE_REL


def _copy_rendered(source: Path, destination: Path, *, config: PipelineConfig) -> None:
    text = source.read_text(encoding="utf-8")
    text = text.replace(PACKAGE_PLACEHOLDER, config.dist_metadata.package_name)
    text = text.replace(
        WORKBOOK_FIXTURE_PLACEHOLDER, config.differential_workbook_rel.name
    )
    if "__SERIES_GRAPH_" in text:
        raise ValueError(f"unrendered series-graph placeholder in {source}")
    destination.write_text(text, encoding="utf-8")
    shutil.copymode(source, destination)


def seed_series_graph(*, config: PipelineConfig) -> None:
    """Install series-graph Python modules, assets, and scripts under ``dist/``."""
    template_root = series_graph_template_root(config)
    if not template_root.is_dir():
        raise FileNotFoundError(f"series-graph template missing: {template_root}")

    package_src = template_root / "package"
    package_dst = config.package_root
    package_dst.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_MODULES:
        source = package_src / name
        if not source.is_file():
            raise FileNotFoundError(f"series-graph package module missing: {source}")
        _copy_rendered(source, package_dst / name, config=config)

    assets_src = template_root / "assets" / "graph"
    assets_dst = config.dist_root / "assets" / "graph"
    if assets_dst.exists():
        shutil.rmtree(assets_dst)
    shutil.copytree(assets_src, assets_dst)

    scripts_src = template_root / "scripts"
    scripts_dst = config.dist_root / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        source = scripts_src / name
        if not source.is_file():
            raise FileNotFoundError(f"series-graph script missing: {source}")
        _copy_rendered(source, scripts_dst / name, config=config)

    example_src = template_root / "examples" / REFERENCE_EXAMPLE
    if example_src.is_file():
        examples_dst = config.dist_root / "examples"
        examples_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(example_src, examples_dst / REFERENCE_EXAMPLE)

    readme_src = template_root / "README.md"
    if readme_src.is_file():
        shutil.copy2(readme_src, config.dist_root / "assets" / "graph" / "README.md")
