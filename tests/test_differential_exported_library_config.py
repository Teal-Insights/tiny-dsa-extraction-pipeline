"""Tests for configurable paths in the exported-library differential harness."""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "tests" / "differential" / "differential_test_exported_library.py"
)


def _load_harness_module():
    return importlib.import_module(
        "tests.differential.differential_test_exported_library"
    )


def test_repo_layout_defaults() -> None:
    harness = _load_harness_module()
    config = harness.resolve_config(
        module_path=MODULE_PATH,
        layout="repo",
    )
    assert config.workbook_path == REPO_ROOT / "data" / "tiny-dsa.xlsx"
    assert config.package_dir == REPO_ROOT / "dist" / "tiny_dsa"
    assert config.package_name == "dist.tiny_dsa.api"
    assert config.import_root == REPO_ROOT
    assert config.report_dir == REPO_ROOT / "data" / "differential" / "exported_library"
    assert config.library_name == "Tiny DSA"


def test_exported_layout_defaults() -> None:
    harness = _load_harness_module()
    dist_root = REPO_ROOT / "dist"
    module_path = (
        dist_root / "tests" / "differential" / "differential_test_exported_library.py"
    )
    config = harness.resolve_config(
        module_path=module_path,
        layout="exported",
    )
    tests_root = dist_root / "tests"
    assert config.workbook_path == tests_root / "fixtures" / "tiny-dsa.xlsx"
    assert config.package_dir == dist_root / "tiny_dsa"
    assert config.package_name == "tiny_dsa.api"
    assert config.import_root == dist_root
    assert config.report_dir == tests_root / "results" / "local"
    assert config.library_name == "Tiny DSA"


def test_parse_args_defaults_to_repo_layout() -> None:
    harness = _load_harness_module()
    args = harness.parse_args([])
    assert args.layout == "repo"


def test_parse_args_accepts_exported_layout() -> None:
    harness = _load_harness_module()
    args = harness.parse_args(["--layout", "exported"])
    assert args.layout == "exported"
