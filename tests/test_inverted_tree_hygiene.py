"""Lock-in tests for cheap inverted-tree hygiene."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    parse_dev_dependencies_from_pyproject,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

REMOVED_PATHS = (
    Path("src/projection_columns.py"),
    Path("src/inverted_tree_export.py"),
    Path("src/runtime_symbols.py"),
    Path("scripts/export_inverted_tree.py"),
    Path("archive/extraction-pipeline.qmd"),
    Path("docs/extraction-pipeline.qmd"),
    Path("docs/extraction-pipeline.md"),
    Path("tests/test_runtime_symbols.py"),
    Path("tests/test_runtime_symbols_config.py"),
    Path("dist/tests/differential_test_exported_library.py"),
)

UNTRACKED_PATHS = (
    Path(".cache/internals-refactors.json"),
    Path(".cache/series-docstrings.json"),
    Path("docs/dependency-graph/dependency-graph.json"),
    Path("docs/dependency-graph/index.html"),
    Path("docs/human-hypothesis-graph/dependency-graph.json"),
    Path("docs/human-hypothesis-graph/index.html"),
)

REMOVED_EXTRACTION_DEV_DEPS = (
    "vl-convert-python",
    "pygments",
    "matplotlib",
    "great-docs",
    "pandas",
    "polars",
    "quarto",
)

KEPT_EXTRACTION_DEV_DEPS = (
    "fastpyxl",
    "xlwings",
    "pywin32",
)


def _package_name(dep: str) -> str:
    return dep.split("[", 1)[0].split("==", 1)[0].split(">=", 1)[0].strip()


def test_removed_hygiene_paths_are_gone() -> None:
    for relative in REMOVED_PATHS:
        assert not (REPO_ROOT / relative).exists(), relative


def test_section_rewrite_templates_are_gone() -> None:
    matches = sorted((REPO_ROOT / "templates").glob("section-rewrite-*.txt"))
    assert matches == []


def test_user_guide_agent_template_remains() -> None:
    assert (REPO_ROOT / "templates" / "user-guide-agent.txt").is_file()


def test_removed_modules_are_not_importable() -> None:
    for module in (
        "src.projection_columns",
        "src.inverted_tree_export",
        "src.runtime_symbols",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_env_example_documents_cursor_key_not_section_rewrite() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CURSOR_API_KEY=" in text
    assert "SECTION_REWRITE_MODEL" not in text


def test_extraction_venv_omits_unused_dev_dependencies() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    names = {_package_name(dep) for dep in parse_dev_dependencies_from_pyproject(text)}
    for package in REMOVED_EXTRACTION_DEV_DEPS:
        assert package not in names
    for package in KEPT_EXTRACTION_DEV_DEPS:
        assert package in names


def test_dist_baseline_still_lists_documentation_packages() -> None:
    names = {_package_name(dep) for dep in DOCUMENTATION_BASELINE_DEV_DEPS}
    assert names >= {"quarto", "great-docs", "pandas", "polars", "matplotlib"}


def test_ctx_era_cache_and_generated_graphs_are_untracked() -> None:
    listed = subprocess.check_output(
        ["git", "ls-files", "--", *[path.as_posix() for path in UNTRACKED_PATHS]],
        cwd=REPO_ROOT,
        text=True,
    )
    assert listed.strip() == ""


def test_stale_dist_root_harness_is_not_tracked() -> None:
    listed = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--",
            "dist/tests/differential_test_exported_library.py",
        ],
        cwd=REPO_ROOT,
        text=True,
    )
    assert listed.strip() == ""
