"""Tests for configurable paths in the graph-oracle differential harness."""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tests" / "differential" / "differential_test_graph.py"


def _load_harness_module():
    return importlib.import_module("tests.differential.differential_test_graph")


def test_repo_layout_defaults() -> None:
    harness = _load_harness_module()
    config = harness.resolve_config(
        module_path=MODULE_PATH,
        layout="repo",
    )
    assert config.workbook_path == (REPO_ROOT / "data" / "tiny-dsa.xlsx").resolve()
    assert config.report_dir == REPO_ROOT / "data" / "differential" / "graph"
    assert config.library_name == "Tiny DSA"
    assert config.atol == harness.ATOL


def test_parse_args_defaults_to_repo_layout() -> None:
    harness = _load_harness_module()
    args = harness.parse_args([])
    assert args.layout == "repo"
