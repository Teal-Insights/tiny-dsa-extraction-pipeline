"""Tests for seeding the series-graph template into ``dist/``."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.export_series_graph import seed_series_graph
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from tests.fixtures.synthetic_pipeline import link_series_graph_template

_PROJECT_SPECIFIC = re.compile(r"tiny[ _-]?dsa|lic[ _-]?dsf|borvelia", re.IGNORECASE)


def _config(repo_root: Path) -> PipelineConfig:
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=repo_root / "data" / "workbook.xlsx",
        guide_path=repo_root / "data" / "guide.md",
        bindings_path=repo_root / "bindings",
        dist_root=repo_root / "dist",
        targets=("Sheet!A1",),
        constraints={},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
        user_guide_agent_prompt_path=repo_root / "templates" / "user-guide-agent.txt",
        differential_workbook_rel=Path("data/model-fixture.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=repo_root / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )


def _seed(tmp_path: Path) -> PipelineConfig:
    link_series_graph_template(tmp_path)
    config = _config(tmp_path)
    seed_series_graph(config=config)
    return config


def _seeded_text_files(dist_root: Path) -> dict[str, str]:
    return {
        path.relative_to(dist_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(dist_root.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".js", ".html", ".md", ".css"}
        and "examples" not in path.relative_to(dist_root).parts
    }


def test_seeded_scripts_import_the_configured_package(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    scripts = config.dist_root / "scripts"

    serve = (scripts / "serve_graph_api.py").read_text(encoding="utf-8")
    bootstrap = (scripts / "write_graph_bootstrap.py").read_text(encoding="utf-8")
    check = (scripts / "check_graph_eval.py").read_text(encoding="utf-8")

    assert "from my_model.graph_api import" in serve
    assert "from my_model.graph_schema import BackendName" in serve
    assert "from my_model.graph_api import available_backends, bootstrap" in bootstrap
    assert "from my_model import graph_formula_evaluator as fe" in check


def test_seeded_evaluator_defaults_to_the_differential_workbook_fixture(
    tmp_path: Path,
) -> None:
    config = _seed(tmp_path)
    evaluator = (config.package_root / "graph_formula_evaluator.py").read_text(
        encoding="utf-8"
    )

    assignments = {
        target.id: ast.unparse(node.value)
        for node in ast.parse(evaluator).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments["DEFAULT_WORKBOOK"] == (
        "_REPO_ROOT / 'tests' / 'fixtures' / 'model-fixture.xlsx'"
    )


def test_seeded_evaluator_builds_the_graph_with_pipeline_blank_ranges(
    tmp_path: Path,
) -> None:
    config = _seed(tmp_path)
    tree = ast.parse(
        (config.package_root / "graph_formula_evaluator.py").read_text(encoding="utf-8")
    )

    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "blank_ranges"
        and node.level == 1
        and [alias.name for alias in node.names] == ["BLANK_RANGES"]
        for node in tree.body
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_dependency_graph"
    ]
    assert calls
    for call in calls:
        blank_ranges = {kw.arg: kw.value for kw in call.keywords}["blank_ranges"]
        assert isinstance(blank_ranges, ast.Name)
        assert blank_ranges.id == "BLANK_RANGES"


def test_seeded_evaluator_reads_input_ids_from_graph_schema(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    evaluator = (config.package_root / "graph_formula_evaluator.py").read_text(
        encoding="utf-8"
    )

    assert "INPUT_IDS" in evaluator
    assert "country_name" not in evaluator
    assert "_CONSTRAINTS_SCHEMA" not in evaluator


def test_seeded_files_are_free_of_project_specific_names(tmp_path: Path) -> None:
    config = _seed(tmp_path)

    offenders = {
        rel: sorted(set(_PROJECT_SPECIFIC.findall(text)))
        for rel, text in _seeded_text_files(config.dist_root).items()
        if _PROJECT_SPECIFIC.search(text)
    }

    assert offenders == {}


def test_seeded_files_have_no_unrendered_placeholders(tmp_path: Path) -> None:
    config = _seed(tmp_path)

    offenders = [
        rel
        for rel, text in _seeded_text_files(config.dist_root).items()
        if "__SERIES_GRAPH_" in text
    ]

    assert offenders == []
